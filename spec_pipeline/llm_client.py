"""
LLM Client - Provider-agnostic LLM calls.

Selection order:
1. An optional Auto-Spec provider, discovered only when the ``AUTO_SPEC_PATH``
   environment variable names an existing readable directory (R7.5). The
   pipeline no longer hardcodes ``../../Auto-Spec`` on ``sys.path``.
2. A direct OpenAI-compatible call configured via ``LLM_API_KEY``,
   ``LLM_BASE_URL``, and ``LLM_MODEL``, with exponential-backoff retries.

The client records the selected provider name, resolved base URL, and model so
the orchestrator can persist them into the Run_Manifest; the API key value is
never recorded (R7.6). Each call can be logged to a run log directory when one
is configured (R21.3), and an LLM_Cache short-circuits the provider on a hit
(R21.4, R21.9). The ``openai`` package is imported lazily so importing this
module never requires it and never makes a network call.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .llm_cache import LLMCache

# Default per-request timeout (seconds) applied to direct OpenAI-compatible
# calls when ``LLM_REQUEST_TIMEOUT`` is unset or invalid.
DEFAULT_REQUEST_TIMEOUT = 120.0


def _resolve_request_timeout() -> float:
    """Resolve the per-request timeout from ``LLM_REQUEST_TIMEOUT`` (seconds).

    Non-numeric, non-positive, or non-finite values fall back to
    ``DEFAULT_REQUEST_TIMEOUT`` so a misconfiguration can never disable the
    timeout (which would reintroduce the indefinite-hang bug).
    """
    import math

    raw = os.environ.get("LLM_REQUEST_TIMEOUT")
    if raw is None:
        return DEFAULT_REQUEST_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_REQUEST_TIMEOUT
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_REQUEST_TIMEOUT
    return value


def _load_auto_spec_provider():
    """Discover the optional Auto-Spec provider via ``AUTO_SPEC_PATH`` (R7.5).

    The directory is added to ``sys.path`` only when the env var is set and
    resolves to an existing readable directory. When absent or invalid, nothing
    is added and the caller falls through to the OpenAI-compatible path.
    """
    auto_spec_path = os.environ.get("AUTO_SPEC_PATH")
    if not auto_spec_path:
        return None, None
    candidate = Path(auto_spec_path)
    if not (candidate.is_dir() and os.access(candidate, os.R_OK)):
        return None, None

    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.append(candidate_str)
    try:
        from auto_spec.config import get_config  # type: ignore
        from auto_spec.call_llm import _raw_llm_call  # type: ignore
    except ImportError:
        return None, None
    return get_config, _raw_llm_call


class LLMClient:
    """Provider-agnostic LLM caller with caching and run logging."""

    def __init__(self, config=None, cache: Optional[LLMCache] = None,
                 run_log_dir: Optional[os.PathLike | str] = None):
        get_config, raw_call = _load_auto_spec_provider()
        self.config = config or (get_config() if get_config else None)
        self._raw_call = raw_call
        self.cache = cache if cache is not None else LLMCache()
        self.run_log_dir: Optional[Path] = Path(run_log_dir) if run_log_dir else None

        # Direct-call configuration (resolved once so provider_info is stable).
        self._api_key = os.environ.get("LLM_API_KEY", "omniroute-local")
        self._base_url = os.environ.get("LLM_BASE_URL", "http://localhost:20127/v1")
        self._model = os.environ.get("LLM_MODEL", "auto")
        # Per-request timeout (seconds) so a hung completion can never stall the
        # pipeline indefinitely. Non-numeric, <=0, or non-finite values fall
        # back to the default.
        self._request_timeout = _resolve_request_timeout()

        if self._raw_call and self.config:
            self._provider_name = "auto_spec"
            self._resolved_base_url = getattr(self.config, "base_url", self._base_url)
            self._resolved_model = getattr(self.config, "model", self._model)
        else:
            self._provider_name = "openai_compatible"
            self._resolved_base_url = self._base_url
            self._resolved_model = self._model

    def provider_info(self) -> dict:
        """Return the selected provider, base URL, and model (R7.6).

        The API key value is deliberately excluded so the orchestrator can
        persist this dict into the Run_Manifest without leaking secrets.
        """
        return {
            "provider": self._provider_name,
            "base_url": self._resolved_base_url,
            "model": self._resolved_model,
        }

    def call(self, system_prompt: str, user_prompt: str, temperature: float = 0.2) -> str:
        """Call LLM with system + user prompt.

        A cache hit short-circuits the provider entirely (R21.4, R21.9). On a
        miss the provider is called, the response recorded to the cache, and the
        call logged to the run log directory when configured (R21.3).
        """
        model = self._resolved_model

        # Cache hit: replay recorded response, no provider call.
        cached = self.cache.get(system_prompt, user_prompt, model, temperature)
        if cached is not None:
            self._log_call(system_prompt, user_prompt, cached, model, temperature,
                           token_counts=None, cached=True)
            return cached

        response = self._provider_call(system_prompt, user_prompt, temperature)
        self.cache.put(system_prompt, user_prompt, model, temperature, response)
        self._log_call(system_prompt, user_prompt, response, model, temperature,
                       token_counts=None, cached=False)
        return response

    def _provider_call(self, system_prompt: str, user_prompt: str, temperature: float) -> str:
        """Invoke the selected provider."""
        if self._raw_call and self.config:
            class _Ctx:
                pass
            ctx = _Ctx()
            ctx.config = self.config
            return self._raw_call(ctx, system_prompt, user_prompt, temperature)
        return self._call_with_retry(system_prompt, user_prompt, temperature)

    def _call_with_retry(self, system_prompt: str, user_prompt: str, temperature: float,
                         max_retries: int = 3, base_delay: float = 2.0) -> str:
        """Call LLM with exponential backoff retry."""
        for attempt in range(max_retries):
            try:
                return self._direct_call(system_prompt, user_prompt, temperature,
                                         api_key=self._api_key, base_url=self._base_url,
                                         model=self._model, timeout=self._request_timeout)
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    print(f"LLM call failed (attempt {attempt + 1}/{max_retries}): {e}")
                    print(f"Retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    # Last attempt failed - provide helpful error (key not shown).
                    raise RuntimeError(
                        f"LLM call failed after {max_retries} attempts "
                        f"(timeout={self._request_timeout}s per attempt). "
                        f"Last error: {e}\n"
                        f"Configuration: base_url={self._base_url}, model={self._model}\n"
                        f"Set LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_REQUEST_TIMEOUT "
                        f"env vars to configure."
                    ) from e

    def _direct_call(self, system_prompt: str, user_prompt: str, temperature: float,
                     api_key: str, base_url: str, model: str,
                     timeout: float = DEFAULT_REQUEST_TIMEOUT) -> str:
        """Direct OpenAI-compatible call. Imports openai lazily.

        ``timeout`` caps each request so a hung completion cannot stall the
        pipeline. ``max_retries=0`` disables the SDK's own retry loop so our
        ``_call_with_retry`` remains the single source of retry behavior.
        """
        from openai import OpenAI

        client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout,
                        max_retries=0)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "100000")),
            temperature=temperature,
            timeout=timeout,
        )
        return (response.choices[0].message.content or "").strip()

    def _log_call(self, system_prompt: str, user_prompt: str, response: str,
                  model: str, temperature: float, token_counts: Optional[dict],
                  cached: bool) -> None:
        """Append a call record to the run log directory when configured (R21.3).

        The record holds the prompts, response, model, temperature, and reported
        token counts. The API key is never written.
        """
        if self.run_log_dir is None:
            return
        try:
            self.run_log_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response": response,
                "model": model,
                "temperature": temperature,
                "token_counts": token_counts,
                "provider": self._provider_name,
                "cached": cached,
            }
            log_path = self.run_log_dir / "llm_calls.jsonl"
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Logging is best-effort and must never break a call.
            pass


def get_llm_client() -> LLMClient:
    """Get or create LLM client."""
    return LLMClient()
