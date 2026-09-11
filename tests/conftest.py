"""Session-wide test guards for the spec_pipeline test suite.

This conftest enforces two invariants for every test run (Requirements 10.2,
23.14):

1. **No network.** Outbound socket connections are blocked for the whole
   session so a test can never reach the LLM endpoint or any other host.
   Loopback and Unix-domain connections stay allowed because pytest plugins,
   coverage, and multiprocessing may rely on them.
2. **No real external binaries.** ``solc`` and ``certoraRun`` are stripped from
   ``PATH`` for the session so a test never invokes a real prover or compiler;
   tool behavior must come from recorded fixtures or shims instead.

Both guards install at import time (before any test collection that might spin
up resources) and are also exposed as an autouse session fixture so the intent
is visible in the fixture graph.
"""

import os
import socket

import pytest

# ---------------------------------------------------------------------------
# Network guard
# ---------------------------------------------------------------------------

# Addresses we still permit: loopback (pytest-xdist, coverage subprocesses,
# local IPC) and anything that is not an IP host connection.
_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


class NetworkBlockedError(RuntimeError):
    """Raised when a test attempts a disallowed outbound network connection."""


def _is_allowed_address(address):
    """Return True when *address* is a loopback/local target we let through."""
    if not isinstance(address, tuple) or not address:
        # Unix-domain sockets and other non (host, port) targets are local.
        return True
    host = address[0]
    return host in _ALLOWED_HOSTS


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _guarded_connect(self, address, *args, **kwargs):
    if not _is_allowed_address(address):
        raise NetworkBlockedError(
            "Network access is disabled during tests; attempted connection to "
            f"{address!r}"
        )
    return _REAL_CONNECT(self, address, *args, **kwargs)


def _guarded_connect_ex(self, address, *args, **kwargs):
    if not _is_allowed_address(address):
        raise NetworkBlockedError(
            "Network access is disabled during tests; attempted connection to "
            f"{address!r}"
        )
    return _REAL_CONNECT_EX(self, address, *args, **kwargs)


def _install_network_guard():
    socket.socket.connect = _guarded_connect
    socket.socket.connect_ex = _guarded_connect_ex


def _remove_network_guard():
    socket.socket.connect = _REAL_CONNECT
    socket.socket.connect_ex = _REAL_CONNECT_EX


# ---------------------------------------------------------------------------
# External-binary guard
# ---------------------------------------------------------------------------

_BLOCKED_BINARIES = ("solc", "certoraRun")


def _sanitized_path(path_value):
    """Return *path_value* with any directory that holds a blocked binary removed."""
    kept = []
    for entry in path_value.split(os.pathsep):
        if not entry:
            continue
        if any(
            os.path.exists(os.path.join(entry, name)) for name in _BLOCKED_BINARIES
        ):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

