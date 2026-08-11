"""Persistent error-memory store — SQLite-backed, keyed by contract hash.

Survives across CLI invocations so run N+1 never repeats run N's mistakes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

# ── Error normalization ──────────────────────────────────────────────────────
# Strip position/name specifics so the same mistake on a different line, rule,
# or identifier still collapses to ONE canonical key. Without this, lint
# messages that embed the offending rule name or code line are stored as
# distinct errors and recurrence is never recognized.

_LINE_REF = re.compile(r"\bline \d+\b", re.I)
# "Line 189 `invariant foo() {` -> Error: msg"  →  "msg"
_CERTORA_LINE_PREFIX = re.compile(r"^line \d+\s+`[^`]*`\s*->\s*(?:error:\s*)?", re.I)
# "Rule `grantAdmin_revertConditions` references..."  →  "rule references..."
_NAME_ANCHOR = re.compile(r"\b(rule|invariant|definition|hook|ghost|definition)\s+`\w+`", re.I)
# certora: "could not type expression \"withdraw(amount)\", message: Missing env..." → the "message:" part
_CERTORA_EXPR = re.compile(r'^\s*could not type expression "[^"]*",\s*message:\s*', re.I)
# "Variable `ghostXxx` has not been declared"  →  generic guidance
_VAR_DECL = re.compile(r"variable\s+`\w+`\s+has not been declared", re.I)
# Any remaining backticked code snippet: replace with a neutral marker
_BACKTICKED = re.compile(r"`[^`]*`")
# Certora/solc "(some reason)" parentheticals appended to otherwise identical messages
_ERR_PREFIX = re.compile(r"^error:\s*", re.I)


def normalize_error(msg: str) -> str:
    """Map an error message to a canonical key.

    Idempotent and order-independent: the same semantic error — from a
    different rule, line, or code snippet — always yields the same key.
    """
    msg = msg.strip()
    if not msg:
        return ""
    msg = msg.lower()
    msg = _LINE_REF.sub("", msg)
    msg = _CERTORA_LINE_PREFIX.sub("", msg)
    msg = _CERTORA_EXPR.sub("", msg)
    msg = _VAR_DECL.sub(
        "variable has not been declared — declare all variables "
        "(e.g. env e; inside rules, ghost uint256 name; before the methods block) before use",
        msg,
    )
    msg = _NAME_ANCHOR.sub(r"\1", msg)
    msg = _BACKTICKED.sub("…", msg)
    msg = _ERR_PREFIX.sub("", msg)
    # collapse whitespace and trailing punctuation
    msg = re.sub(r"\s+", " ", msg).strip(" .;:-")
    return msg


# Backwards-compatible alias (older callers used _generalize).
def _generalize(msg: str) -> str:
    return normalize_error(msg)


class ErrorMemory:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS error_history (
                contract_hash TEXT,
                run_id TEXT,
                attempt INTEGER,
                spec_snapshot TEXT,
                errors TEXT,
                created_at REAL
            )
        """)
        self.conn.commit()

    @staticmethod
    def hash_contract(source: str) -> str:
        return hashlib.sha256(source.encode()).hexdigest()

    @staticmethod
    def new_run_id() -> str:
        return uuid.uuid4().hex[:12]

    def record(self, contract_hash: str, run_id: str, attempt: int, spec: str, errors: list[str]):
        # Generalize + deduplicate before storing — line-specific messages are useless
        # across runs; we want semantic patterns the repair prompt can act on.
        seen: set[str] = set()
        generalized: list[str] = []
        for e in errors:
            g = _generalize(e)
            if g and g not in seen:
                seen.add(g)
                generalized.append(g)
        self.conn.execute(
            "INSERT INTO error_history VALUES (?,?,?,?,?,?)",
            (contract_hash, run_id, attempt, spec, json.dumps(generalized), time.time()),
        )
        self.conn.commit()

    def all_known_errors(self, contract_hash: str, limit: int = 50) -> list[str]:
        """Every distinct error ever seen for this exact contract, most recent first."""
        rows = self.conn.execute(
            "SELECT errors FROM error_history WHERE contract_hash=? "
            "ORDER BY created_at DESC LIMIT ?",
            (contract_hash, limit),
        ).fetchall()
        seen: set[str] = set()
        out: list[str] = []
        for (errs_json,) in rows:
            for e in json.loads(errs_json):
                if e not in seen:
                    seen.add(e)
                    out.append(e)
        return out

    def close(self):
        self.conn.close()
