"""CVL_Extractor + CVL_Validator (Requirement 18).

This module is the single, non-destructive replacement for the two drifted
``_extract_cvl`` copies that lived in ``spec_pipeline.stage3_rules`` and
``spec_pipeline.stage3_iterative``. Those copies performed semantics-changing
regex rewrites on the model's CVL output; every one of them is deleted here:

* ``assume x <= y``      -> ``assume x < y + 1``   (changed the assertion)
* ``!= 0``               -> ``> 0``                (changed the assertion)
* ``!= address(0)``      -> ``> address(0)``       (changed the assertion)
* deletion of whole ``invariant`` declarations
* deletion of tuple-comparison ``invariant`` declarations (iterative copy)
* stripping of ``using`` lines
* ``!hasRole`` -> ``not hasRole`` rewrites

The new contract (R18.4, R18.5): extraction preserves ``using`` declarations,
``assume`` / ``require`` statements, ``invariant`` declarations, comparison
operators, and negation *exactly* as the model produced them. Problems are
reported as :class:`CVLDiagnostic` findings by :func:`validate_cvl` rather than
silently rewritten, and only the four explicitly allow-listed, semantics
preserving rewrites are applied by :func:`autofix_cvl` (and only under
``--autofix-cvl``).

Design note on imports: this module is pure regex/text handling and imports
nothing from the pipeline (no ``stage1_extract`` / slither chain), so it is
import-safe on a machine without slither.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# CVL declaration vocabulary (R18.12)
# ---------------------------------------------------------------------------

# Whole-word tokens that mark a fenced block (or document) as CVL. Matched as
# whole words so an identifier such as ``ruleset`` or ``functional`` does not
# count as a declaration keyword.
CVL_DECLARATION_KEYWORDS = (
    "rule",
    "invariant",
    "methods",
    "hook",
    "ghost",
    "using",
    "definition",
    "function",
    "import",
    "use",
)

_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(CVL_DECLARATION_KEYWORDS) + r")\b"
)

# A fenced block: ```<optional info string>\n<body>```
# Group "info" holds the language/info string, group "body" the block content.
_FENCE_RE = re.compile(
    r"```[ \t]*(?P<info>[^\n`]*)\n(?P<body>.*?)```",
    re.DOTALL,
)


def _contains_cvl_keyword(text: str) -> bool:
    """Return True when *text* holds at least one CVL declaration keyword."""
    return _KEYWORD_RE.search(text) is not None


# ---------------------------------------------------------------------------
# CVL_Extractor (R18.2, R18.3, R18.4, R18.6, R18.7, R18.13)
# ---------------------------------------------------------------------------


def extract_cvl(response: str, methods_block: Optional[str] = None) -> str:
    """Recover CVL text from an LLM *response* without altering its semantics.

    Selection order (R18.2, R18.3):

    1. The body of the first ```cvl fenced block, if any.
    2. Otherwise the body of the first fenced block whose text contains a CVL
       declaration keyword.
    3. Otherwise the whole response.

    Only surrounding whitespace is stripped; every remaining character is kept
    verbatim (R18.2-R18.4). When *methods_block* is supplied it is prepended,
    separated by a blank line (R18.6, R18.13).

    Idempotence (R18.7): with no ``methods_block``, ``extract_cvl`` of an
    already-extracted document returns that document unchanged, because the
    document has no surrounding fences to re-parse and stripping is a no-op on
    already-stripped text.
    """
    extracted = _select_cvl_body(response)
    extracted = extracted.strip()

    if methods_block is not None:
        return methods_block + "\n\n" + extracted
    return extracted


def _select_cvl_body(response: str) -> str:
    """Return the raw (unstripped) CVL body per the R18.2/R18.3 selection order."""
    first_generic_body: Optional[str] = None
    first_keyword_body: Optional[str] = None

    for match in _FENCE_RE.finditer(response):
        info = match.group("info").strip().lower()
        body = match.group("body")

        # 1. First ```cvl fence wins outright.
        if info == "cvl":
            return body

        if first_generic_body is None:
            first_generic_body = body

        # 2. Remember the first fence that contains a CVL keyword.
        if first_keyword_body is None and _contains_cvl_keyword(body):
            first_keyword_body = body

    if first_keyword_body is not None:
        return first_keyword_body

    # 3. No fence held a CVL keyword: fall back to the whole response.
    return response


