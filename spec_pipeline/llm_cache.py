"""
LLM_Cache - On-disk record/replay store for LLM prompts and responses.

Implements the determinism/auditability requirements R21.4 and R21.9: each
response is keyed on the SHA-256 digest of (system_prompt, user_prompt, model,
temperature). On a key hit the recorded response is replayed and NO provider
call is issued. On a miss the caller performs the provider call and the response
is written to the cache under the computed key before it is returned.

The cache is gated by the ``LLM_CACHE_DIR`` environment variable or an explicit
directory argument. When neither is present the cache is disabled and every
call falls through to the provider.

Entries are stored as JSON files named ``<digest>.json`` so a run can be
replayed byte-identically and audited after the fact. The API key value is
never part of the key and is never written into an entry.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable, Optional


def cache_key(system_prompt: str, user_prompt: str, model: str, temperature: float) -> str:
    """Return the SHA-256 digest that keys a response (R21.4).

    The temperature is normalized to a stable decimal string so that ``0.2`` and
    ``0.20`` map to the same key.
    """
    hasher = hashlib.sha256()
    for part in (system_prompt, user_prompt, model, _normalize_temperature(temperature)):
        # Length-prefix each field so concatenation is unambiguous and two
        # different field splits can never collide.
        encoded = part.encode("utf-8")
        hasher.update(str(len(encoded)).encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(encoded)
    return hasher.hexdigest()


def _normalize_temperature(temperature: float) -> str:
    """Render *temperature* as a stable string for keying."""
    return format(float(temperature), ".6f")


class LLMCache:
    """Record/replay store for LLM responses.

    The cache is active only when a directory is configured. When inactive
    ``get`` always misses and ``put`` is a no-op, so callers can wire the cache
    unconditionally and rely on the gate.
    """

    def __init__(self, cache_dir: Optional[os.PathLike | str] = None):
        resolved = cache_dir if cache_dir is not None else os.environ.get("LLM_CACHE_DIR")
        self.cache_dir: Optional[Path] = Path(resolved) if resolved else None

    @property
    def enabled(self) -> bool:
        """True when a cache directory is configured (R21.4 gate)."""
        return self.cache_dir is not None

    def _entry_path(self, key: str) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / f"{key}.json"

    def get(self, system_prompt: str, user_prompt: str, model: str, temperature: float) -> Optional[str]:
        """Return the recorded response for the key, or None on a miss.

        A hit issues no provider call; the recorded response is returned as
        written (R21.9).
        """
        if not self.enabled:
            return None
        key = cache_key(system_prompt, user_prompt, model, temperature)
        path = self._entry_path(key)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        response = entry.get("response")
        return response if isinstance(response, str) else None

    def put(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        response: str,
    ) -> Optional[Path]:
        """Write *response* under the computed key. No-op when disabled.

        The API key is never part of the entry (redaction, R21.2/R7.6).
        """
        if not self.enabled:
            return None
        key = cache_key(system_prompt, user_prompt, model, temperature)
        self.cache_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        path = self._entry_path(key)
        entry = {
            "key": key,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "model": model,
            "temperature": _normalize_temperature(temperature),
            "response": response,
        }
        path.write_text(
            json.dumps(entry, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def get_or_call(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
        provider_call: Callable[[], str],
    ) -> str:
        """Return a cached response on a hit, else call *provider_call*.

        On a hit ``provider_call`` is never invoked (R21.9). On a miss the
        provider response is recorded before it is returned.
        """
        hit = self.get(system_prompt, user_prompt, model, temperature)
        if hit is not None:
            return hit
        response = provider_call()
        self.put(system_prompt, user_prompt, model, temperature, response)
        return response
