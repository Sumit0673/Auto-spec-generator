"""Unit tests for LLM_Cache and LLMClient path handling (R7.5, R21.4, R21.9).

The ``spec_pipeline`` package ``__init__`` eagerly imports the slither-backed
stages, which are not needed to exercise the LLM cache/client and which the
offline test environment (Requirements 10.2, 23.14) does not install. These two
modules are self-contained (they only depend on each other), so we load them
directly from their file paths to keep the test independent of slither.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PKG_DIR = Path(__file__).resolve().parents[2] / "spec_pipeline"


def _load_module(name: str, filename: str):
    """Load a spec_pipeline submodule without triggering the package __init__."""
    full_name = f"spec_pipeline.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    # Register a lightweight parent package so relative imports resolve.
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
cache_key = llm_cache.cache_key
LLMClient = llm_client.LLMClient


class _ProviderExploded(RuntimeError):
    """Raised if the provider is invoked when a cache hit was expected."""


def test_cache_disabled_without_dir(monkeypatch):
    monkeypatch.delenv("LLM_CACHE_DIR", raising=False)
    cache = LLMCache()
    assert cache.enabled is False
    # Disabled cache always misses and put is a no-op.
    assert cache.get("s", "u", "m", 0.2) is None
    assert cache.put("s", "u", "m", 0.2, "resp") is None


def test_hit_replays_without_provider_call(tmp_path):
    cache = LLMCache(tmp_path)
    cache.put("sys", "user", "model-x", 0.2, "recorded-response")

    def boom():
        raise _ProviderExploded("provider must not be called on a hit")

    result = cache.get_or_call("sys", "user", "model-x", 0.2, boom)
    assert result == "recorded-response"


def test_hit_is_byte_identical(tmp_path):
    payload = "line1\nline2\twith tabs\nunicode: \u00e9\n"
    cache = LLMCache(tmp_path)
    cache.put("sys", "user", "model-x", 0.2, payload)
    assert cache.get("sys", "user", "model-x", 0.2) == payload


def test_miss_records_then_subsequent_call_hits(tmp_path):
    cache = LLMCache(tmp_path)
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return "fresh-response"

    # First call: miss -> provider invoked, response recorded.
    first = cache.get_or_call("sys", "user", "model-x", 0.2, provider)
    assert first == "fresh-response"
    assert calls["n"] == 1

    # Second identical call: hit -> provider NOT invoked again.
    def provider_boom():
        raise _ProviderExploded("should have hit the cache")

    second = cache.get_or_call("sys", "user", "model-x", 0.2, provider_boom)
    assert second == "fresh-response"
    assert calls["n"] == 1


def test_distinct_inputs_produce_distinct_keys():
    base = cache_key("sys", "user", "model", 0.2)
    assert cache_key("SYS", "user", "model", 0.2) != base
    assert cache_key("sys", "USER", "model", 0.2) != base
    assert cache_key("sys", "user", "MODEL", 0.2) != base
    assert cache_key("sys", "user", "model", 0.7) != base


def test_equivalent_temperature_formats_share_key():
    assert cache_key("s", "u", "m", 0.2) == cache_key("s", "u", "m", 0.20)


def test_key_prefixing_avoids_field_boundary_collision():
    # "ab"|"c" must not collide with "a"|"bc".
    assert cache_key("ab", "c", "m", 0.2) != cache_key("a", "bc", "m", 0.2)


def test_client_cache_hit_short_circuits_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "model-x")
    monkeypatch.delenv("AUTO_SPEC_PATH", raising=False)
    cache = LLMCache(tmp_path)
    cache.put("sys", "user", "model-x", 0.2, "cached-answer")

    client = LLMClient(cache=cache)

    def explode(*args, **kwargs):
        raise _ProviderExploded("provider must not run on a cache hit")

    monkeypatch.setattr(client, "_provider_call", explode)
    assert client.call("sys", "user", temperature=0.2) == "cached-answer"


def test_provider_info_excludes_api_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "super-secret-value")
    monkeypatch.setenv("LLM_BASE_URL", "http://example/v1")
    monkeypatch.setenv("LLM_MODEL", "model-z")
    monkeypatch.delenv("AUTO_SPEC_PATH", raising=False)

    client = LLMClient()
    info = client.provider_info()
    assert info == {
        "provider": "openai_compatible",
        "base_url": "http://example/v1",
        "model": "model-z",
    }
    assert "super-secret-value" not in str(info)


def test_missing_auto_spec_path_adds_nothing_to_syspath(monkeypatch):
    monkeypatch.delenv("AUTO_SPEC_PATH", raising=False)
    before = list(sys.path)
    get_config, raw_call = llm_client._load_auto_spec_provider()
    assert get_config is None and raw_call is None
    assert sys.path == before


def test_nonexistent_auto_spec_path_adds_nothing_to_syspath(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("AUTO_SPEC_PATH", str(missing))
    before = list(sys.path)
    get_config, raw_call = llm_client._load_auto_spec_provider()
    assert get_config is None and raw_call is None
    assert sys.path == before
    assert str(missing) not in sys.path


def test_run_log_records_call(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "model-x")
    monkeypatch.delenv("AUTO_SPEC_PATH", raising=False)
    cache = LLMCache(tmp_path / "cache")
    cache.put("sys", "user", "model-x", 0.2, "answer")

    log_dir = tmp_path / "logs"
    client = LLMClient(cache=cache, run_log_dir=log_dir)
    client.call("sys", "user", temperature=0.2)

    log_file = log_dir / "llm_calls.jsonl"
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")
    assert "\"model\": \"model-x\"" in content
    assert "\"response\": \"answer\"" in content
