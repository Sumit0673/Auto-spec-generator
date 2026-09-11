"""Property tests for the Pair_Index round-trip (design Property 2).

**Property 2: Pair_Index round-trip**

Writing then reading a :class:`PairIndex` yields an equal index - equal entry
count, order, and fields (Requirement 11.5). We generate arbitrary indices with
hypothesis and assert ``read_index(write_index(idx)) == idx`` over at least 100
examples.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from spec_pipeline.eval.pair_index import (
    PairEntry,
    PairIndex,
    UnpairedSpec,
    read_index,
    write_index,
)

# Path-ish tokens: keep them JSON-safe and free of separators we do not need.
_names = st.text(
    alphabet=st.characters(
        min_codepoint=32,
        max_codepoint=0x2FFF,
        blacklist_characters='"\\\n\r\t/',
    ),
    min_size=1,
    max_size=12,
)

_paths = st.lists(_names, min_size=1, max_size=4).map(lambda parts: "/".join(parts))


@st.composite
def _pair_entries(draw):
    repo = draw(_names)
    contract = draw(_paths)
    spec = draw(_paths)
    name = draw(_names)
    rejected = tuple(draw(st.lists(_paths, max_size=3)))
    return PairEntry(
        repository=repo,
        contract_path=contract,
        spec_path=spec,
        contract_name=name,
        rejected_contracts=rejected,
    )


@st.composite
def _unpaired(draw):
    return UnpairedSpec(
        repository=draw(_names),
        spec_path=draw(_paths),
        contract_name=draw(_names),
        reason=draw(_names),
    )


@st.composite
def _pair_indices(draw):
    return PairIndex(
        entries=draw(st.lists(_pair_entries(), max_size=6)),
        unpaired=draw(st.lists(_unpaired(), max_size=4)),
    )


@given(index=_pair_indices())
def test_pair_index_write_read_round_trip(index, tmp_path_factory):
    out = tmp_path_factory.mktemp("pair_index") / "index.json"
    write_index(index, out)
    reloaded = read_index(out)

    assert reloaded == index
    assert len(reloaded.entries) == len(index.entries)
    assert [e.contract_path for e in reloaded.entries] == [
        e.contract_path for e in index.entries
    ]
    assert reloaded.unpaired == index.unpaired
