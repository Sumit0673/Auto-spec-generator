"""Unit tests for the Artifact_Store canonical envelope writer/loader.

Covers the pieces added in task 2.2 (Requirements 2.6, 2.7, 3.1, 21.7):

* :class:`spec_pipeline.artifacts.Provenance`
* :func:`spec_pipeline.artifacts.write_artifact`
* :func:`spec_pipeline.artifacts.load_artifact`
* :class:`spec_pipeline.artifacts.ArtifactError`
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

try:
    # Normal import path once the package's optional native deps (slither) are
    # installed.
    from spec_pipeline.artifacts import (
        ArtifactError,
        Provenance,
        load_artifact,
        write_artifact,
    )
except Exception:  # pragma: no cover - fallback when spec_pipeline.__init__ deps absent
    # spec_pipeline/__init__.py eagerly imports the slither-backed stages, which
    # are unrelated to this pure module. Load artifacts.py directly so the
    # functions under test can be verified without the external toolchain.
    _ARTIFACTS_PATH = (
        Path(__file__).resolve().parents[2] / "spec_pipeline" / "artifacts.py"
    )
    _MOD_NAME = "spec_pipeline_artifacts_under_test"
    _spec = importlib.util.spec_from_file_location(_MOD_NAME, _ARTIFACTS_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    # Register in sys.modules BEFORE exec so that @dataclass (under
    # ``from __future__ import annotations``) can resolve the module via
    # ``sys.modules[cls.__module__]`` during class processing.
    sys.modules[_MOD_NAME] = _mod
    _spec.loader.exec_module(_mod)
    ArtifactError = _mod.ArtifactError
    Provenance = _mod.Provenance
    load_artifact = _mod.load_artifact
    write_artifact = _mod.write_artifact


def _prov(**overrides) -> Provenance:
    base = dict(
        pipeline_version="1.2.3",
        stage=1,
        completed_utc="2024-01-02T03:04:05Z",
        source_path="/proj/contracts/Pool.sol",
        source_fingerprint="sha256:abc123",
        consumed=[(0, "sha256:deadbeef")],
    )
    base.update(overrides)
    return Provenance(**base)


# ---------------------------------------------------------------------------
# Round-trip  (Requirements 2.7, 3.1)
# ---------------------------------------------------------------------------


def test_write_then_load_round_trips_payload_and_provenance(tmp_path):
    prov = _prov()
    payload = {"contracts": {"Pool": {"vars": ["x", "y"]}}, "note": "café"}

    path = write_artifact(tmp_path, "Pool", 1, payload, prov)
    assert path == tmp_path / "Pool_stage1.json"

    result = load_artifact(tmp_path, "Pool", 1)
    assert result.present is True
    assert result.error is None
    assert result.payload == payload
    assert result.provenance == prov


def test_stage1_payload_reports_contract_count(tmp_path):
    payload = {"contracts": {"A": {}, "B": {}, "C": {}}}
    write_artifact(tmp_path, "proj", 1, payload, _prov(stage=1))
    result = load_artifact(tmp_path, "proj", 1)
    assert result.contract_count == 3


def test_zero_contracts_payload_reports_count_zero(tmp_path):
    write_artifact(tmp_path, "proj", 1, {"contracts": {}}, _prov(stage=1))
    result = load_artifact(tmp_path, "proj", 1)
    assert result.present is True
    assert result.contract_count == 0


def test_non_stage1_payload_has_null_contract_count(tmp_path):
    write_artifact(tmp_path, "proj", 2, {"findings": [1, 2]}, _prov(stage=2))
    result = load_artifact(tmp_path, "proj", 2)
    assert result.contract_count is None


def test_empty_consumed_list_round_trips(tmp_path):
    prov = _prov(consumed=[])
    write_artifact(tmp_path, "proj", 1, {"contracts": {}}, prov)
    result = load_artifact(tmp_path, "proj", 1)
    assert result.provenance.consumed == []


# ---------------------------------------------------------------------------
# Canonical formatting  (Requirement 21.7)
# ---------------------------------------------------------------------------


def test_canonical_formatting_sorted_keys_indent_and_trailing_newline(tmp_path):
    # Payload keys deliberately out of order to prove they get sorted.
    payload = {"zeta": 1, "alpha": {"gamma": 2, "beta": 3}}
    path = write_artifact(tmp_path, "proj", 1, payload, _prov())
    text = path.read_text(encoding="utf-8")

    # Exactly one trailing newline.
    assert text.endswith("\n")
    assert not text.endswith("\n\n")

    # LF line endings only.
    assert "\r" not in text

    # Two-space indentation.
    assert '\n  "payload"' in text
    assert '\n  "provenance"' in text

    # Keys sorted at every depth: top-level payload < provenance, and within
    # payload alpha < zeta, and within alpha beta < gamma.
    assert text.index('"payload"') < text.index('"provenance"')
    assert text.index('"alpha"') < text.index('"zeta"')
    assert text.index('"beta"') < text.index('"gamma"')


def test_canonical_formatting_preserves_non_ascii(tmp_path):
    path = write_artifact(tmp_path, "proj", 1, {"name": "café_naïve"}, _prov())
    text = path.read_text(encoding="utf-8")
    # ensure_ascii=False keeps the literal characters instead of \uXXXX escapes.
    assert "café_naïve" in text


# ---------------------------------------------------------------------------
# Absent file  (Requirement 2.7 - not an error)
# ---------------------------------------------------------------------------


def test_absent_file_returns_present_false_no_error(tmp_path):
    result = load_artifact(tmp_path, "does-not-exist", 1)
    assert result.present is False
    assert result.error is None
    assert result.payload is None
    assert result.provenance is None
    assert result.contract_count is None


# ---------------------------------------------------------------------------
# Invalid JSON  (Requirement 2.8)
# ---------------------------------------------------------------------------


def test_invalid_json_raises_artifact_error_naming_path(tmp_path):
    bad = tmp_path / "proj_stage1.json"
    bad.write_text("{ not valid json ", encoding="utf-8")
    with pytest.raises(ArtifactError) as exc:
        load_artifact(tmp_path, "proj", 1)
    message = str(exc.value)
    assert str(bad) in message
    assert "invalid JSON" in message


# ---------------------------------------------------------------------------
# Missing / wrong-typed provenance fields  (Requirement 2.6)
# ---------------------------------------------------------------------------


def _write_raw_envelope(tmp_path: Path, base: str, stage: int, envelope_text: str):
    path = tmp_path / f"{base}_stage{stage}.json"
    path.write_text(envelope_text, encoding="utf-8")
    return path


def test_missing_provenance_field_raises_naming_field(tmp_path):
    # 'source_fingerprint' omitted from provenance.
    envelope = (
        '{"provenance": {"pipeline_version": "1", "stage": 1, '
        '"completed_utc": "t", "source_path": "p", "consumed": []}, '
        '"payload": {}}'
    )
    path = _write_raw_envelope(tmp_path, "proj", 1, envelope)
    with pytest.raises(ArtifactError) as exc:
        load_artifact(tmp_path, "proj", 1)
    message = str(exc.value)
    assert str(path) in message
    assert "source_fingerprint" in message


def test_wrong_typed_provenance_field_raises_naming_field(tmp_path):
    # 'stage' is a string instead of an integer.
    envelope = (
        '{"provenance": {"pipeline_version": "1", "stage": "one", '
        '"completed_utc": "t", "source_path": "p", '
        '"source_fingerprint": "sha256:x", "consumed": []}, '
        '"payload": {}}'
    )
    path = _write_raw_envelope(tmp_path, "proj", 1, envelope)
    with pytest.raises(ArtifactError) as exc:
        load_artifact(tmp_path, "proj", 1)
    message = str(exc.value)
    assert str(path) in message
    assert "stage" in message


def test_missing_top_level_payload_raises(tmp_path):
    envelope = (
        '{"provenance": {"pipeline_version": "1", "stage": 1, '
        '"completed_utc": "t", "source_path": "p", '
        '"source_fingerprint": "sha256:x", "consumed": []}}'
    )
    path = _write_raw_envelope(tmp_path, "proj", 1, envelope)
    with pytest.raises(ArtifactError) as exc:
        load_artifact(tmp_path, "proj", 1)
    assert "payload" in str(exc.value)
    assert str(path) in str(exc.value)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
