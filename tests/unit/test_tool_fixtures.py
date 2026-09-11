"""Unit tests for the recorded-tool shims and the LLM_Cache fixture loader
(task 1.3).

Covers Requirements 10.4 and 10.8 (a shimmed ``solc``/``slither``/``certoraRun``
replays its recording keyed by argv, and a missing recording fails naming the
invocation) and Requirements 10.3 and 10.7 (the LLM_Cache fixture replays a
seeded digest and fails naming the absent digest on a miss).

These are fixture-backed, one-to-three-examples-per-case tests, not property
tests (Requirement 23.11): they exercise external-tool substitution behavior.

The shims and the LLM_Cache loader are wired in ``tests/conftest.py`` as the
``recorded_tools`` and ``llm_cache_fixture`` fixtures. The shim replay engine
lives in ``tests/fixtures/tools/_shim.py``; the seeded cache entry lives under
``tests/fixtures/llm_cache/``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[1]
_TOOLS_FIXTURE_DIR = _TESTS_DIR / "fixtures" / "tools"
_SEED_RECORDINGS = _TOOLS_FIXTURE_DIR / "recordings.json"

# The seeded LLM_Cache entry's prompt inputs (see the fixture JSON). The digest
# is derived from these via llm_cache.cache_key, so the test never hard-codes
# the digest string.
_SEED_SYSTEM = "You are a Certora CVL spec generator."
_SEED_USER = "Generate a CVL rule for the Counter contract increment function."
_SEED_MODEL = "gpt-4o-mini"
_SEED_TEMPERATURE = 0.2


# ---------------------------------------------------------------------------
# Recorded tool shims (Requirements 10.4, 10.8)
# ---------------------------------------------------------------------------


def _which_on_path() -> str | None:
    """Resolve ``solc`` on the current PATH (the shim once activated)."""
    return shutil.which("solc")


def test_shim_replays_recorded_solc_invocation(recorded_tools):
    """A shimmed tool replays its recording keyed by argv (R10.4)."""
    recorded_tools()  # activate with the seeded recordings.json

    resolved = _which_on_path()
    assert resolved is not None, "solc shim must be resolvable on PATH once activated"
    assert Path(resolved).parent == _TOOLS_FIXTURE_DIR

    result = subprocess.run(
        ["solc", "--version"], capture_output=True, text=True
    )
    # The recorded stdout/exit are replayed verbatim; no real solc is involved.
    assert result.returncode == 0
    assert "0.8.19" in result.stdout
    assert "solidity compiler" in result.stdout


def test_shim_replays_each_recorded_tool(recorded_tools):
    """Each of solc/slither/certoraRun replays its own recording (R10.4)."""
    recorded_tools()
    expectations = {
        "solc": "0.8.19",
        "slither": "0.10.0",
        "certoraRun": "certora-cli 7.0.0",
    }
    for tool, needle in expectations.items():
        result = subprocess.run([tool, "--version"], capture_output=True, text=True)
        assert result.returncode == 0, f"{tool} shim should exit 0 for a recorded call"
        assert needle in result.stdout, f"{tool} shim replayed unexpected stdout"


def test_shim_missing_recording_fails_naming_invocation(recorded_tools):
    """An unrecorded invocation fails naming the tool and its argv (R10.8)."""
    recorded_tools()
    # No record exists for `solc --bogus-flag someInput.sol`.
    result = subprocess.run(
        ["solc", "--bogus-flag", "someInput.sol"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "an unrecorded invocation must fail loudly"
    combined = result.stdout + result.stderr
    assert "NO RECORDING" in combined
    # The diagnostic names the tool and the exact argv that was not recorded.
    assert "solc" in combined
    assert "--bogus-flag" in combined
    assert "someInput.sol" in combined


def test_shim_missing_recordings_file_fails(recorded_tools, tmp_path):
    """A recordings file that does not exist fails naming the path (R10.8)."""
    missing = tmp_path / "does_not_exist.json"
    recorded_tools(missing)
    result = subprocess.run(["solc", "--version"], capture_output=True, text=True)
    assert result.returncode != 0
    assert "cannot replay" in (result.stdout + result.stderr)


def test_shim_catch_all_argv_record(recorded_tools, tmp_path):
    """A record with argv ``\"*\"`` matches any argv for that tool (R10.4)."""
    recordings = tmp_path / "recordings.json"
    recordings.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "tool": "certoraRun",
                        "argv": "*",
                        "stdout": "catch-all replayed\n",
                        "exit": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    recorded_tools(recordings)
    result = subprocess.run(
        ["certoraRun", "--verify", "Counter:spec.spec"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "catch-all replayed" in result.stdout


def test_shim_not_active_without_fixture():
    """Without activating the fixture, the shim dir is not on PATH.

    The session guard keeps real solc/certoraRun off PATH; this documents that
    a test which does not opt into ``recorded_tools`` sees neither the real tool
    nor the shim, so the "tools absent" default holds.
    """
    resolved = shutil.which("solc")
    if resolved is not None:
        # If anything resolves, it must not be our shim dir.
        assert Path(resolved).parent != _TOOLS_FIXTURE_DIR


# ---------------------------------------------------------------------------
# LLM_Cache fixture loader (Requirements 10.3, 10.7)
# ---------------------------------------------------------------------------


def test_llm_cache_fixture_hits_seeded_digest(llm_cache_fixture):
    """The fixture replays the recorded response for a seeded digest (R10.3)."""
    response = llm_cache_fixture.get(
        _SEED_SYSTEM, _SEED_USER, _SEED_MODEL, _SEED_TEMPERATURE
    )
    assert "rule increment_increases_count" in response
    assert "```cvl" in response


def test_llm_cache_fixture_key_matches_llm_cache_module(llm_cache_fixture):
    """The fixture keys on the same digest as spec_pipeline.llm_cache (R10.3)."""
    from spec_pipeline.llm_cache import cache_key

    expected = cache_key(_SEED_SYSTEM, _SEED_USER, _SEED_MODEL, _SEED_TEMPERATURE)
    assert llm_cache_fixture.key_for(
        _SEED_SYSTEM, _SEED_USER, _SEED_MODEL, _SEED_TEMPERATURE
    ) == expected
    # And the seeded entry replays through get_by_digest for that digest.
    assert "increment" in llm_cache_fixture.get_by_digest(expected)


def test_llm_cache_fixture_miss_names_absent_digest(llm_cache_fixture):
    """An unknown prompt misses, and the error names the absent digest (R10.7)."""
    from spec_pipeline.llm_cache import cache_key

    unknown_digest = cache_key("unseen system", "unseen user", "some-model", 0.7)
    with pytest.raises(KeyError) as excinfo:
        llm_cache_fixture.get("unseen system", "unseen user", "some-model", 0.7)
    # The diagnostic names the exact absent digest so the miss is actionable.
    assert unknown_digest in str(excinfo.value)


def test_llm_cache_fixture_seeded_entry_is_llmcache_compatible(llm_cache_fixture):
    """The seeded entry is readable by the real LLMCache too (R21.4 shape).

    The fixture and the production ``LLMCache`` share the ``<digest>.json`` on
    disk format, so an entry seeded for a test replays through either reader.
    """
    from spec_pipeline.llm_cache import LLMCache

    cache = LLMCache(llm_cache_fixture.cache_dir)
    assert cache.enabled
    hit = cache.get(_SEED_SYSTEM, _SEED_USER, _SEED_MODEL, _SEED_TEMPERATURE)
    assert hit is not None
    assert hit == llm_cache_fixture.get(
        _SEED_SYSTEM, _SEED_USER, _SEED_MODEL, _SEED_TEMPERATURE
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
