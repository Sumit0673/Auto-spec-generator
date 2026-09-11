"""Unit tests for the Outcome_Set -> exit-code table and secret redaction.

Task 7.1 (Requirements 20.6, 20.10, 21.2). These tests exercise the single
source of truth in :mod:`spec_pipeline.outcomes` and assert:

* ``exit_code_for`` returns the documented code for every one of the 14
  Outcome_Set members, and returns ``0`` only for the three zero-code outcomes
  (``verified``, ``verified_with_warnings``, ``skipped_missing_tool``);
* an unknown or ``None`` outcome maps to ``error`` (12);
* ``redact_secrets`` replaces the value of any ``_API_KEY`` / ``_TOKEN`` /
  ``_SECRET`` env var with ``REDACTED`` in a sample string.

``spec_pipeline.outcomes`` is pure (stdlib only), so it imports directly even
where slither is uninstallable; a defensive importlib fallback mirrors the other
unit tests in case the package ``__init__`` ever changes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

try:  # Pure module: normally importable directly.
    from spec_pipeline.outcomes import (  # type: ignore
        ERROR_EXIT_CODE,
        OUTCOME_EXIT_CODES,
        REDACTED,
        ZERO_EXIT_OUTCOMES,
        Outcome,
        exit_code_for,
        redact_secrets,
    )
except Exception:  # pragma: no cover - defensive: load the pure module directly
    _OUTCOMES_PATH = (
        Path(__file__).resolve().parents[2] / "spec_pipeline" / "outcomes.py"
    )
    _MODNAME = "spec_pipeline_outcomes_under_test"
    _spec = importlib.util.spec_from_file_location(_MODNAME, _OUTCOMES_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_MODNAME] = _mod
    _spec.loader.exec_module(_mod)
    ERROR_EXIT_CODE = _mod.ERROR_EXIT_CODE
    OUTCOME_EXIT_CODES = _mod.OUTCOME_EXIT_CODES
    REDACTED = _mod.REDACTED
    ZERO_EXIT_OUTCOMES = _mod.ZERO_EXIT_OUTCOMES
    Outcome = _mod.Outcome
    exit_code_for = _mod.exit_code_for
    redact_secrets = _mod.redact_secrets


# The documented Outcome_Set -> exit-code table (design Data Models; README).
# Duplicated here on purpose so the test fails if the table in the module drifts.
_EXPECTED: dict[str, int] = {
    "verified": 0,
    "verified_with_warnings": 0,
    "violated": 1,
    "vacuous": 2,
    "no_first_party_contracts": 4,
    "tool_unavailable": 5,
    "typecheck_failed": 6,
    "compile_failed": 7,
    "no_compatible_solc": 8,
    "unsupported_pragma_set": 9,
    "llm_unavailable": 10,
    "skipped_missing_tool": 0,
    "timeout": 11,
    "error": 12,
    "typecheck_passed": 0,
    "setup_failed": 13,
}


def test_table_covers_exactly_the_outcomes():
    """All Outcome_Set members are present and no extras (Requirement 20.10)."""
    assert set(o.value for o in Outcome) == set(_EXPECTED)
    assert len(OUTCOME_EXIT_CODES) == len(_EXPECTED)


@pytest.mark.parametrize("name,code", sorted(_EXPECTED.items()))
def test_exit_code_for_enum_member(name, code):
    """Each Outcome enum member maps to its documented code (Requirement 20.10)."""
    assert exit_code_for(Outcome(name)) == code


@pytest.mark.parametrize("name,code", sorted(_EXPECTED.items()))
def test_exit_code_for_bare_string(name, code):
    """The bare outcome string maps to the same code as the enum member."""
    assert exit_code_for(name) == code


def test_only_success_outcomes_map_to_zero():
    """Exit 0 occurs ONLY for the zero-code outcomes (Requirement 20.10).

    ``typecheck_passed`` joins the success set: a clean keyless local CVL
    typecheck in ``--typecheck-only`` mode is a success (the deferred cloud
    rule-proof is never attempted).
    """
    zero = {name for name, code in _EXPECTED.items() if code == 0}
    assert zero == {
        "verified",
        "verified_with_warnings",
        "skipped_missing_tool",
        "typecheck_passed",
    }
    assert {o.value for o in ZERO_EXIT_OUTCOMES} == zero
    # And no other outcome sneaks in a zero.
    for name, code in _EXPECTED.items():
        if name not in zero:
            assert exit_code_for(name) != 0


def test_every_nonzero_code_is_distinct():
    """Every non-success outcome has a distinct exit code (Requirement 20.10)."""
    nonzero = [code for name, code in _EXPECTED.items() if code != 0]
    assert len(nonzero) == len(set(nonzero))


def test_unknown_outcome_maps_to_error():
    """An unrecognized outcome string maps to ``error`` (12), never 0."""
    assert exit_code_for("definitely_not_an_outcome") == ERROR_EXIT_CODE == 12


def test_none_outcome_maps_to_error():
    """A ``None`` outcome (no terminal state) maps to ``error`` (12)."""
    assert exit_code_for(None) == 12


def test_non_string_outcome_maps_to_error():
    """A non-string, non-Outcome value maps to ``error`` (12)."""
    assert exit_code_for(object()) == 12
    assert exit_code_for(42) == 12


# ---------------------------------------------------------------------------
# Secret redaction (Requirement 21.2)
# ---------------------------------------------------------------------------


def test_redacts_api_key_token_and_secret_values():
    """A fake _API_KEY/_TOKEN/_SECRET value is replaced with REDACTED (R21.2)."""
    env = {
        "LLM_API_KEY": "sk-fake-key-abc123",
        "GITHUB_TOKEN": "ghp_faketoken789",
        "SOME_SECRET": "topsecretvalue",
        "LLM_MODEL": "auto",  # not a secret; must survive verbatim
    }
    text = (
        "calling https://host with key sk-fake-key-abc123 and "
        "token ghp_faketoken789 and secret topsecretvalue, model auto"
    )
    out = redact_secrets(text, env)
    assert "sk-fake-key-abc123" not in out
    assert "ghp_faketoken789" not in out
    assert "topsecretvalue" not in out
    assert out.count(REDACTED) == 3
    # Non-secret values are untouched.
    assert "model auto" in out
    assert "https://host" in out


def test_redaction_is_case_insensitive_on_suffix():
    """Suffix match is case-insensitive on the env var name (Requirement 21.2)."""
    env = {"my_api_key": "lower-secret"}
    assert redact_secrets("value=lower-secret", env) == "value=REDACTED"


def test_redaction_leaves_text_without_secrets_unchanged():
    """Text with no secret values is returned verbatim."""
    env = {"LLM_API_KEY": "sk-fake"}
    assert redact_secrets("nothing sensitive here", env) == "nothing sensitive here"


def test_redaction_ignores_empty_secret_values():
    """An empty secret value never blanks unrelated text (no empty-string replace)."""
    env = {"LLM_API_KEY": ""}
    assert redact_secrets("abc", env) == "abc"


def test_redaction_coerces_non_string_input():
    """A non-string argument is coerced to str before redaction."""
    env = {"X_TOKEN": "tok"}
    assert redact_secrets(12345, env) == "12345"


def test_longer_secret_redacted_before_shorter_substring():
    """A secret containing another secret as a substring is fully masked."""
    env = {"A_TOKEN": "abc", "B_SECRET": "abcdef"}
    # If "abc" were replaced first, "abcdef" would become "REDACTEDdef".
    out = redact_secrets("value=abcdef", env)
    assert out == "value=REDACTED"
