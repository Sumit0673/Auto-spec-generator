"""Unit tests for the Ground-Truth Pair_Index (Requirement 11).

These tests exercise :mod:`spec_pipeline.eval.pair_index` two ways:

* Against tiny temp-directory fixtures for the discrete behaviors - unpaired
  specs, ambiguous multi-``.sol`` matches, rule/invariant extraction,
  ``use rule`` resolution, unparseable specs, and the write/read round-trip.
* Against the REAL in-repository ``Paired_Dataset`` to assert the discovery
  count of 23 repositories and 56 pairs (Requirement 11.2). The dataset lives in
  the repo, so this needs no network and runs in the default suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spec_pipeline.eval import pair_index as pi
from spec_pipeline.eval.pair_index import (
    GroundTruthResult,
    PairEntry,
    PairIndex,
    UnpairedSpec,
    build_pair_index,
    extract_ground_truth,
    read_index,
    resolve_dataset_root,
    write_index,
)


# ---------------------------------------------------------------------------
# Temp-dir fixture helpers
# ---------------------------------------------------------------------------


def _make_repo(dataset_root: Path, repo: str) -> Path:
    repo_dir = dataset_root / repo
    repo_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Discovery: basic matching, ordering
# ---------------------------------------------------------------------------


def test_matches_spec_and_sol_by_base_name_at_any_depth(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    repo = _make_repo(ds, "org_repo")
    _write(repo / "src" / "deep" / "Vault.sol", "contract Vault {}")
    _write(repo / "certora" / "specs" / "Vault.spec", "rule r { assert true; }")

    index = build_pair_index(base=tmp_path)

    assert len(index.entries) == 1
    entry = index.entries[0]
    assert entry.repository == "org_repo"
    assert entry.contract_name == "Vault"
    assert entry.contract_path == "org_repo/src/deep/Vault.sol"
    assert entry.spec_path == "org_repo/certora/specs/Vault.spec"
    assert index.unpaired == []


def test_case_insensitive_base_name_match(tmp_path):
    # `pool.spec` <-> `Pool.sol` mirrors the real dataset convention.
    ds = tmp_path / "Paired_Dataset"
    repo = _make_repo(ds, "org_repo")
    _write(repo / "contracts" / "Pool.sol", "contract Pool {}")
    _write(repo / "specs" / "pool.spec", "rule r { assert true; }")

    index = build_pair_index(base=tmp_path)

    assert len(index.entries) == 1
    # contract_name preserves the .sol's true casing.
    assert index.entries[0].contract_name == "Pool"


def test_entries_ordered_by_repo_then_contract_path(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    repo_b = _make_repo(ds, "b_repo")
    repo_a = _make_repo(ds, "a_repo")
    _write(repo_b / "Z.sol", "contract Z {}")
    _write(repo_b / "Z.spec", "rule r { assert true; }")
    _write(repo_a / "src" / "B.sol", "contract B {}")
    _write(repo_a / "src" / "B.spec", "rule r { assert true; }")
    _write(repo_a / "src" / "A.sol", "contract A {}")
    _write(repo_a / "src" / "A.spec", "rule r { assert true; }")

    index = build_pair_index(base=tmp_path)

    ordered = [(e.repository, e.contract_path) for e in index.entries]
    assert ordered == sorted(ordered)
    assert ordered[0][0] == "a_repo"
    assert ordered[-1][0] == "b_repo"


def test_immediate_children_are_repositories(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    repo = _make_repo(ds, "only_repo")
    _write(repo / "A.sol", "contract A {}")
    _write(repo / "A.spec", "rule r { assert true; }")
    # A nested directory is NOT a repository of its own.
    _write(repo / "nested" / "B.sol", "contract B {}")
    _write(repo / "nested" / "B.spec", "rule r { assert true; }")

    index = build_pair_index(base=tmp_path)

    assert {e.repository for e in index.entries} == {"only_repo"}
    assert len(index.entries) == 2


# ---------------------------------------------------------------------------
# Unpaired handling (R11.3)
# ---------------------------------------------------------------------------


def test_unpaired_spec_recorded_with_reason(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    repo = _make_repo(ds, "org_repo")
    _write(repo / "Orphan.spec", "rule r { assert true; }")
    _write(repo / "Other.sol", "contract Other {}")

    index = build_pair_index(base=tmp_path)

    assert index.entries == []
    assert len(index.unpaired) == 1
    orphan = index.unpaired[0]
    assert isinstance(orphan, UnpairedSpec)
    assert orphan.contract_name == "Orphan"
    assert orphan.spec_path == "org_repo/Orphan.spec"
    assert "Orphan" in orphan.reason


def test_sol_in_different_repo_does_not_pair(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    repo_a = _make_repo(ds, "a")
    repo_b = _make_repo(ds, "b")
    _write(repo_a / "X.spec", "rule r { assert true; }")
    _write(repo_b / "X.sol", "contract X {}")

    index = build_pair_index(base=tmp_path)

    assert index.entries == []
    assert len(index.unpaired) == 1
    assert index.unpaired[0].repository == "a"


# ---------------------------------------------------------------------------
# Ambiguous multiple .sol matches (R11.8)
# ---------------------------------------------------------------------------


def test_ambiguous_matches_pick_first_lexicographic_record_rest(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    repo = _make_repo(ds, "org_repo")
    _write(repo / "b" / "Token.sol", "contract Token {}")
    _write(repo / "a" / "Token.sol", "contract Token {}")
    _write(repo / "c" / "Token.sol", "contract Token {}")
    _write(repo / "Token.spec", "rule r { assert true; }")

    index = build_pair_index(base=tmp_path)

    assert len(index.entries) == 1
    entry = index.entries[0]
    # First lexicographic wins.
    assert entry.contract_path == "org_repo/a/Token.sol"
    assert entry.rejected_contracts == (
        "org_repo/b/Token.sol",
        "org_repo/c/Token.sol",
    )


# ---------------------------------------------------------------------------
# Round-trip (R11.5)
# ---------------------------------------------------------------------------


def test_write_read_round_trip_equal(tmp_path):
    ds = tmp_path / "Paired_Dataset"
    repo = _make_repo(ds, "org_repo")
    _write(repo / "a" / "Token.sol", "contract Token {}")
    _write(repo / "b" / "Token.sol", "contract Token {}")
    _write(repo / "Token.spec", "rule r { assert true; }")
    _write(repo / "Orphan.spec", "rule r { assert true; }")

    index = build_pair_index(base=tmp_path)
    out = tmp_path / "index.json"
    write_index(index, out)
    reloaded = read_index(out)

    assert reloaded == index
    assert reloaded.entries == index.entries
    assert reloaded.unpaired == index.unpaired


def test_round_trip_of_hand_built_index(tmp_path):
    index = PairIndex(
        entries=[
            PairEntry(
                repository="r1",
                contract_path="r1/A.sol",
                spec_path="r1/A.spec",
                contract_name="A",
                rejected_contracts=("r1/dup/A.sol",),
            )
        ],
        unpaired=[
            UnpairedSpec(
                repository="r1",
                spec_path="r1/Orphan.spec",
                contract_name="Orphan",
                reason="no match",
            )
        ],
    )
    out = tmp_path / "idx.json"
    write_index(index, out)
    assert read_index(out) == index


# ---------------------------------------------------------------------------
# Ground-truth extraction (R11.6, R11.7, R11.9, R11.10)
# ---------------------------------------------------------------------------


def test_extract_rules_and_invariants_with_references(tmp_path):
    sol = _write(
        tmp_path / "Vault.sol",
        """
        contract Vault {
            uint256 public totalShares;
            function deposit(uint256 amount) external {}
            function withdraw(uint256 amount) external {}
        }
        """,
    )
    spec = _write(
        tmp_path / "Vault.spec",
        """
        methods { }

        rule depositIncreasesShares(uint256 amount) {
            uint256 before = totalShares;
            deposit(amount);
            assert totalShares >= before;
        }

        invariant sharesNonNegative()
            totalShares >= 0;
        """,
    )

    result = extract_ground_truth(spec, sol)

    assert result.parse_error is None
    assert result.count == 2
    by_name = {p.name: p for p in result.properties}
    assert by_name["depositIncreasesShares"].kind == "rule"
    assert "deposit" in by_name["depositIncreasesShares"].functions
    assert "totalShares" in by_name["depositIncreasesShares"].state_variables
    assert by_name["sharesNonNegative"].kind == "invariant"
    assert "totalShares" in by_name["sharesNonNegative"].state_variables


def test_declarations_inside_comments_are_ignored(tmp_path):
    sol = _write(tmp_path / "C.sol", "contract C { function f() external {} }")
    spec = _write(
        tmp_path / "C.spec",
        """
        // rule commentedOut { assert false; }
        /* rule alsoCommented { assert false; }
           invariant blockCommented() true; */
        rule realRule {
            f();
            assert true;
        }
        """,
    )

    result = extract_ground_truth(spec, sol)

    names = {p.name for p in result.properties}
    assert names == {"realRule"}
    assert result.count == 1


def test_spec_with_no_rule_or_invariant_counts_zero(tmp_path):
    sol = _write(tmp_path / "C.sol", "contract C {}")
    spec = _write(tmp_path / "C.spec", "methods { }\n")

    result = extract_ground_truth(spec, sol)

    assert result.parse_error is None
    assert result.count == 0
    assert result.properties == []


def test_use_rule_resolves_against_imported_spec_in_same_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sol = _write(repo / "C.sol", "contract C { function step() external {} }")
    _write(
        repo / "Base.spec",
        """
        rule sharedRule {
            step();
            assert true;
        }
        """,
    )
    spec = _write(
        repo / "C.spec",
        """
        import "Base.spec";
        use rule sharedRule;
        methods { }
        """,
    )

    result = extract_ground_truth(spec, sol, repo_dir=repo)

    assert result.parse_error is None
    assert result.unresolved_uses == []
    names = {p.name for p in result.properties}
    assert "sharedRule" in names
    assert result.count == 1
    shared = next(p for p in result.properties if p.name == "sharedRule")
    assert shared.origin == "Base"
    assert "step" in shared.functions


def test_unresolved_use_rule_recorded_and_excluded(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sol = _write(repo / "C.sol", "contract C {}")
    # Import target does not exist in the repo -> use stays unresolved.
    spec = _write(
        repo / "C.spec",
        """
        import "Missing.spec";
        use rule ghostRule;
        methods { }
        """,
    )

    result = extract_ground_truth(spec, sol, repo_dir=repo)

    assert result.parse_error is None
    assert result.count == 0
    assert result.unresolved_uses == ["rule ghostRule"]


def test_use_invariant_resolution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sol = _write(repo / "C.sol", "contract C { uint256 public x; }")
    _write(
        repo / "Base.spec",
        "invariant xBounded()\n    x >= 0;\n",
    )
    spec = _write(
        repo / "C.spec",
        'import "Base.spec";\nuse invariant xBounded;\nmethods { }\n',
    )

    result = extract_ground_truth(spec, sol, repo_dir=repo)

    assert result.unresolved_uses == []
    assert result.count == 1
    prop = result.properties[0]
    assert prop.kind == "invariant"
    assert prop.name == "xBounded"


def test_unparseable_spec_recorded_as_error_and_continues(tmp_path, monkeypatch):
    # A spec file whose bytes are not valid UTF-8 triggers a read/parse error.
    sol = _write(tmp_path / "C.sol", "contract C {}")
    spec = tmp_path / "C.spec"
    spec.write_bytes(b"\xff\xfe rule broken { \x80\x81 ")

    result = extract_ground_truth(spec, sol)

    assert result.parse_error is not None
    assert result.count == 0
    assert result.properties == []


def test_missing_sol_still_extracts_declarations(tmp_path):
    # A missing .sol yields no name matches but is not a spec parse error.
    spec = _write(
        tmp_path / "C.spec",
        "rule r {\n    doThing();\n    assert true;\n}\n",
    )
    result = extract_ground_truth(spec, tmp_path / "C.sol")

    assert result.parse_error is None
    assert result.count == 1
    # No .sol => no function references matched.
    assert result.properties[0].functions == ()


# ---------------------------------------------------------------------------
# REAL dataset discovery (R11.2) - runs in the default suite (in-repo, no net).
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_real_dataset_discovers_23_repos_and_56_pairs():
    dataset_root = resolve_dataset_root()
    if not dataset_root.exists():
        pytest.skip(f"Paired_Dataset not present at {dataset_root}")

    index = build_pair_index()

    repos = {e.repository for e in index.entries}
    assert len(repos) == 23, f"expected 23 repositories, got {len(repos)}: {sorted(repos)}"
    assert len(index.entries) == 56, (
        f"expected 56 pairs, got {len(index.entries)}"
    )
    # Every real .spec pairs with a .sol; none left unpaired.
    assert index.unpaired == []
    # Entries are ordered by repository then contract path.
    ordered = [(e.repository, e.contract_path) for e in index.entries]
    assert ordered == sorted(ordered)


@pytest.mark.slow
def test_real_dataset_ground_truth_use_rule_is_unresolved():
    # Firewall.spec `use rule method_reachability;` imports Sanity.spec, which is
    # not present in the dataset, so the use must be recorded as unresolved and
    # excluded from the count (R11.9, R11.10).
    dataset_root = resolve_dataset_root()
    if not dataset_root.exists():
        pytest.skip(f"Paired_Dataset not present at {dataset_root}")

    index = build_pair_index()
    firewall = next(
        (e for e in index.entries if e.contract_name == "Firewall"), None
    )
    assert firewall is not None
    result = extract_ground_truth(
        dataset_root / firewall.spec_path,
        dataset_root / firewall.contract_path,
        repo_dir=dataset_root / firewall.repository,
    )
    assert result.parse_error is None
    assert "rule method_reachability" in result.unresolved_uses
    assert result.count == 0


@pytest.mark.slow
def test_real_dataset_ground_truth_extracts_named_properties():
    # AToken.spec declares several rules and an invariant referencing mint/burn.
    dataset_root = resolve_dataset_root()
    if not dataset_root.exists():
        pytest.skip(f"Paired_Dataset not present at {dataset_root}")

    index = build_pair_index()
    atoken = next((e for e in index.entries if e.contract_name == "AToken"), None)
    assert atoken is not None
    result = extract_ground_truth(
        dataset_root / atoken.spec_path,
        dataset_root / atoken.contract_path,
        repo_dir=dataset_root / atoken.repository,
    )
    assert result.parse_error is None
    assert result.count > 0
    names = {p.name for p in result.properties}
    assert "permitIntegrity" in names
    kinds = {p.kind for p in result.properties}
    assert "rule" in kinds
