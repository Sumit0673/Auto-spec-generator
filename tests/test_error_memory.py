"""Tests for error_memory module."""

import json
import tempfile
from pathlib import Path

from auto_spec.error_memory import ErrorMemory


class TestErrorMemory:
    def _make_memory(self, tmp_path):
        return ErrorMemory(tmp_path / "test_errors.db")

    def test_record_and_retrieve_roundtrip(self, tmp_path):
        mem = self._make_memory(tmp_path)
        h = ErrorMemory.hash_contract("contract X {}")
        mem.record(h, "run1", 0, "methods {}", ["Variable Y not declared", "syntax error"])
        errors = mem.all_known_errors(h)
        assert "Variable Y not declared" in errors
        assert "syntax error" in errors
        mem.close()

    def test_deduplication_across_runs(self, tmp_path):
        mem = self._make_memory(tmp_path)
        h = ErrorMemory.hash_contract("contract X {}")
        mem.record(h, "run1", 0, "spec1", ["error A", "error B"])
        mem.record(h, "run2", 0, "spec2", ["error B", "error C"])
        errors = mem.all_known_errors(h)
        # B should appear only once
        assert errors.count("error B") == 1
        # All three should be present
        assert set(errors) == {"error A", "error B", "error C"}
        mem.close()

    def test_different_contracts_are_isolated(self, tmp_path):
        mem = self._make_memory(tmp_path)
        h1 = ErrorMemory.hash_contract("contract A {}")
        h2 = ErrorMemory.hash_contract("contract B {}")
        mem.record(h1, "run1", 0, "spec", ["error for A"])
        mem.record(h2, "run1", 0, "spec", ["error for B"])
        assert mem.all_known_errors(h1) == ["error for A"]
        assert mem.all_known_errors(h2) == ["error for B"]
        mem.close()

    def test_empty_history_returns_empty_list(self, tmp_path):
        mem = self._make_memory(tmp_path)
        h = ErrorMemory.hash_contract("contract Z {}")
        assert mem.all_known_errors(h) == []
        mem.close()

    def test_hash_is_deterministic(self):
        h1 = ErrorMemory.hash_contract("contract X {}")
        h2 = ErrorMemory.hash_contract("contract X {}")
        assert h1 == h2

    def test_most_recent_errors_come_first(self, tmp_path):
        mem = self._make_memory(tmp_path)
        h = ErrorMemory.hash_contract("contract X {}")
        mem.record(h, "run1", 0, "spec1", ["old error"])
        mem.record(h, "run2", 0, "spec2", ["new error"])
        errors = mem.all_known_errors(h)
        assert errors[0] == "new error"
        mem.close()
