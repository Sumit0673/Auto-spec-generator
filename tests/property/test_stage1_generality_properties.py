"""Property-based tests for the two Stage 1 generality metamorphic properties
(design Properties 13 & 14, Requirements 19.7 & 19.8).

* **Property 13 - dependency-file addition metamorphic (R19.7):** adding a
  contract file UNDER A DEPENDENCY ROOT leaves the Stage 1 table unchanged.
* **Property 14 - file-subset monotonicity metamorphic (R19.8):** analyzing a
  SUBSET of the first-party files yields a Stage 1 table whose contract set is a
  subset of the table produced from all files.

Offline design
--------------
slither / solc / crytic-compile are all absent here (the session conftest also
strips ``solc``/``certoraRun`` from PATH and blocks the network), so these
properties are exercised at the *extraction / classification helper* level, not
by compiling Solidity.

``spec_pipeline/__init__.py`` eagerly imports the slither-backed stages, and
``stage1_extract.py`` imports ``solidity_graph.analyzer`` at module top, so we:

1. install slither-free stubs for ``solidity_graph.analyzer`` and the parent
   ``solidity_graph`` / ``spec_pipeline`` packages in ``sys.modules`` BEFORE
   loading anything, then
2. load ``spec_pipeline.stage1_extract`` by file path via importlib.

``resolve.py`` is pure (no slither import) and is imported lazily inside
``_identify_first_party``; it walks the real filesystem for ``node_modules`` /
``lib`` roots, so every synthetic project is materialised under a ``tmp_path``
with an explicit ``node_modules`` dependency root, and the extractor is driven
against a duck-typed slither ``graph`` whose ``ContractInfo`` stand-ins carry
only the attributes the real production helpers read (``name``, ``kind``,
``source_file``, ``functions``, ``state_variables``). No real analyzer runs: we
replicate the exact build phase of ``FirstPartyExtractor.analyze`` (identify ->
sorted build) so the production ``_identify_first_party``,
``_build_first_party_contract`` and ``_ordered_contract`` are the code under
test.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Offline stubs + module load (mirrors test_pipeline_zero_contract.py)
# ---------------------------------------------------------------------------


def _install_stub_packages() -> None:
    """Register slither-free stubs so ``spec_pipeline.stage1_extract`` loads offline."""
    if "solidity_graph.analyzer" not in sys.modules:
        analyzer = types.ModuleType("solidity_graph.analyzer")
        analyzer.SolidityAnalyzer = object
        analyzer.SolidityGraph = object
        analyzer.ContractInfo = object
        analyzer.FunctionNode = object
        analyzer._find_solc = lambda *a, **k: None
        analyzer._SHARED_DEPS = Path("/nonexistent-shared-deps")
        analyzer._build_solc_remaps = lambda *a, **k: []
        sys.modules["solidity_graph.analyzer"] = analyzer

    if "solidity_graph" not in sys.modules or not hasattr(
        sys.modules["solidity_graph"], "__path__"
    ):
        sg = types.ModuleType("solidity_graph")
        sg.__path__ = [str(_REPO_ROOT / "solidity_graph")]
        sg.analyzer = sys.modules["solidity_graph.analyzer"]
        sys.modules["solidity_graph"] = sg

    if "spec_pipeline" not in sys.modules or not hasattr(
        sys.modules["spec_pipeline"], "__path__"
    ):
        sp = types.ModuleType("spec_pipeline")
        sp.__path__ = [str(_REPO_ROOT / "spec_pipeline")]
        sys.modules["spec_pipeline"] = sp

    # resolve.py is pure (no slither). Ensure the real module is importable as
    # ``spec_pipeline.resolve`` (the extractor imports it lazily by that name).
    if "spec_pipeline.resolve" not in sys.modules:
        _load_module_by_path("spec_pipeline.resolve", "spec_pipeline/resolve.py")


def _load_module_by_path(mod_name: str, rel_path: str):
    path = _REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_install_stub_packages()

if "spec_pipeline.stage1_extract" not in sys.modules:
    _stage1 = _load_module_by_path(
        "spec_pipeline.stage1_extract", "spec_pipeline/stage1_extract.py"
    )
else:  # pragma: no cover - depends on collection order
    _stage1 = sys.modules["spec_pipeline.stage1_extract"]

FirstPartyExtractor = _stage1.FirstPartyExtractor
Stage1Table = _stage1.Stage1Table


# ---------------------------------------------------------------------------
# Duck-typed slither stand-ins (only the attributes production helpers read)
# ---------------------------------------------------------------------------


@dataclass
class _Func:
    """Stand-in for solidity_graph FunctionNode (minimal read surface)."""

    name: str
    visibility: str = "external"
    mutability: str = "nonpayable"
    parameters: list = field(default_factory=list)
    return_types: list = field(default_factory=list)
    modifiers: list = field(default_factory=list)
    state_vars_written: list = field(default_factory=list)
    state_vars_read: list = field(default_factory=list)
    internal_calls: list = field(default_factory=list)
    external_calls: list = field(default_factory=list)
    is_constructor: bool = False
    is_fallback: bool = False
    is_receive: bool = False


@dataclass
class _ContractInfo:
    """Stand-in for solidity_graph ContractInfo (minimal read surface)."""

    name: str
    kind: str
    source_file: str
    functions: list = field(default_factory=list)
    state_variables: list = field(default_factory=list)


class _Graph:
    """Stand-in for SolidityGraph: only ``.contracts`` (name -> ContractInfo)."""

    def __init__(self, contracts: dict):
        self.contracts = contracts


def _build_table_from_graph(project_path: Path, graph: _Graph) -> Stage1Table:
    """Replicate ``FirstPartyExtractor.analyze``'s build phase against a synthetic
    graph, so the real production classification / build helpers are exercised
    without invoking slither.

    This performs exactly the identify -> sorted-build sequence of ``analyze``:
    it sets ``extractor.graph``, runs the real ``_identify_first_party`` (which
    lazily imports the real ``Dependency_Resolver`` and walks the filesystem for
    dependency roots), then builds the table in sorted first-party-name order via
    the real ``_build_first_party_contract`` + ``_ordered_contract``.

    ``FirstPartyExtractor.__init__`` constructs a ``SolidityAnalyzer`` (stubbed
    to bare ``object`` for offline loading, which is not constructible), so the
    instance is created via ``__new__`` and its ``__init__``-established
    attributes are set by hand - none of them touch slither.
    """
    extractor = FirstPartyExtractor.__new__(FirstPartyExtractor)
    extractor.project_path = Path(project_path).resolve()
    extractor.analyzer = None
    extractor.first_party_names = set()
    extractor.contract_to_file = {}
    extractor.graph = graph
    extractor._identify_first_party()

    table = Stage1Table(project_path=str(extractor.project_path))
    for cname in sorted(extractor.first_party_names):
        if cname in graph.contracts:
            cinfo = graph.contracts[cname]
            fpc = _stage1._ordered_contract(
                extractor._build_first_party_contract(cinfo)
            )
            table.contracts[cname] = fpc
    return table


# ---------------------------------------------------------------------------
# Project materialisation on disk
# ---------------------------------------------------------------------------
#
# The Dependency_Resolver walks the real filesystem for ``node_modules`` / ``lib``
# directories, so a synthetic project must exist on disk with a real dependency
# root for a source file placed under it to be classified as dependency scope.
# We materialise: <root>/foundry.toml (project marker), <root>/src/*.sol for
# first-party contracts, and <root>/node_modules/... for dependency contracts.


def _materialise_project(root: Path, first_party_files, dep_files) -> None:
    """Create the project skeleton and empty ``.sol`` files on disk.

    ``first_party_files`` / ``dep_files`` are iterables of paths RELATIVE to
    ``root``; the files' contents are irrelevant (slither never runs) - only
    their locations drive the path-based first-party classification.
    """
    root.mkdir(parents=True, exist_ok=True)
    # A build marker so Project/Dependency resolution treats ``root`` as the
    # project root; and a real node_modules dir so it is a resolvable dep root.
    (root / "foundry.toml").write_text("[profile.default]\n", encoding="utf-8")
    (root / "node_modules").mkdir(exist_ok=True)
    for rel in list(first_party_files) + list(dep_files):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("// synthetic\n", encoding="utf-8")


def _contract_info(name: str, source_file: Path) -> _ContractInfo:
    """A minimal but non-trivial ContractInfo: one gated fn + one state var, so
    ``_build_first_party_contract`` exercises real gate/state-var/edge building
    rather than an all-empty contract."""
    fn = _Func(
        name="set" + name,
        visibility="external",
        mutability="nonpayable",
        parameters=[{"type": "uint256", "name": "x"}],
        return_types=[],
        modifiers=["onlyOwner"],
        state_vars_written=["value"],
    )
    return _ContractInfo(
        name=name,
        kind="contract",
        source_file=str(source_file),
        functions=[fn],
        state_variables=[
            {
                "name": "value",
                "type": "uint256",
                "visibility": "public",
                "is_constant": False,
                "is_immutable": False,
            }
        ],
    )


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# Contract / identifier names: cover non-ASCII, length-1 identifiers and a
# broader latin range (R23.1). Kept to valid-ish Solidity identifier characters
# so the file names are writable on disk.
_NAME = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll"),
        min_codepoint=ord("A"),
        max_codepoint=0x24F,  # through Latin Extended-B, includes non-ASCII
    ),
    min_size=1,
    max_size=8,
).filter(lambda s: s.strip() == s and "/" not in s and "\\" not in s)


@st.composite
def _first_party_contracts(draw):
    """A non-empty set of distinctly-named first-party contracts, each in its
    own ``src/<Name>.sol`` file. Returns ``{name: relpath}``."""
    names = draw(st.lists(_NAME, min_size=1, max_size=5, unique=True))
    return {name: Path("src") / f"{name}.sol" for name in names}


# ---------------------------------------------------------------------------
# Property 13 - dependency-file addition metamorphic (R19.7)
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    fp=_first_party_contracts(),
    dep_name=_NAME,
    dep_rel=st.sampled_from(
        [
            Path("node_modules") / "dep.sol",
            Path("node_modules") / "@scope" / "pkg" / "lib.sol",
            Path("node_modules") / "openzeppelin" / "ERC20.sol",
        ]
    ),
)
def test_dependency_file_addition_leaves_table_unchanged(tmp_path_factory, fp, dep_name, dep_rel):
    """Adding a contract file under a dependency root leaves the Stage 1 table
    byte-identical (Property 13, R19.7)."""
    root = tmp_path_factory.mktemp("proj13")

    # The dependency contract's name must not collide with a first-party name
    # (a collision is a different scenario: same name, different scope).
    if dep_name in fp:
        dep_name = dep_name + "_dep"

    _materialise_project(root, fp.values(), [dep_rel])

    # Baseline graph: first-party contracts only.
    base_contracts = {
        name: _contract_info(name, root / rel) for name, rel in fp.items()
    }
    base_table = _build_table_from_graph(root, _Graph(dict(base_contracts)))

    # Augmented graph: identical, plus one contract whose source file lives under
    # the node_modules dependency root.
    aug_contracts = dict(base_contracts)
    aug_contracts[dep_name] = _contract_info(dep_name, root / dep_rel)
    aug_table = _build_table_from_graph(root, _Graph(aug_contracts))

    # The dependency contract must have been filtered out entirely...
    assert dep_name not in aug_table.contracts
    # ...and the table (contract set AND serialized content) is unchanged.
    assert set(aug_table.contracts) == set(base_table.contracts)
    assert aug_table.to_json() == base_table.to_json()
    assert aug_table.to_text() == base_table.to_text()


# ---------------------------------------------------------------------------
# Property 14 - file-subset monotonicity metamorphic (R19.8)
# ---------------------------------------------------------------------------


@st.composite
def _fp_and_subset(draw):
    fp = draw(_first_party_contracts())
    names = list(fp)
    # Choose a subset (possibly empty, possibly the whole set) of the files to
    # analyze in the restricted run.
    keep_flags = draw(
        st.lists(st.booleans(), min_size=len(names), max_size=len(names))
    )
    subset = {n: fp[n] for n, keep in zip(names, keep_flags) if keep}
    return fp, subset


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(data=_fp_and_subset())
def test_file_subset_yields_subset_contract_set(tmp_path_factory, data):
    """Analyzing a subset of the first-party files yields a contract set that is
    a subset of the full analysis, and each shared contract is identical
    (Property 14, R19.8)."""
    fp, subset = data
    root = tmp_path_factory.mktemp("proj14")
    _materialise_project(root, fp.values(), [])

    full_contracts = {
        name: _contract_info(name, root / rel) for name, rel in fp.items()
    }
    full_table = _build_table_from_graph(root, _Graph(dict(full_contracts)))

    subset_contracts = {
        name: _contract_info(name, root / rel) for name, rel in subset.items()
    }
    subset_table = _build_table_from_graph(root, _Graph(subset_contracts))

    # Monotonicity: the subset's contract set is a subset of the full set.
    assert set(subset_table.contracts) <= set(full_table.contracts)
    # And restricting to exactly the analyzed files loses no analyzed contract.
    assert set(subset_table.contracts) == set(subset)
    # Each contract present in both renders identically (subset analysis does
    # not perturb a contract that survives into it).
    for name in subset_table.contracts:
        assert _stage1.asdict(subset_table.contracts[name]) == _stage1.asdict(
            full_table.contracts[name]
        )


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
