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

# ── Error generalization ──────────────────────────────────────────────────────
# Strip position/name specifics before storing so the same mistake on a
# different line or with a different identifier still matches as a known error.
_GENERALIZATIONS: list[tuple[re.Pattern, str]] = [
    # "Line 189 `invariant foo() {` -> Error: msg"  →  "msg"
    (re.compile(r'^Line \d+\s+`[^`]*`\s*->\s*(?:Error:\s*)?'), ''),
    # "Variable `ghostXxx` has not been declared"  →  generic
    (re.compile(r"Variable `\w+` has not been declared"),
     "Variable has not been declared — declare all variables "
     "(e.g. `env e;` inside rules, `ghost uint256 name;` before methods block) before use"),
    # Strip standalone "Error: " prefix
    (re.compile(r'^Error:\s*'), ''),
    # Strip line references embedded in messages
    (re.compile(r'\bline \d+\b', re.I), ''),
]


def _generalize(msg: str) -> str:
    """Strip position/identifier specifics; keep the semantic error pattern."""
    for pat, repl in _GENERALIZATIONS:
        msg = pat.sub(repl, msg)
    return msg.strip()


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
