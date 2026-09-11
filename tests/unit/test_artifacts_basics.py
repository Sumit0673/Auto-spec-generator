"""Unit tests for the Artifact_Store base-name and fingerprint helpers.

Covers the two functions added in task 2.1 (Requirements 2.5, 3.2):

* :func:`spec_pipeline.artifacts.artifact_base_name`
* :func:`spec_pipeline.artifacts.source_fingerprint`
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

try:
    # Normal import path once the package's optional native deps (slither) are
    # installed.
    from spec_pipeline.artifacts import artifact_base_name, source_fingerprint
except Exception:  # pragma: no cover - fallback when spec_pipeline.__init__ deps absent
    # spec_pipeline/__init__.py eagerly imports the slither-backed stages, which
    # are unrelated to this pure module. Load artifacts.py directly so the two
    # functions under test can be verified without the external toolchain.
    _ARTIFACTS_PATH = (
        Path(__file__).resolve().parents[2] / "spec_pipeline" / "artifacts.py"
    )
    _MODNAME = "spec_pipeline_artifacts_under_test"
    _spec = importlib.util.spec_from_file_location(_MODNAME, _ARTIFACTS_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    # Register before exec so dataclasses defined in the module can resolve
    # their own module via sys.modules (module_from_spec alone does not
    # register it, which raised AttributeError once artifacts.py gained
    # dataclasses).
    sys.modules[_MODNAME] = _mod
    _spec.loader.exec_module(_mod)
    artifact_base_name = _mod.artifact_base_name
    source_fingerprint = _mod.source_fingerprint


# ---------------------------------------------------------------------------
# artifact_base_name  (Requirement 2.5)
# ---------------------------------------------------------------------------


def test_base_name_file_strips_sol_suffix():
    assert artifact_base_name(Path("/proj/contracts/Pool.sol")) == "Pool"


def test_base_name_file_without_sol_suffix_kept():
    # A file name that does not end in .sol is returned unchanged.
    assert artifact_base_name(Path("/proj/notes.txt")) == "notes.txt"


def test_base_name_only_trailing_sol_is_stripped():
    # ".sol" appearing mid-name must not be removed; only one trailing ".sol".
    assert artifact_base_name(Path("/proj/My.sol.bak")) == "My.sol.bak"
    assert artifact_base_name(Path("/proj/Token.sol")) == "Token"


def test_base_name_directory_returns_dir_name():
    assert artifact_base_name(Path("/proj/aave-v3-core")) == "aave-v3-core"


def test_base_name_read_equals_write_for_directory():
    # The whole point of the single function: same input -> same name, so the
    # writer and the loader never disagree (this is the bug 2.5 fixes).
    d = Path("/some/where/Certora_liquid-collective-protocol")
    assert artifact_base_name(d) == artifact_base_name(d)
    assert artifact_base_name(d) == "Certora_liquid-collective-protocol"


# ---------------------------------------------------------------------------
# source_fingerprint  (Requirement 3.2)
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_fingerprint_is_prefixed_hex_digest(tmp_path):
    f = _write(tmp_path, "A.sol", "contract A {}")
    fp = source_fingerprint(tmp_path, [f])
    assert fp.startswith("sha256:")
    hexpart = fp.split(":", 1)[1]
    assert len(hexpart) == 64
    int(hexpart, 16)  # must be valid hex


def test_fingerprint_stable_for_same_inputs(tmp_path):
    a = _write(tmp_path, "a/A.sol", "contract A {}")
    b = _write(tmp_path, "b/B.sol", "contract B {}")
    first = source_fingerprint(tmp_path, [a, b])
    second = source_fingerprint(tmp_path, [a, b])
    assert first == second


def test_fingerprint_order_independent(tmp_path):
    a = _write(tmp_path, "a/A.sol", "contract A {}")
    b = _write(tmp_path, "b/B.sol", "contract B {}")
    c = _write(tmp_path, "c/C.sol", "contract C {}")
    ordered = source_fingerprint(tmp_path, [a, b, c])
    shuffled = source_fingerprint(tmp_path, [c, a, b])
    assert ordered == shuffled


def test_fingerprint_sensitive_to_content_change(tmp_path):
    f = _write(tmp_path, "A.sol", "contract A {}")
    before = source_fingerprint(tmp_path, [f])
    f.write_text("contract A { uint256 x; }")
    after = source_fingerprint(tmp_path, [f])
    assert before != after


def test_fingerprint_sensitive_to_relative_path(tmp_path):
    # Same content but a different relative path must change the digest, because
    # the fingerprint hashes (relpath, content_digest) pairs.
    root_a = tmp_path / "one"
    root_b = tmp_path / "two"
    fa = _write(root_a, "src/A.sol", "contract A {}")
    fb = _write(root_b, "lib/A.sol", "contract A {}")
    assert source_fingerprint(root_a, [fa]) != source_fingerprint(root_b, [fb])


def test_fingerprint_empty_input_is_stable(tmp_path):
    assert source_fingerprint(tmp_path, []) == source_fingerprint(tmp_path, [])
    assert source_fingerprint(tmp_path, []).startswith("sha256:")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