# Property tests must evaluate at least 100 generated examples (Requirement
# 23.10). We register and load a profile here so the setting applies suite-wide
# without every test having to repeat it. Hypothesis is an optional test
# dependency, so importing it is best-effort until it is pinned in task 5.1.
try:
    from hypothesis import HealthCheck, settings

    settings.register_profile(
        "ci",
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.load_profile("ci")
except ImportError:  # pragma: no cover - hypothesis not installed yet
    pass


# Install the guards at import time so they are active before any test runs.
_install_network_guard()
_ORIGINAL_PATH = os.environ.get("PATH", "")
os.environ["PATH"] = _sanitized_path(_ORIGINAL_PATH)


@pytest.fixture(scope="session", autouse=True)
def _session_guards():
    """Keep the network and binary guards active for the whole session.

    The guards are installed at import time; this autouse fixture documents the
    intent in the fixture graph and restores the original state on teardown.
    """
    yield
    _remove_network_guard()
    os.environ["PATH"] = _ORIGINAL_PATH


# ---------------------------------------------------------------------------
# Recorded-tool and LLM_Cache fixtures (Requirements 10.3, 10.4, 10.7, 10.8)
# ---------------------------------------------------------------------------
#
# The session guards above strip real ``solc``/``certoraRun`` from PATH and
# block the network so a test can never reach a real prover, compiler, or the
# LLM endpoint. These two opt-in fixtures give a test the *recorded* substitute
# for each:
#
# * ``recorded_tools`` prepends ``tests/fixtures/tools`` to PATH for the test so
#   ``solc``/``slither``/``certoraRun`` resolve to the shim scripts backed by
#   ``_shim.py``. The shim replays recorded output keyed by argv from a
#   recordings JSON, and fails naming the invocation when a recording is absent
#   (Requirements 10.4, 10.8).
# * ``llm_cache_fixture`` returns a loader over the seeded ``tests/fixtures/
#   llm_cache`` entries. A seeded digest replays its recorded response; an
#   unknown digest raises, naming the absent digest (Requirements 10.3, 10.7).

import hashlib
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_TOOLS_FIXTURE_DIR = _TESTS_DIR / "fixtures" / "tools"
_DEFAULT_RECORDINGS = _TOOLS_FIXTURE_DIR / "recordings.json"
_LLM_CACHE_FIXTURE_DIR = _TESTS_DIR / "fixtures" / "llm_cache"


class RecordedToolMissing(RuntimeError):
    """Raised (by an assertion on shim exit) when a tool invocation is unrecorded."""


@pytest.fixture
def recorded_tools(monkeypatch):
    """Prepend the recorded-tool shim dir to PATH for a test (R10.4, R10.8).

    Returns a callable ``activate(recordings_path=None)`` that a test invokes to
    turn on the shims with a chosen recordings file (defaulting to the seeded
    ``recordings.json``). Activating:

    * points ``TOOL_SHIM_RECORDINGS`` at the recordings file the shim replays,
    * sets ``TOOL_SHIM_PYTHON`` to the running interpreter so the shim scripts
      exec ``_shim.py`` without depending on a ``python`` on the sanitized test
      PATH, and
    * prepends the shim directory to PATH so ``solc``/``slither``/``certoraRun``
      resolve to the shims.

    ``monkeypatch`` restores PATH and the environment on teardown, so the shims
    are visible only for the duration of the opting-in test and the session-wide
    "tools absent" guarantee is preserved for every other test.
    """
    import sys as _sys

    def activate(recordings_path: "os.PathLike | str | None" = None) -> Path:
        recordings = Path(recordings_path) if recordings_path else _DEFAULT_RECORDINGS
        monkeypatch.setenv("TOOL_SHIM_RECORDINGS", str(recordings))
        monkeypatch.setenv("TOOL_SHIM_PYTHON", _sys.executable)
        current = os.environ.get("PATH", "")
        monkeypatch.setenv(
            "PATH", str(_TOOLS_FIXTURE_DIR) + os.pathsep + current
        )
        return _TOOLS_FIXTURE_DIR

    return activate


class LLMCacheFixture:
    """A loader over seeded LLM_Cache entries (R10.3, R10.7).

    A hit replays the recorded response for a digest; a miss raises
    :class:`KeyError` naming the absent digest, so a test that expected a
    recording fails loudly instead of silently falling through to a provider
    call (which the network guard would block anyway).
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)

    def key_for(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float
    ) -> str:
        """Return the digest that keys the recorded response (llm_cache.cache_key)."""
        from spec_pipeline.llm_cache import cache_key

        return cache_key(system_prompt, user_prompt, model, temperature)

    def get_by_digest(self, digest: str) -> str:
        """Return the recorded response for *digest*, or raise naming it (R10.7)."""
        path = self.cache_dir / f"{digest}.json"
        if not path.is_file():
            raise KeyError(
                f"LLM_Cache MISS: no recorded entry for digest {digest!r} "
                f"under {self.cache_dir!r}. Seed {path.name} or record this call."
            )
        import json as _json

        entry = _json.loads(path.read_text(encoding="utf-8"))
        response = entry.get("response")
        if not isinstance(response, str):
            raise KeyError(
                f"LLM_Cache entry {path.name!r} has no string 'response' field"
            )
        return response

    def get(
        self, system_prompt: str, user_prompt: str, model: str, temperature: float
    ) -> str:
        """Replay the recorded response for a prompt, or raise naming the digest."""
        return self.get_by_digest(
            self.key_for(system_prompt, user_prompt, model, temperature)
        )


@pytest.fixture
def llm_cache_fixture() -> "LLMCacheFixture":
    """Return a loader over the seeded LLM_Cache entries (R10.3, R10.7)."""
    return LLMCacheFixture(_LLM_CACHE_FIXTURE_DIR)