# ---------------------------------------------------------------------------
# CVL_Validator (R18.8, R18.14)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CVLDiagnostic:
    """One machine-readable CVL finding.

    Attributes:
        category: A stable category token, e.g. ``duplicated_methods_block``.
        line: The 1-based line number the finding refers to.
        message: A human-readable description.
    """

    category: str
    line: int
    message: str


_PRAGMA_RE = re.compile(r"^\s*pragma\b", re.IGNORECASE)
_LICENSE_RE = re.compile(r"//\s*SPDX-License-Identifier", re.IGNORECASE)
_HOOK_ASSIGN_RE = re.compile(r":=")
_METHODS_OPEN_RE = re.compile(r"\bmethods\s*\{")


def _normalize_methods_block(block: str) -> str:
    """Collapse whitespace so two methods blocks compare on content, not layout."""
    return re.sub(r"\s+", " ", block).strip()


def _find_methods_blocks(text: str) -> list[tuple[int, str]]:
    """Return ``(start_line, block_text)`` for each brace-balanced methods block."""
    results: list[tuple[int, str]] = []
    for m in _METHODS_OPEN_RE.finditer(text):
        brace_start = text.index("{", m.start())
        depth = 0
        end = None
        for i in range(brace_start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            end = len(text)
        block = text[m.start():end]
        line = text.count("\n", 0, m.start()) + 1
        results.append((line, block))
    return results


def validate_cvl(
    text: str,
    generated_methods_block: Optional[str] = None,
) -> list[CVLDiagnostic]:
    """Report CVL problems in *text* without changing it (R18.8, R18.14).

    Emits one :class:`CVLDiagnostic` for each of:

    * every EXTRA ``methods {`` block when the text holds two or more of them
      (category ``duplicated_methods_block``); the first block is kept and each
      2nd+ block is flagged on its own start line. A single methods block --
      matching the generated one or not -- is correct and is never flagged.
      ``generated_methods_block`` is accepted for the autofix but does not gate
      this count-based diagnostic;
    * a ``:=`` hook assignment (category ``hook_assignment``);
    * a Solidity ``pragma`` line (category ``solidity_pragma``);
    * a license identifier line (category ``license_identifier``);
    * an extracted document that holds no CVL declaration keyword at all
      (category ``no_cvl``), emitted once at line 1.

    No diagnostic is emitted for ``assume`` / ``require`` statements, comparison
    operators, ``using`` declarations, or ``invariant`` declarations: those are
    preserved verbatim and are not defects (R18.8).

    The validator NEVER mutates *text*; without ``--autofix-cvl`` the caller
    reports these diagnostics and leaves the document untouched (R18.14).
    """
    diagnostics: list[CVLDiagnostic] = []
    lines = text.splitlines()

    # No-CVL document (R18.8): whole extracted text holds no declaration keyword.
    if not _contains_cvl_keyword(text):
        diagnostics.append(
            CVLDiagnostic(
                category="no_cvl",
                line=1,
                message="Extracted document contains no CVL declaration keyword.",
            )
        )

    # Duplicated methods block (R18.8): a spec with 2 OR MORE methods blocks is
    # genuinely duplicated. The FIRST block is the legitimate one; every extra
    # (2nd+) block is a duplicate and is flagged on its own start line. A single
    # methods block -- whether or not it matches the generated one -- is the
    # normal, correct case and is NEVER flagged. Detection is count-based and
    # does not depend on ``generated_methods_block``.
    methods_blocks = _find_methods_blocks(text)
    if len(methods_blocks) >= 2:
        for start_line, _block in methods_blocks[1:]:
            diagnostics.append(
                CVLDiagnostic(
                    category="duplicated_methods_block",
                    line=start_line,
                    message=(
                        "Methods block duplicates the generated methods "
                        "block."
                    ),
                )
            )

    # Per-line diagnostics.
    for idx, line in enumerate(lines, start=1):
        if _HOOK_ASSIGN_RE.search(line):
            diagnostics.append(
                CVLDiagnostic(
                    category="hook_assignment",
                    line=idx,
                    message="Hook assignment uses ':=' instead of '='.",
                )
            )
        if _PRAGMA_RE.match(line):
            diagnostics.append(
                CVLDiagnostic(
                    category="solidity_pragma",
                    line=idx,
                    message="Solidity pragma line is not valid CVL.",
                )
            )
        if _LICENSE_RE.search(line):
            diagnostics.append(
                CVLDiagnostic(
                    category="license_identifier",
                    line=idx,
                    message="License identifier line is not valid CVL.",
                )
            )

    diagnostics.sort(key=lambda d: (d.line, d.category))
    return diagnostics


# ---------------------------------------------------------------------------
# Autofix_Allowlist (R18.10, R18.11)
# ---------------------------------------------------------------------------


def autofix_cvl(
    text: str,
    generated_methods_block: Optional[str] = None,
) -> tuple[str, list[tuple[int, str]]]:
    """Apply ONLY the four allow-listed, semantics-preserving CVL rewrites.

    Returns ``(fixed_text, applied)`` where *applied* is a list of
    ``(line, rule)`` pairs, one per rewrite performed. The allow-list (R18.10):

    1. ``remove_pragma``       - remove Solidity ``pragma`` lines.
    2. ``remove_license``      - remove license identifier lines.
    3. ``remove_methods_block``- remove DUPLICATE methods blocks (the 2nd+),
       keeping the first one, when two or more methods blocks are present. A
       lone methods block is diagnostic-free and is never removed.
    4. ``fix_hook_assignment`` - replace ``:=`` with ``=`` inside a hook body.

    Every other line is returned unchanged, character for character (R18.11).
    Line numbers in *applied* refer to the ORIGINAL 1-based line numbering.
    """
    applied: list[tuple[int, str]] = []

    # 3. Remove DUPLICATE methods blocks first (a block may span many lines).
    #    Consistent with ``validate_cvl``: removal is COUNT-based -- only when
    #    the text holds 2 OR MORE methods blocks. The FIRST block is the
    #    legitimate one and is kept; each subsequent (2nd, 3rd, ...) block is a
    #    duplicate and its exact line span is dropped. With 0 or 1 methods block
    #    this rule does nothing (an autofix never removes a diagnostic-free
    #    construct). ``generated_methods_block`` does not gate this logic.
    removed_methods_line_spans: set[int] = set()
    methods_blocks = _find_methods_blocks(text)
    if len(methods_blocks) >= 2:
        for m in list(_METHODS_OPEN_RE.finditer(text))[1:]:
            brace_start = text.index("{", m.start())
            depth = 0
            end = None
            for i in range(brace_start, len(text)):
                ch = text[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end is None:
                end = len(text)
            start_line = text.count("\n", 0, m.start()) + 1
            end_line = text.count("\n", 0, end) + 1
            for ln in range(start_line, end_line + 1):
                removed_methods_line_spans.add(ln)
            applied.append((start_line, "remove_methods_block"))

    out_lines: list[str] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        # 3. Drop lines belonging to a removed duplicated methods block.
        if idx in removed_methods_line_spans:
            continue

        # 1. Remove Solidity pragma lines.
        if _PRAGMA_RE.match(line):
            applied.append((idx, "remove_pragma"))
            continue

        # 2. Remove license identifier lines.
        if _LICENSE_RE.search(line):
            applied.append((idx, "remove_license"))
            continue

        # 4. Replace ':=' with '=' inside a hook body.
        if _HOOK_ASSIGN_RE.search(line):
            line = line.replace(":=", "=")
            applied.append((idx, "fix_hook_assignment"))

        out_lines.append(line)

    applied.sort(key=lambda pair: (pair[0], pair[1]))

    # Preserve a trailing newline if the input had one.
    fixed = "\n".join(out_lines)
    if text.endswith("\n") and not fixed.endswith("\n"):
        fixed += "\n"
    return fixed, applied
