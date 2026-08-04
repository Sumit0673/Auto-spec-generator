"""Small, optional Certora compilation gate for generated CVL specs."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationResult:
    status: str  # passed, failed, unavailable, timed_out
    command: list[str]
    output: str
    returncode: int | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def _certora_run_path() -> str | None:
    """Prefer the certoraRun installed beside the active Python interpreter."""
    local = Path(sys.executable).with_name("certoraRun")
    if local.is_file():
        return str(local)
    return shutil.which("certoraRun")


def validate_cvl(
    contract_path: str | Path,
    spec_path: str | Path,
    contract_name: str,
    timeout_seconds: int = 300,
    project_root: str | Path | None = None,
    certora_config: str | Path | None = None,
) -> ValidationResult:
    """Compile Solidity and CVL locally without submitting a proof job."""
    certora_run = _certora_run_path()
    if not certora_run:
        return ValidationResult(
            status="unavailable",
            command=[],
            output="certoraRun is not installed; spec was generated but not validated.",
        )

    root = Path(project_root).resolve() if project_root else Path(contract_path).resolve().parent
    contract_file = Path(contract_path).resolve()
    input_file = Path(certora_config).resolve() if certora_config else f"{contract_file}:{contract_name}"
    command = [
        certora_run,
        str(input_file),
        "--verify",
        f"{contract_name}:{Path(spec_path).resolve()}",
        "--compilation_steps_only",
        "--short_output",
    ]
    if not certora_config:
        command.extend(["--solc_allow_path", str(root)])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            cwd=root,
        )
    except subprocess.TimeoutExpired as error:
        return ValidationResult(
            status="timed_out",
            command=command,
            output=(error.stdout or "") + (error.stderr or ""),
        )

    return ValidationResult(
        status="passed" if completed.returncode == 0 else "failed",
        command=command,
        output=(completed.stdout + completed.stderr).strip(),
        returncode=completed.returncode,
    )
