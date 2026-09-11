"""
Preflight_Checker - resolve external tool availability before any stage runs
(design: Preflight_Checker; Requirements 6.1-6.7).

The current pipeline burns LLM budget across stages 2-4 and only discovers at
stage 5 that ``certoraRun`` is missing. This module resolves the Python
interpreter, ``slither``, ``solc`` (possibly several installed versions), and
``certoraRun`` *before* the orchestrator runs anything, derives which tools each
requested stage actually needs, and produces a result structure the orchestrator
writes into the Run_Manifest.

Design constraints honored here:

* The interpreter's own ``bin`` directory (``Path(sys.executable).parent``) is
  searched BEFORE the ``PATH`` entries (Requirement 6.2). The first runnable
  executable file found wins.
* A version probe that does not return within :data:`VERSION_PROBE_TIMEOUT`
  seconds, or that yields no recognizable version number, records the version as
  ``"unknown"`` (Requirement 6.1).
* Required tools are derived from the requested stages (Requirement 6.8):
  stage 1 needs ``slither`` + ``solc``; the LLM stages 2, 3, 4 need the
  configured LLM endpoint; stage 5 needs ``certoraRun`` + ``solc``.
* Any absent required tool is reported with its blocked stages, the searched
  locations, and the install command drawn from the Documentation, before any
  LLM call (Requirement 6.3).
* Decision helpers map an absent required tool to outcome ``tool_unavailable``
  (exit 5) when ``--allow-missing-tools`` is off, or to a per-stage
  ``skipped_missing_tool`` outcome when it is on (Requirements 6.4, 6.5).
* When an LLM stage is requested, at most one LLM probe runs (Requirement 6.9).
  The probe is injectable so tests pass a fake; the default probe performs NO
  network call. An auth failure, a model-not-found, or a timeout yields
  ``llm_unavailable`` reporting the base URL and model with the API key redacted
  (Requirement 6.7).

The module is import-safe without slither: it never imports the slither-backed
stages, so it can be loaded directly (see the importlib fallback in
``tests/unit/test_artifacts_basics.py``) even where slither is uninstallable.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "VERSION_PROBE_TIMEOUT",
    "PREFLIGHT_TIMEOUT_BUDGET",
    "UNKNOWN_VERSION",
    "REDACTED",
    "INSTALL_COMMANDS",
    "ToolResult",
    "LLMProbeResult",
    "PreflightResult",
    "Preflight_Checker",
    "required_tools_for_stages",
    "stages_requiring_llm",
]

# A version probe must return within this many seconds or the version is
# recorded as "unknown" (Requirement 6.1).
VERSION_PROBE_TIMEOUT = 5.0

# When every requested tool resolves on the local filesystem the whole check
# must complete within this many seconds (Requirement 6.6). Kept as a documented
# budget; the orchestrator may assert against it.
PREFLIGHT_TIMEOUT_BUDGET = 15.0

UNKNOWN_VERSION = "unknown"

# The literal written in place of any secret value (Requirement 21.2, 6.7).
REDACTED = "REDACTED"

# Install commands drawn from the Documentation (Requirement 5.4). Kept here so
# preflight can name the fix for an absent tool (Requirement 6.3) without
# importing the (not-yet-written) README.
INSTALL_COMMANDS: dict[str, str] = {
    "python": "install a supported Python (3.10+) from https://www.python.org/downloads/",
    "slither": "pip install slither-analyzer",
    "solc": "pip install solc-select && solc-select install <version> && solc-select use <version>",
    "certoraRun": "pip install certora-cli",
}

# Tokens the LLM probe classifies as an unavailable endpoint (Requirement 6.7,
# 6.9): authentication rejection, model-not-found, or timeout.
_LLM_UNAVAILABLE_TOKENS = (
    "auth",
    "unauthor",
    "forbidden",
    "401",
    "403",
    "model_not_found",
    "model not found",
    "does not exist",
    "not found",
    "timeout",
    "timed out",
)


# ---------------------------------------------------------------------------
# Stage -> required tool derivation (Requirement 6.8)
# ---------------------------------------------------------------------------

# Binary tools (resolved on the filesystem) required per stage.
_STAGE_BINARY_TOOLS: dict[int, tuple[str, ...]] = {
    1: ("slither", "solc"),
    2: (),
    3: (),
    4: (),
    5: ("certoraRun", "solc"),
}

# Stages that require the configured LLM endpoint.
_LLM_STAGES = frozenset({2, 3, 4})


def stages_requiring_llm(stages: list[int]) -> list[int]:
    """Return the subset of *stages* that require the LLM endpoint (R6.8)."""
    return sorted(s for s in stages if s in _LLM_STAGES)


def required_tools_for_stages(stages: list[int]) -> dict[str, list[int]]:
    """Map each required binary tool to the requested stages that need it.

    Derives the required-tool set from the requested stages (Requirement 6.8):
    ``slither``/``solc`` for stage 1, ``certoraRun``/``solc`` for stage 5. The
    LLM endpoint is handled separately via :func:`stages_requiring_llm` because
    it is probed, not resolved on the filesystem.

    Args:
        stages: The requested stage numbers.

    Returns:
        A dict mapping tool name -> sorted list of requesting stage numbers.
    """
    tool_to_stages: dict[str, list[int]] = {}
    for stage in stages:
        for tool in _STAGE_BINARY_TOOLS.get(stage, ()):  # unknown stage -> no tools
            tool_to_stages.setdefault(tool, [])
            if stage not in tool_to_stages[tool]:
                tool_to_stages[tool].append(stage)
    for tool in tool_to_stages:
        tool_to_stages[tool].sort()
    return tool_to_stages


# ---------------------------------------------------------------------------
# Result structures (written into the Run_Manifest)
# ---------------------------------------------------------------------------


@dataclass
class ToolResult:
    """Resolution of one external tool (Requirement 6.1)."""

    name: str
    present: bool
    path: Optional[str] = None
    version: str = UNKNOWN_VERSION
    # All directories searched, in order (interpreter dir first, then PATH).
    searched: list[str] = field(default_factory=list)
    # Stages that this tool blocks when it is absent (empty when present or
    # when no requested stage needs it).
    blocked_stages: list[int] = field(default_factory=list)
    install_command: Optional[str] = None
    # For solc, the additional installed versions discovered beyond the first.
    all_paths: list[str] = field(default_factory=list)

    def to_manifest(self) -> dict:
        """Render a Run_Manifest-friendly dict (no secrets involved)."""
        return {
            "name": self.name,
            "present": self.present,
            "path": self.path,
            "version": self.version,
            "searched": list(self.searched),
            "blocked_stages": list(self.blocked_stages),
            "install_command": self.install_command,
            "all_paths": list(self.all_paths),
        }


@dataclass
class LLMProbeResult:
    """Outcome of the single optional LLM probe (Requirements 6.7, 6.9)."""

    probed: bool
    available: bool
    base_url: Optional[str] = None
    model: Optional[str] = None
    # Present only on failure; the API key is never included here.
    reason: Optional[str] = None

    def to_manifest(self) -> dict:
        return {
            "probed": self.probed,
            "available": self.available,
            "base_url": self.base_url,
            "model": self.model,
            "reason": self.reason,
            # Always record that the key was redacted, never its value (R6.7).
            "api_key": REDACTED,
        }


@dataclass
class PreflightResult:
    """The full preflight result the orchestrator writes into the manifest.

    ``requested_stages`` records what was asked for; ``tools`` holds one
    :class:`ToolResult` per resolved binary; ``llm`` holds the probe outcome.
    The decision helpers on :class:`Preflight_Checker` consume this to select an
    outcome and exit code.
    """

    requested_stages: list[int]
    tools: dict[str, ToolResult]
    llm: LLMProbeResult

    def missing_required_tools(self) -> list[ToolResult]:
        """Return the required tools that resolved as absent (Requirement 6.3).

        A tool is "required" here when it blocks at least one requested stage.
        """
        missing = [
            tr for tr in self.tools.values()
            if tr.blocked_stages and not tr.present
        ]
        missing.sort(key=lambda tr: tr.name)
        return missing

    def to_manifest(self) -> dict:
        return {
            "requested_stages": list(self.requested_stages),
            "tools": {name: tr.to_manifest() for name, tr in sorted(self.tools.items())},
            "llm": self.llm.to_manifest(),
        }


# ---------------------------------------------------------------------------
# Preflight_Checker
# ---------------------------------------------------------------------------


class Preflight_Checker:
    """Resolve external tools and the LLM endpoint before any stage runs.

    Args:
        stages: The requested stage numbers.
        allow_missing_tools: Whether ``--allow-missing-tools`` was supplied.
        llm_config: Optional dict with ``base_url``, ``model`` (and, if present,
            an api-key-bearing key that is never recorded). Defaults are read
            from the ``LLM_BASE_URL``/``LLM_MODEL`` environment variables.
        llm_probe: Injectable probe callable. It receives ``(base_url, model)``
            and MUST return ``None`` on success or a short failure reason string
            on failure. The default probe performs NO network call (it returns
            ``None`` unconditionally) so importing/using this module never
            reaches the network; tests inject a fake to exercise the
            ``llm_unavailable`` path.
        env: Environment mapping (defaults to ``os.environ``); injectable for
            tests.
        version_probe_timeout: Per-tool version probe timeout in seconds.
    """

    def __init__(
        self,
        stages: list[int],
        allow_missing_tools: bool = False,
        llm_config: Optional[dict] = None,
        llm_probe: Optional[Callable[[str, str], Optional[str]]] = None,
        env: Optional[dict] = None,
        version_probe_timeout: float = VERSION_PROBE_TIMEOUT,
    ) -> None:
        self.stages = sorted(set(stages))
        self.allow_missing_tools = allow_missing_tools
        self._env = env if env is not None else os.environ
        self._version_probe_timeout = version_probe_timeout
        self._llm_probe = llm_probe if llm_probe is not None else self._default_llm_probe

        cfg = dict(llm_config or {})
        self._llm_base_url = cfg.get("base_url") or self._env.get(
            "LLM_BASE_URL", "http://localhost:20127/v1"
        )
        self._llm_model = cfg.get("model") or self._env.get("LLM_MODEL", "auto")

    # -- resolution -------------------------------------------------------

    def _search_dirs(self) -> list[Path]:
        """Directories to search, interpreter dir FIRST then PATH (R6.2)."""
        dirs: list[Path] = []
        seen: set[str] = set()

        interp_dir = Path(sys.executable).parent
        dirs.append(interp_dir)
        seen.add(str(interp_dir))

        for entry in self._env.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            if entry in seen:
                continue
            seen.add(entry)
            dirs.append(Path(entry))
        return dirs

    @staticmethod
    def _is_runnable(candidate: Path) -> bool:
        """True when *candidate* is a regular file the user can execute (R6.1)."""
        return candidate.is_file() and os.access(candidate, os.X_OK)

    def _find_all(self, name: str) -> tuple[list[str], list[str]]:
        """Return (found_paths, searched_dirs) for executable *name*.

        Searches the interpreter directory before PATH (Requirement 6.2) and
        selects the first runnable executable file the user can run
        (Requirement 6.1). All matching paths are collected in search order so
        callers (e.g. solc) can inspect multiple installed versions.
        """
        found: list[str] = []
        searched: list[str] = []
        for directory in self._search_dirs():
            searched.append(str(directory))
            candidate = directory / name
            if self._is_runnable(candidate) and str(candidate) not in found:
                found.append(str(candidate))
        return found, searched

    def _probe_version(self, executable: str) -> str:
        """Return a version string, or ``"unknown"`` on timeout/no number (R6.1)."""
        for flag in ("--version", "version", "-V"):
            try:
                proc = subprocess.run(
                    [executable, flag],
                    capture_output=True,
                    text=True,
                    timeout=self._version_probe_timeout,
                    check=False,
                )
            except (subprocess.TimeoutExpired, OSError):
                # A probe that does not return in time -> unknown (R6.1).
                return UNKNOWN_VERSION
            blob = f"{proc.stdout}\n{proc.stderr}"
            version = self._parse_version(blob)
            if version is not None:
                return version
        return UNKNOWN_VERSION

    @staticmethod
    def _parse_version(text: str) -> Optional[str]:
        """Extract a dotted version number, or None when there is none."""
        match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", text)
        return match.group(1) if match else None

    def _resolve_tool(
        self, name: str, blocked_stages: list[int], collect_all: bool = False
    ) -> ToolResult:
        found, searched = self._find_all(name)
        if not found:
            return ToolResult(
                name=name,
                present=False,
                path=None,
                version=UNKNOWN_VERSION,
                searched=searched,
                blocked_stages=sorted(blocked_stages),
                install_command=INSTALL_COMMANDS.get(name),
                all_paths=[],
            )
        primary = found[0]
        version = self._probe_version(primary)
        all_paths = found if collect_all else [primary]
        return ToolResult(
            name=name,
            present=True,
            path=primary,
            version=version,
            searched=searched,
            blocked_stages=[],  # present -> blocks nothing
            install_command=INSTALL_COMMANDS.get(name),
            all_paths=all_paths,
        )

    # -- LLM probe --------------------------------------------------------

    @staticmethod
    def _default_llm_probe(base_url: str, model: str) -> Optional[str]:
        """Default probe: performs NO network call, reports success.

        Tests inject a fake probe to exercise the ``llm_unavailable`` path; the
        default never reaches the network (Requirement 6.9).
        """
        return None

    def _run_llm_probe(self) -> LLMProbeResult:
        """Run at most one LLM probe when an LLM stage is requested (R6.9)."""
        if not stages_requiring_llm(self.stages):
            return LLMProbeResult(probed=False, available=True)

        try:
            reason = self._llm_probe(self._llm_base_url, self._llm_model)
        except Exception as exc:  # a raising probe is treated as unavailable
            reason = str(exc) or exc.__class__.__name__

        if reason is None:
            return LLMProbeResult(
                probed=True,
                available=True,
                base_url=self._llm_base_url,
                model=self._llm_model,
            )
        return LLMProbeResult(
            probed=True,
            available=False,
            base_url=self._llm_base_url,
            model=self._llm_model,
            reason=self._redact(reason),
        )

    def _redact(self, message: str) -> str:
        """Replace any configured API key value with ``REDACTED`` (R6.7)."""
        key = self._env.get("LLM_API_KEY")
        if key and key in message:
            message = message.replace(key, REDACTED)
        return message

    # -- public API -------------------------------------------------------

    def run(self) -> PreflightResult:
        """Resolve every tool and probe the LLM, returning the full result.

        Always resolves the interpreter, ``slither``, ``solc``, and
        ``certoraRun`` so the Run_Manifest records all four (Requirement 6.1),
        while only the tools required by the requested stages carry
        ``blocked_stages`` (Requirement 6.8).
        """
        required = required_tools_for_stages(self.stages)

        tools: dict[str, ToolResult] = {}

        # The interpreter is always present (we are running in it) but recorded
        # for the manifest (R6.1).
        tools["python"] = ToolResult(
            name="python",
            present=True,
            path=sys.executable,
            version=self._probe_version(sys.executable),
            searched=[str(Path(sys.executable).parent)],
            blocked_stages=[],
            install_command=INSTALL_COMMANDS.get("python"),
            all_paths=[sys.executable],
        )

        for name, collect_all in (("slither", False), ("solc", True), ("certoraRun", False)):
            blocked = required.get(name, [])
            tools[name] = self._resolve_tool(name, blocked, collect_all=collect_all)

        llm = self._run_llm_probe()

        return PreflightResult(
            requested_stages=list(self.stages),
            tools=tools,
            llm=llm,
        )

    # -- decision helpers -------------------------------------------------

    def decide(self, result: PreflightResult) -> dict:
        """Select the preflight outcome and exit code from *result*.

        Returns a decision dict the orchestrator acts on:

        * ``{"outcome": "llm_unavailable", "exit_code": 10, ...}`` when an LLM
          stage was requested and the probe was rejected (Requirement 6.7). This
          is reported with the base URL and model, key redacted, and takes
          precedence because an LLM stage cannot proceed without the endpoint.
        * ``{"outcome": "tool_unavailable", "exit_code": 5, ...}`` when a
          required tool is absent and ``--allow-missing-tools`` is off
          (Requirement 6.4).
        * ``{"outcome": "proceed", "exit_code": 0, "skipped": {...}}`` when the
          flag is on: the stages whose tools resolved run, and every stage
          blocked by a missing tool is recorded as ``skipped_missing_tool`` with
          the tool name (Requirement 6.5). ``skipped`` maps stage -> tool name.
        * ``{"outcome": "proceed", "exit_code": 0}`` when everything resolved.

        The caller reports the blocked stages, searched locations, and install
        command (already on each :class:`ToolResult`) before issuing any LLM
        call (Requirement 6.3).
        """
        if result.llm.probed and not result.llm.available:
            return {
                "outcome": "llm_unavailable",
                "exit_code": 10,
                "base_url": result.llm.base_url,
                "model": result.llm.model,
                "api_key": REDACTED,
                "reason": result.llm.reason,
            }

        missing = result.missing_required_tools()
        if not missing:
            return {"outcome": "proceed", "exit_code": 0}

        if not self.allow_missing_tools:
            return {
                "outcome": "tool_unavailable",
                "exit_code": 5,
                "missing_tools": [
                    {
                        "name": tr.name,
                        "blocked_stages": list(tr.blocked_stages),
                        "searched": list(tr.searched),
                        "install_command": tr.install_command,
                    }
                    for tr in missing
                ],
            }

        # --allow-missing-tools: run resolvable stages; record the rest as
        # skipped_missing_tool with the tool name (R6.5).
        skipped: dict[int, str] = {}
        for tr in missing:
            for stage in tr.blocked_stages:
                # First blocking tool per stage wins the attribution.
                skipped.setdefault(stage, tr.name)
        return {
            "outcome": "proceed",
            "exit_code": 0,
            "skipped": skipped,
            "skipped_outcome": "skipped_missing_tool",
        }
