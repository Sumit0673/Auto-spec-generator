"""Unit tests for Artifact_Store staleness detection (task 2.6).

Covers :func:`spec_pipeline.artifacts.is_stale` and its precedence
(Requirements 3.3, 3.4, 3.5; precedence per design R3.6): one reason per call in
the fixed order ``absent_provenance`` -> ``version`` -> ``source_fingerprint`` ->
``consumed``, or ``None`` when fresh.

The module is loaded via a direct importlib file-load (mirroring
``test_artifacts_basics.py``) so the pure ``artifacts`` module can be tested
without triggering ``spec_pipeline/__init__``'s eager, slither-backed imports.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

try:
    from spec_pipeline.artifacts import (
        Staleness,
        is_stale,
        STALE_ABSENT_PROVENANCE,
        STALE_VERSION,
        STALE_SOURCE_FINGERPRINT,
        STALE_CONSUMED,
    )
except Exception:  # pragma: no cover - fallback when spec_pipeline.__init__ deps absent
    _ARTIFACTS_PATH = (
        Path(__file__).resolve().parents[2] / "spec_pipeline" / "artifacts.py"
    )
    _MOD_NAME = "spec_pipeline_artifacts_stale_under_test"
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _ARTIFACTS_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    # Register before exec so @dataclass can resolve cls.__module__ in sys.modules
    # while the module body is still executing.
    sys.modules[_MOD_NAME] = _mod
    _spec.loader.exec_module(_mod)
    Staleness = _mod.Staleness
    is_stale = _mod.is_stale
    STALE_ABSENT_PROVENANCE = _mod.STALE_ABSENT_PROVENANCE
    STALE_VERSION = _mod.STALE_VERSION
    STALE_SOURCE_FINGERPRINT = _mod.STALE_SOURCE_FINGERPRINT
    STALE_CONSUMED = _mod.STALE_CONSUMED


# A minimal provenance-like stand-in so the test does not depend on task 2.2's
# Provenance having landed. is_stale inspects it structurally via getattr.
@dataclass
class _FakeProv:
    pipeline_version: str
    source_fingerprint: str
    consumed: list = field(default_factory=list)


_VERSION = "1.2.3"
_FP = "sha256:aaaa"


def _fresh_prov(consumed=None) -> _FakeProv:
    return _FakeProv(
        pipeline_version=_VERSION,
        source_fingerprint=_FP,
        consumed=consumed if consumed is not None else [],
    )


# ---------------------------------------------------------------------------
# absent / malformed provenance  (highest precedence)
# ---------------------------------------------------------------------------


def test_none_provenance_is_absent():
    result = is_stale(None, _FP, _VERSION, {})
    assert result is not None
    assert result.reason == STALE_ABSENT_PROVENANCE
    assert result.current == _VERSION


def test_missing_field_provenance_is_absent():
    # An object missing the required attributes is treated as absent, not fresh.
    class _Partial:
        pipeline_version = _VERSION
        # no source_fingerprint, no consumed

    result = is_stale(_Partial(), _FP, _VERSION, {})
    assert result is not None
    assert result.reason == STALE_ABSENT_PROVENANCE


# ---------------------------------------------------------------------------
# version mismatch
# ---------------------------------------------------------------------------


def test_changed_version():
    prov = _FakeProv(pipeline_version="9.9.9", source_fingerprint=_FP, consumed=[])
    result = is_stale(prov, _FP, _VERSION, {})
    assert result is not None
    assert result.reason == STALE_VERSION
    assert result.recorded == "9.9.9"
    assert result.current == _VERSION


# ---------------------------------------------------------------------------
# source fingerprint mismatch
# ---------------------------------------------------------------------------


def test_changed_fingerprint():
    prov = _FakeProv(
        pipeline_version=_VERSION, source_fingerprint="sha256:old", consumed=[]
    )
    result = is_stale(prov, _FP, _VERSION, {})
    assert result is not None
    assert result.reason == STALE_SOURCE_FINGERPRINT
    assert result.recorded == "sha256:old"
    assert result.current == _FP


# ---------------------------------------------------------------------------
# consumed input mismatch / missing
# ---------------------------------------------------------------------------


def test_changed_consumed_digest():
    prov = _fresh_prov(consumed=[(1, "sha256:input-old")])
    result = is_stale(prov, _FP, _VERSION, {1: "sha256:input-new"})
    assert result is not None
    assert result.reason == STALE_CONSUMED
    assert result.recorded == "sha256:input-old"
    assert result.current == "sha256:input-new"


def test_missing_consumed_input():
    # Recorded a consumed stage-1 input, but disk_inputs has no digest for it.
    prov = _fresh_prov(consumed=[(1, "sha256:input-old")])
    result = is_stale(prov, _FP, _VERSION, {})
    assert result is not None
    assert result.reason == STALE_CONSUMED
    assert result.recorded == "sha256:input-old"
    assert result.current is None


# ---------------------------------------------------------------------------
# fresh
# ---------------------------------------------------------------------------


def test_fresh_returns_none():
    prov = _fresh_prov(consumed=[(1, "sha256:input")])
    assert is_stale(prov, _FP, _VERSION, {1: "sha256:input"}) is None


def test_fresh_with_no_consumed_returns_none():
    assert is_stale(_fresh_prov(), _FP, _VERSION, {}) is None


# ---------------------------------------------------------------------------
# precedence: only one reason returned, highest-priority wins
# ---------------------------------------------------------------------------


def test_precedence_version_over_fingerprint_and_consumed():
    # Everything is wrong at once; version must win.
    prov = _FakeProv(
        pipeline_version="9.9.9",
        source_fingerprint="sha256:old",
        consumed=[(1, "sha256:input-old")],
    )
    result = is_stale(prov, _FP, _VERSION, {1: "sha256:input-new"})
    assert result is not None
    assert result.reason == STALE_VERSION


def test_precedence_fingerprint_over_consumed():
    prov = _FakeProv(
        pipeline_version=_VERSION,
        source_fingerprint="sha256:old",
        consumed=[(1, "sha256:input-old")],
    )
    result = is_stale(prov, _FP, _VERSION, {1: "sha256:input-new"})
    assert result is not None
    assert result.reason == STALE_SOURCE_FINGERPRINT


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
