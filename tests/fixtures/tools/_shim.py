#!/usr/bin/env python3
"""Recorded-tool shim (Requirements 10.4, 10.8).

A single replay engine backing the ``solc``, ``slither``, and ``certoraRun``
shim scripts in this directory. A test that opts into the ``recorded_tools``
fixture gets this directory prepended to ``PATH``; when the code under test then
invokes one of those tools, the matching shim script execs this engine, which:

1. Reads a recordings file (JSON) named by the ``TOOL_SHIM_RECORDINGS``
   environment variable (the ``recorded_tools`` fixture sets it).
2. Looks up a recording keyed by the invocation - the tool name plus its
   argv - and replays the recorded ``stdout``/``stderr`` and exits with the
   recorded exit code (Requirement 10.4: recorded tool output is replayed).
3. When no recording exists for the invocation, prints a diagnostic that NAMES
   the tool and the exact argv that was not recorded, then exits non-zero so the
   test fails loudly instead of silently reaching a real binary (Requirement
   10.8: fail naming the invocation when a recording is missing).

Recordings format (``recordings.json``)::

    {
      "records": [
        {
          "tool": "solc",
          "argv": ["--version"],
          "stdout": "solc, the solidity compiler ...\n",
          "stderr": "",
          "exit": 0
        }
      ]
    }

Matching is by ``(tool, argv)``. A record whose ``argv`` is the JSON literal
``"*"`` matches any argv for that tool (a catch-all a test can seed for a tool
whose exact argv is an implementation detail). Exact-argv records win over the
catch-all. ``stdout``/``stderr`` default to the empty string and ``exit`` to 0
when omitted.

The engine is deliberately dependency-free (standard library only) so the shim
runs under any interpreter on ``PATH`` and never triggers the test suite's
network guard.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _load_records() -> list[dict]:
    recordings_env = os.environ.get("TOOL_SHIM_RECORDINGS")
    if not recordings_env:
        _fail_no_recordings_file(None, "TOOL_SHIM_RECORDINGS is not set")
    path = Path(recordings_env)
    if not path.is_file():
        _fail_no_recordings_file(path, "recordings file does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # pragma: no cover - defensive
        _fail_no_recordings_file(path, f"recordings file is unreadable: {exc}")
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        _fail_no_recordings_file(path, "recordings file has no 'records' list")
    return records


def _fail_no_recordings_file(path, reason: str) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write(
        "tool shim: cannot replay - "
        f"{reason} (path={path!r}). Set TOOL_SHIM_RECORDINGS to a recordings "
        "file seeded for this invocation.\n"
    )
    raise SystemExit(97)


def _find_record(records: list[dict], tool: str, argv: list[str]) -> dict | None:
    catch_all: dict | None = None
    for record in records:
        if record.get("tool") != tool:
            continue
        rec_argv = record.get("argv")
        if rec_argv == argv:
            return record
        if rec_argv == "*" and catch_all is None:
            catch_all = record
    return catch_all


def main(tool: str, argv: list[str]) -> int:
    records = _load_records()
    record = _find_record(records, tool, argv)
    if record is None:
        # Requirement 10.8: no recording -> fail naming the invocation.
        sys.stderr.write(
            "tool shim: NO RECORDING for invocation "
            f"{tool} {' '.join(argv)!r}. "
            "Record this invocation in the recordings file "
            f"({os.environ.get('TOOL_SHIM_RECORDINGS')!r}) or remove the call.\n"
        )
        return 96
    sys.stdout.write(record.get("stdout", "") or "")
    sys.stderr.write(record.get("stderr", "") or "")
    return int(record.get("exit", 0) or 0)


if __name__ == "__main__":  # pragma: no cover - exercised via the shim scripts
    # argv[1] is the tool name the wrapper passes; the rest is the tool's argv.
    _tool = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    raise SystemExit(main(_tool, sys.argv[2:]))
