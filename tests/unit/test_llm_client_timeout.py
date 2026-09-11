"""Unit tests for LLMClient per-request timeout wiring.

A hung completion must never stall the pipeline, so ``_direct_call`` now passes
an env-configurable timeout (``LLM_REQUEST_TIMEOUT``, default 120.0s) to BOTH
the OpenAI client constructor (with ``max_retries=0`` so our own retry loop is
the single source of retry truth) and the per-request ``create`` call.

These tests are hermetic: no real network or openai server is used. Because
``_direct_call`` imports ``openai`` lazily and constructs ``OpenAI()``, we
inject a fake ``openai`` module via ``sys.modules`` whose ``OpenAI`` records the
kwargs it was constructed and called with.

Like ``test_llm_cache.py``, the ``spec_pipeline`` package ``__init__`` eagerly
imports slither-backed stages that the offline environment does not install, so
we load ``llm_cache``/``llm_client`` directly from their file paths.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parents[2] / "spec_pipeline"


def _load_module(name: str, filename: str):
    """Load a spec_pipeline submodule without triggering the package __init__."""
    full_name = f"spec_pipeline.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    if "spec_pipeline" not in sys.modules:
        pkg_spec = importlib.util.spec_from_loader("spec_pipeline", loader=None, is_package=True)
        pkg = importlib.util.module_from_spec(pkg_spec)
        pkg.__path__ = [str(_PKG_DIR)]
        sys.modules["spec_pipeline"] = pkg
    spec = importlib.util.spec_from_file_location(full_name, _PKG_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


llm_cache = _load_module("llm_cache", "llm_cache.py")
llm_client = _load_module("llm_client", "llm_client.py")
LLMCache = llm_cache.LLMCache
LLMClient = llm_client.LLMClient


class _FakeTimeout(Exception):
    """Stand-in for openai.APITimeoutError (a plain Exception subclass)."""


def _make_fake_openai(create_impl, records):
    """Build a fake ``openai`` module.

    ``records`` collects the client-constructor kwargs and per-call kwargs so
    tests can assert on the wiring. ``create_impl`` is invoked as
    ``create_impl(records)`` and must return the fake response content string
    (or raise to simulate a timeout).
    """

    class _Message:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Message(content)

    class _Response:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class _Completions:
        def create(self, **kwargs):
            records["create_kwargs"].append(kwargs)
            content = create_impl(records)
            return _Response(content)

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _OpenAI:
        def __init__(self, **kwargs):
            records["client_kwargs"].append(kwargs)
            self.chat = _Chat()

    module = types.ModuleType("openai")
    module.OpenAI = _OpenAI
    return module


def _install_fake_openai(monkeypatch, create_impl):
    records = {"client_kwargs": [], "create_kwargs": []}
    monkeypatch.setitem(sys.modules, "openai", _make_fake_openai(create_impl, records))
    return records


def _fresh_client(monkeypatch, tmp_path):
    """LLMClient with a MISSING cache and no auto_spec provider."""
    monkeypatch.delenv("AUTO_SPEC_PATH", raising=False)
    monkeypatch.delenv("LLM_CACHE_DIR", raising=False)
    monkeypatch.setenv("LLM_MODEL", "model-x")
    monkeypatch.setenv("LLM_BASE_URL", "http://example/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret-key-value")
    cache = LLMCache()  # no dir -> disabled -> always misses
    assert cache.enabled is False
    return LLMClient(cache=cache)


def test_default_timeout_is_120_and_max_retries_zero(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)
    records = _install_fake_openai(monkeypatch, lambda r: "ok")
    client = _fresh_client(monkeypatch, tmp_path)

    result = client.call("sys", "user", temperature=0.2)
    assert result == "ok"

    assert records["client_kwargs"][0]["timeout"] == 120.0
    assert records["client_kwargs"][0]["max_retries"] == 0
    assert records["create_kwargs"][0]["timeout"] == 120.0


def test_override_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "30")
    records = _install_fake_openai(monkeypatch, lambda r: "ok")
    client = _fresh_client(monkeypatch, tmp_path)

    client.call("sys", "user", temperature=0.2)
    assert records["client_kwargs"][0]["timeout"] == 30.0
    assert records["create_kwargs"][0]["timeout"] == 30.0


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "", "inf", "nan"])
def test_invalid_timeout_falls_back_to_default(monkeypatch, tmp_path, bad):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", bad)
    records = _install_fake_openai(monkeypatch, lambda r: "ok")
    client = _fresh_client(monkeypatch, tmp_path)

    client.call("sys", "user", temperature=0.2)
    assert records["client_kwargs"][0]["timeout"] == 120.0
    assert records["create_kwargs"][0]["timeout"] == 120.0


def test_timeout_then_success_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_a, **_k: None)

    def create_impl(records):
        if len(records["create_kwargs"]) == 1:
            raise _FakeTimeout("Request timed out.")
        return "second-try-content"

    records = _install_fake_openai(monkeypatch, create_impl)
    client = _fresh_client(monkeypatch, tmp_path)

    result = client.call("sys", "user", temperature=0.2)
    assert result == "second-try-content"
    assert len(records["create_kwargs"]) == 2


def test_persistent_timeout_raises_runtimeerror_with_timeout_no_key(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_client.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "45")

    def create_impl(records):
        raise _FakeTimeout("Request timed out.")

    records = _install_fake_openai(monkeypatch, create_impl)
    client = _fresh_client(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError) as exc_info:
        client.call("sys", "user", temperature=0.2)

    message = str(exc_info.value)
    assert "timeout=45.0s" in message
    assert "secret-key-value" not in message
    # Retried the full max_retries (default 3) times before giving up.
    assert len(records["create_kwargs"]) == 3
