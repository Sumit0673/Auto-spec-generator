"""
Outcome_Set -> exit-code table (single source of truth) and secret redaction.

This module is the ONE place that maps every :data:`Outcome_Set` member to its
documented process exit code (design Data Models "Outcome_Set -> exit code",
Requirements 5.6, 20.10). The CLI, the README, and every terminal-state decision
funnel through :func:`exit_code_for` so the table can never drift between the
code and the documentation.

Design invariants realized here:

* Exit ``0`` is returned ONLY for ``verified``, ``verified_with_warnings``,
  ``skipped_missing_tool``, and ``typecheck_passed`` (the clean keyless local
  CVL typecheck that is the success terminal of ``--typecheck-only`` mode).
  Every other outcome maps to a distinct, documented nonzero code
  (Requirement 20.10).
* A truly unexpected condition maps to ``error`` (12); callers pass an unknown
  or ``None`` outcome to :func:`exit_code_for` and receive the ``error`` code
  rather than a spurious ``0``.
* Secrets are redacted at the sink (Requirement 21.2): :func:`redact_secrets`
  replaces the value of any environment variable whose name ends with
  ``_API_KEY``, ``_TOKEN``, or ``_SECRET`` with the literal ``REDACTED`` in any
  emitted text.

**Import safety.** This module is pure: it imports only the standard library and
declares no dependency on slither, the LLM stages, or any binary. It can be
loaded directly (via importlib) on a machine where slither is uninstallable,
which is what lets its unit tests run in the network-disabled, tool-absent test
environment.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Mapping, Optional

__all__ = [
    "Outcome",
    "OUTCOME_EXIT_CODES",
    "ZERO_EXIT_OUTCOMES",
    "ERROR_EXIT_CODE",
    "REDACTED",
    "SECRET_NAME_SUFFIXES",
    "exit_code_for",
    "redact_secrets",
]

# The literal written in place of a secret value (Requirement 21.2).
REDACTED = "REDACTED"

# Exit code for a truly unexpected/unhandled terminal state (Requirement 20.10,
# design Error Handling: an unrecognized ArtifactError etc. is caught as
# ``error``).
ERROR_EXIT_CODE = 12


class Outcome(str, Enum):
    """The enumerated run outcomes (glossary Outcome_Set).

    Subclassing ``str`` keeps the members interchangeable with the plain outcome
    strings the pipeline results and Verification_Status already carry, so
    callers may look up either ``Outcome.VIOLATED`` or the bare ``"violated"``.
    """

    VERIFIED = "verified"
    VERIFIED_WITH_WARNINGS = "verified_with_warnings"
    VIOLATED = "violated"
    VACUOUS = "vacuous"
    TYPECHECK_PASSED = "typecheck_passed"
    TYPECHECK_FAILED = "typecheck_failed"
    SETUP_FAILED = "setup_failed"
    COMPILE_FAILED = "compile_failed"
    NO_FIRST_PARTY_CONTRACTS = "no_first_party_contracts"
    NO_COMPATIBLE_SOLC = "no_compatible_solc"
    UNSUPPORTED_PRAGMA_SET = "unsupported_pragma_set"
    TOOL_UNAVAILABLE = "tool_unavailable"
    LLM_UNAVAILABLE = "llm_unavailable"
    SKIPPED_MISSING_TOOL = "skipped_missing_tool"
    TIMEOUT = "timeout"
    ERROR = "error"


# The single source of truth for the Outcome_Set -> exit-code table. Mirrors the
# design Data Models table and the README exactly (Requirements 5.6, 20.10).
OUTCOME_EXIT_CODES: dict[Outcome, int] = {
    Outcome.VERIFIED: 0,
    Outcome.VERIFIED_WITH_WARNINGS: 0,
    Outcome.VIOLATED: 1,
    Outcome.VACUOUS: 2,
    Outcome.NO_FIRST_PARTY_CONTRACTS: 4,
    Outcome.TOOL_UNAVAILABLE: 5,
    Outcome.TYPECHECK_FAILED: 6,
    Outcome.COMPILE_FAILED: 7,
    Outcome.NO_COMPATIBLE_SOLC: 8,
    Outcome.UNSUPPORTED_PRAGMA_SET: 9,
    Outcome.LLM_UNAVAILABLE: 10,
    Outcome.SKIPPED_MISSING_TOOL: 0,
    Outcome.TIMEOUT: 11,
    Outcome.ERROR: 12,
    # A clean keyless local CVL typecheck is a SUCCESS in ``--typecheck-only``
    # mode: the spec typechecks and the deferred cloud rule-proof was never
    # attempted. It maps to exit 0 so a typecheck-only run funnels through the
    # normal sink instead of the misleading generic ``error`` (12).
    Outcome.TYPECHECK_PASSED: 0,
    # certoraRun failed BEFORE the CVL typechecker (unknown --verify target,
    # compile error, bad argument): a distinct nonzero terminal, NOT a CVL
    # typecheck failure and NOT a pass.
    Outcome.SETUP_FAILED: 13,
}

# The only outcomes that map to a successful (zero) exit (Requirement 20.10).
ZERO_EXIT_OUTCOMES: frozenset[Outcome] = frozenset(
    outcome for outcome, code in OUTCOME_EXIT_CODES.items() if code == 0
)

# Environment-variable name suffixes whose values are secrets (Requirement 21.2).
SECRET_NAME_SUFFIXES: tuple[str, ...] = ("_API_KEY", "_TOKEN", "_SECRET")


def exit_code_for(outcome: Optional[object]) -> int:
    """Return the documented process exit code for *outcome*.

    Accepts an :class:`Outcome`, its bare string value (e.g. ``"violated"``), or
    an unknown/``None`` value. An unrecognized outcome maps to
    :data:`ERROR_EXIT_CODE` (12) so an unexpected terminal state can never leak a
    spurious ``0`` (Requirement 20.10).

    Args:
        outcome: An ``Outcome``, the equivalent string, or an unknown value.

    Returns:
        The exit code from :data:`OUTCOME_EXIT_CODES`, or 12 when the outcome is
        not a recognized Outcome_Set member.
    """
    if isinstance(outcome, Outcome):
        return OUTCOME_EXIT_CODES[outcome]
    if isinstance(outcome, str):
        try:
            return OUTCOME_EXIT_CODES[Outcome(outcome)]
        except ValueError:
            return ERROR_EXIT_CODE
    return ERROR_EXIT_CODE


def _secret_values(env: Mapping[str, str]) -> list[str]:
    """Return the non-empty values of every secret-named env var in *env*."""
    values: list[str] = []
    for name, value in env.items():
        if not value:
            continue
        if name.upper().endswith(SECRET_NAME_SUFFIXES):
            values.append(value)
    return values


def redact_secrets(text: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Replace secret env-var values in *text* with ``REDACTED`` (R21.2).

    For every environment variable whose name ends with ``_API_KEY``,
    ``_TOKEN``, or ``_SECRET`` (case-insensitive), any occurrence of its value in
    *text* is replaced with the literal ``REDACTED``. This is applied at the CLI
    sink before any results or errors are printed, so a secret pulled into an
    error message or a manifest string never reaches stdout/stderr.

    Longer secret values are redacted first so that a secret which contains
    another secret as a substring is fully masked.

    Args:
        text: The text about to be emitted.
        env: Environment mapping to read secret names/values from; defaults to
            :data:`os.environ`.

    Returns:
        *text* with every secret value replaced by ``REDACTED``. When *text* is
        not a string it is coerced with :func:`str` first.
    """
    if env is None:
        env = os.environ
    if not isinstance(text, str):
        text = str(text)
    # Redact longer values first to avoid a short secret unmasking a longer one.
    for value in sorted(_secret_values(env), key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    return text
