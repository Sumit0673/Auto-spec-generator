#!/usr/bin/env bash
#
# bootstrap.sh - Bootstrap_Script for the Robust Spec Generation pipeline.
#
# Responsibilities (Requirements 5.3, 5.9):
#   1. Install the declared Python dependencies into the *active* virtual
#      environment at their pinned (==) versions, sourced from pyproject.toml.
#   2. Print external-tool status: for each of solc, certoraRun, node+npm, and
#      the GitHub CLI (gh), print either the resolved absolute path with its
#      reported version, or "absent" together with the pipeline stage(s) that
#      the missing tool blocks.
#
# On a dependency install failure at the pinned version, the script exits 1
# with an error naming the dependency and its pinned version, and never
# silently installs a different version.
#
# This script does not create or activate a virtual environment; it installs
# into whatever environment is currently active. Run it inside a clean venv of
# a supported Python version with network access.

set -euo pipefail

# --- Locate the repository root and the Project_Manifest --------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
PYPROJECT="${REPO_ROOT}/pyproject.toml"

# Choose a Python interpreter (prefer python3, fall back to python).
if command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON="python"
else
  echo "ERROR: no python3 or python interpreter found on PATH." >&2
  exit 1
fi

echo "=== Robust Spec Generation bootstrap ==="
echo "Repository root : ${REPO_ROOT}"
echo "Python          : $(command -v "${PYTHON}") ($("${PYTHON}" --version 2>&1))"
echo "pip             : $("${PYTHON}" -m pip --version 2>&1)"
echo

# --- Step 1: install declared, pinned Python dependencies -------------------
if [ ! -f "${PYPROJECT}" ]; then
  echo "ERROR: Project_Manifest not found at ${PYPROJECT}." >&2
  echo "       pyproject.toml must declare the pinned dependencies (task 5.1)." >&2
  exit 1
fi

echo "--- Installing pinned Python dependencies from pyproject.toml ---"

# Extract every 'name==version' pin declared anywhere in pyproject.toml
# (project.dependencies and any optional-dependencies groups such as tests).
# We rely on the exact '==' pins required by Requirement 5.2 so that a failed
# install is reported instead of resolving to a different version.
mapfile -t PINNED_DEPS < <(
  "${PYTHON}" - "${PYPROJECT}" <<'PYEOF'
import sys

try:
    try:
        import tomllib as toml  # Python 3.11+
        mode = "rb"
    except ModuleNotFoundError:
        import tomli as toml     # Python 3.8-3.10 (if available)
        mode = "rb"
    with open(sys.argv[1], mode) as fh:
        data = toml.load(fh)
except Exception as exc:  # pragma: no cover - reported to the shell below
    sys.stderr.write("PARSE_ERROR: %s\n" % exc)
    sys.exit(3)

project = data.get("project", {})
specs = list(project.get("dependencies", []) or [])
for group in (project.get("optional-dependencies", {}) or {}).values():
    specs.extend(group or [])

seen = set()
for spec in specs:
    s = spec.strip()
    if "==" not in s:
        # Not an exact pin; skip here (Requirement 5.2 expects '==' pins).
        continue
    if s in seen:
        continue
    seen.add(s)
    print(s)
PYEOF
) || {
  echo "ERROR: failed to parse pinned dependencies from ${PYPROJECT}." >&2
  exit 1
}

if [ "${#PINNED_DEPS[@]}" -eq 0 ]; then
  echo "ERROR: no exact ('==') version pins found in ${PYPROJECT}." >&2
  echo "       Every declared dependency must be pinned (Requirement 5.2)." >&2
  exit 1
fi

for dep in "${PINNED_DEPS[@]}"; do
  name="${dep%%==*}"
  version="${dep##*==}"
  echo "Installing ${name} (pinned ${version})..."
  # Install the exact pin. If the pinned version cannot be installed, fail
  # loudly instead of letting pip resolve a different version.
  if ! "${PYTHON}" -m pip install "${dep}"; then
    echo "ERROR: failed to install dependency '${name}' at pinned version '${version}'." >&2
    echo "       Not installing a different version. Aborting." >&2
    exit 1
  fi

  # Verify the resolved version matches the pin exactly (defense against any
  # index/resolver surprise that would otherwise leave a mismatched version).
  installed="$("${PYTHON}" -m pip show "${name}" 2>/dev/null | awk -F': ' '/^Version:/ {print $2}')"
  if [ -n "${installed}" ] && [ "${installed}" != "${version}" ]; then
    echo "ERROR: dependency '${name}' resolved to '${installed}', expected pinned '${version}'." >&2
    echo "       Not accepting a different version. Aborting." >&2
    exit 1
  fi
done

# Install the pipeline package itself (editable) so 'spec_pipeline' is importable.
echo
echo "Installing the spec_pipeline package (editable) from pyproject.toml..."
if ! "${PYTHON}" -m pip install -e "${REPO_ROOT}"; then
  echo "ERROR: failed to install the spec_pipeline package from ${PYPROJECT}." >&2
  exit 1
fi

echo
echo "All pinned Python dependencies installed successfully."
echo

# --- Step 2: report external tool status ------------------------------------
# Each entry: "tool|blocked-stages description". If the tool is absent we print
# "absent" together with the stage(s) it blocks.
echo "--- External tool status ---"

report_tool() {
  # $1 = display name, $2 = executable to resolve,
  # $3 = version command, $4 = blocked-stages description
  local display="$1"
  local exe="$2"
  local version_cmd="$3"
  local blocks="$4"
  local resolved
  if resolved="$(command -v "${exe}" 2>/dev/null)"; then
    # Resolve to an absolute path.
    resolved="$(cd -- "$(dirname -- "${resolved}")" >/dev/null 2>&1 && pwd)/$(basename -- "${resolved}")"
    local ver
    ver="$(eval "${version_cmd}" 2>&1 | head -n 1 || true)"
    printf '  %-12s %s (%s)\n' "${display}:" "${resolved}" "${ver}"
  else
    printf '  %-12s absent -- blocks %s\n' "${display}:" "${blocks}"
  fi
}

# solc + slither drive Stage 1 (extraction) and Stage 5 (verification).
report_tool "solc"      "solc"       "solc --version"        "stage 1 and stage 5 (solc/slither)"
# certoraRun drives Stage 5 (verification).
report_tool "certoraRun" "certoraRun" "certoraRun --version" "stage 5 (verification)"
# node/npm are used for dependency resolution on Hardhat/npm project layouts.
report_tool "node"      "node"       "node --version"        "dependency resolution for Hardhat/npm layouts"
report_tool "npm"       "npm"        "npm --version"         "dependency resolution for Hardhat/npm layouts"
# gh (GitHub CLI) is used for data scraping.
report_tool "gh"        "gh"         "gh --version"          "data scraping (GitHub CLI)"

echo
echo "Bootstrap complete."
