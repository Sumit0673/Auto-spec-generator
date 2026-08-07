"""Persistent error-memory store — SQLite-backed, keyed by contract hash.

Survives across CLI invocations so run N+1 never repeats run N's mistakes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path


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
        self.conn.execute(
            "INSERT INTO error_history VALUES (?,?,?,?,?,?)",
            (contract_hash, run_id, attempt, spec, json.dumps(errors), time.time()),
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
