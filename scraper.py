#!/usr/bin/env python3
"""
Certora SecurityReports Dataset Scraper v2

Scrapes:
1. All PDF reports from Certora/SecurityReports/Reports/*
2. Extracts GitHub URLs from PDF annotations (hyperlinks to specific branches/files)
3. Scrapes those repos/branches recursively for .sol and .spec file pairs
4. Stores pairs in contracts_with_specs/, lone contracts in contracts_only/,
   lone specs in spec_only/

Key improvements over v1:
- Uses annotation URLs to find repos/branches WITH specs (not just default branch)
- Searches entire repo tree for .spec files (not just certora/specs/)
- Matches .sol/.spec by base name across folders
- Falls back to default branch if annotation branch is deprecated
- Verbose logging shows every repo/branch scraped and files found
- Creates spec_only/ for lone .spec files

Usage:
  python3 scraper.py --list-reports
  python3 scraper.py --download-reports
  python3 scraper.py --map-repos
  python3 scraper.py --scrape-all [--verbose]
  python3 scraper.py --scrape-urls [--verbose]   # scrape from annotation URLs only
"""

import argparse
import json
import os
import subprocess
import re
import sys
import warnings
from pathlib import Path

# Suppress pdfplumber FontBBox warnings (harmless)
warnings.filterwarnings("ignore", message="Could not get FontBBox")
os.environ["PYTHONWARNINGS"] = "ignore::UserWarning:pdfplumber"

# Dataset base directory and derived paths are resolved at runtime (see
# resolve_base / configure_paths) rather than hardcoded, so the scraper runs
# from any checkout location and honors --base / CERTORA_DATASET_ROOT.
BASE = None
REPORTS_DIR = None
CONTRACTS_WITH_SPECS = None
CONTRACTS_ONLY = None
SPEC_ONLY = None
MAPPING_FILE = None

VERBOSE = False


def resolve_base(cli_base=None):
    """
    Resolve the dataset base directory in priority order:
      1. the --base CLI argument
      2. the CERTORA_DATASET_ROOT environment variable
      3. the repository root inferred from this module's location

    Returns the first candidate that resolves to a readable directory.
    Raises RuntimeError if none of the candidates is a readable directory.
    """
    candidates = [
        ("--base argument", cli_base),
        ("CERTORA_DATASET_ROOT environment variable", os.environ.get("CERTORA_DATASET_ROOT")),
        ("module location", Path(__file__).resolve().parent),
    ]

    tried = []
    for source, value in candidates:
        if not value:
            continue
        path = Path(value).expanduser()
        tried.append(f"{source} ({path})")
        if path.is_dir() and os.access(path, os.R_OK):
            return path.resolve()

    raise RuntimeError(
        "Could not resolve a readable dataset base directory. Tried: "
        + (", ".join(tried) if tried else "no candidates provided")
    )


def configure_paths(base):
    """
    Set the module-level base and derived directories from a resolved base,
    creating the output subdirectories. Returns the resolved base.
    """
    global BASE, REPORTS_DIR, CONTRACTS_WITH_SPECS, CONTRACTS_ONLY, SPEC_ONLY, MAPPING_FILE

    BASE = base
    REPORTS_DIR = BASE / "reports"
    CONTRACTS_WITH_SPECS = BASE / "contracts_with_specs"
    CONTRACTS_ONLY = BASE / "contracts_only"
    SPEC_ONLY = BASE / "spec_only"
    MAPPING_FILE = BASE / "repo_mapping.json"

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    CONTRACTS_WITH_SPECS.mkdir(parents=True, exist_ok=True)
    CONTRACTS_ONLY.mkdir(parents=True, exist_ok=True)
    SPEC_ONLY.mkdir(parents=True, exist_ok=True)

    return BASE


def log(msg, level=0):
    """Print message with optional indentation."""
    prefix = "  " * level
    print(f"{prefix}{msg}")
    sys.stdout.flush()


def run_gh(cmd):
    """Run a gh api command and return parsed JSON."""
    result = subprocess.run(
        ["gh", "api"] + cmd,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        if VERBOSE:
            log(f"gh error: {result.stderr}", 1)
        return None
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def list_all_reports():
    """Return list of (year, report_name, download_url) tuples."""
    reports = []
    years = ["2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026"]
    for year in years:
        data = run_rite(["repos/Certora/SecurityReports/contents/Reports/" + year])
        if not data:
            continue
        for item in data:
            if item["type"] == "file" and item["name"].endswith(".pdf"):
                reports.append({
                    "year": year,
                    "name": item["name"],
                    "download_url": item["download_url"],
                    "path": item["path"],
                })
    return reports


def run_rite(path_list):
    """Alias for run_gh with a path argument"""
    return run_gh(path_list)


def save_mapping(mapping):
    """Save repo mapping to JSON file."""
    with open(MAPPING_FILE, "w") as f:
        json.dump(mapping, f, indent=2)


def load_mapping():
    """Load repo mapping from JSON file."""
    if MAPPING_FILE.exists():
        with open(MAPPING_FILE) as f:
            return json.load(f)
    return {}


def download_reports(reports):
    """Download all report PDFs to reports/ dir."""
    for r in reports:
        dest = REPORTS_DIR / r["name"]
        if dest.exists():
            log(f"Skip (exists): {r['name']}")
            continue
        log(f"Downloading: {r['name']}")
        subprocess.run(["wget", "-q", r["download_url"], "-O", str(dest)], check=False)


def extract_github_url(pdf_path):
    """Extract GitHub URLs from a PDF report (both text and embedded annotations)."""
    import pdfplumber
    urls = set()
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                # 1. Extract from text
                text = page.extract_text()
                if text:
                    found = re.findall(r'https?://github\.com/[^\s)\]]+', text)
                    for u in found:
                        urls.add(u.rstrip('.').rstrip(',').rstrip(')'))
                # 2. Extract from annotations/hyperlinks (embedded links)
                if hasattr(page, 'annots') and page.annots:
                    for annot in page.annots:
                        if 'uri' in annot and annot['uri']:
                            uri = annot['uri']
                            if isinstance(uri, bytes):
                                uri = uri.decode('utf-8')
                            if 'github.com' in uri:
                                urls.add(uri.rstrip('.').rstrip(',').rstrip(')'))
    except Exception as e:
        if VERBOSE:
            log(f"Error reading {pdf_path}: {e}", 1)
    return urls


def parse_github_url(url):
    """
    Parse a GitHub URL into (owner, repo, branch, path).
    Handles:
      - https://github.com/owner/repo
      - https://github.com/owner/repo/tree/branch
      - https://github.com/owner/repo/tree/branch/path/to/dir
      - https://github.com/owner/repo/tree/commit-hash
      - https://github.com/owner/repo/blob/branch/file.spec
    """
    if "github.com/" not in url:
        return None

    parts = url.split("github.com/")[1].split("/")
    if len(parts) < 2:
        return None

    owner = parts[0]
    repo = parts[1]

    # Default values
    branch = None
    path = ""

    if len(parts) >= 3:
        kind = parts[2]  # "tree", "blob", or None
        if kind in ("tree", "blob") and len(parts) >= 4:
            branch = parts[3]
            if len(parts) >= 5:
                path = "/".join(parts[4:])
        elif kind == "commit" and len(parts) >= 4:
            branch = parts[3]  # commit hash
        elif kind == "pull" or kind == "issues":
            # Not a code URL
            return None
        elif kind and kind not in ("tree", "blob", "commit", "pull", "issues", "wiki", "settings"):
            # Could be a direct file without /tree/ or /blob/
            # e.g. github.com/owner/repo/somefile.sol
            branch = "main"  # Try default
            path = "/".join(parts[2:])

    return {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "path": path,
    }


def get_default_branch(repo_owner, repo_name):
    """Get the default branch for a repo."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo_owner}/{repo_name}", "--jq", ".default_branch"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        return "main"
    return result.stdout.strip() or "main"


def recursive_list_files(repo_owner, repo_name, branch=None, path=""):
    """
    Recursively list all files in a GitHub repo via the /contents API.
    Returns a list of file paths (strings).
    Tries the specified branch first, falls back to default branch.
    """
    if branch is None:
        branch = get_default_branch(repo_owner, repo_name)

    url = f"repos/{repo_owner}/{repo_name}/contents/{path}?ref={branch}"
    data = run_gh([url])
    if not data:
        # Try without ref (use default branch)
        url = f"repos/{repo_owner}/{repo_name}/contents/{path}"
        data = run_gh([url])
        if not data:
            return []

    files = []
    dirs = []
    for item in data:
        if item["type"] == "file":
            files.append(item["path"])
        elif item["type"] == "dir":
            # Skip test directories and common noise
            name = item["name"].lower()
            if name in ("test", "tests", "node_modules", "lib", ".git", "artifacts", "cache"):
                continue
            dirs.append(item["path"])

    # Recurse into subdirectories
    for d in dirs:
        files.extend(recursive_list_files(repo_owner, repo_name, branch, d))

    return files


def find_spec_pairs_in_tree(repo_owner, repo_name, branch=None, verbose=True):
    """
    Find .sol and .spec file pairs in a GitHub repo branch.
    Returns dict with 'pairs', 'lone_specs', 'lone_sols'.
    """
    if verbose:
        branch_label = branch or "(default)"
        log(f"    Traversing {repo_owner}/{repo_name} @{branch_label}...", 1)

    all_files = recursive_list_files(repo_owner, repo_name, branch)

    if verbose:
        log(f"    Found {len(all_files)} total files", 1)

    sol_files = [f for f in all_files if f.endswith(".sol")]
    spec_files = [f for f in all_files if f.endswith(".spec")]

    if verbose:
        log(f"    Found {len(sol_files)} .sol files, {len(spec_files)} .spec files", 1)

    pairs = []
    lone_specs = []

    for spec_path in spec_files:
        base = Path(spec_path).stem  # e.g. "Splits" from "Splits.spec"
        # Find .sol with same base name (case-insensitive match on filename)
        match = None
        for sol_path in sol_files:
            if Path(sol_path).stem.lower() == base.lower():
                match = sol_path
                break
        if match:
            pairs.append({"contract": match, "spec": spec_path})
        else:
            # No matching .sol in this branch; store spec alone
            lone_specs.append(spec_path)

    # Find .sol files that have no matching .spec
    paired_sol = set(p["contract"] for p in pairs)
    lone_sols = [f for f in sol_files if f not in paired_sol]

    return {
        "pairs": pairs,
        "lone_specs": lone_specs,
        "lone_sols": lone_sols,
    }


def download_file(repo_owner, repo_name, branch, file_path, dest):
    """Download a single file from GitHub raw content."""
    url = f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/{branch}/{file_path}"
    try:
        result = subprocess.run(
            ["wget", "-q", url, "-O", str(dest)],
            capture_output=True,
            text=True,
            check=False
        )
        return result.returncode == 0
    except Exception as e:
        if VERBOSE:
            log(f"    Error downloading {file_path}: {e}", 1)
        return False


def scrape_from_mapping(mapping, verbose=True):
    """
    Scrape repos from the standard repo_mapping.json.
    Uses known repo mappings and tries default branch.
    """
    global VERBOSE
    def old_scrape_repo(repo_owner, repo_name):
        """Old scraping logic for repos from KNOWN mappings (default branch)."""
        if verbose:
            log(f"  Scraping {repo_owner}/{repo_name} (default branch)", 1)
        branch = get_default_branch(repo_owner, repo_name)
        result = find_spec_pairs_in_tree(repo_owner, repo_name, branch, verbose)

        pairs_dir = CONTRACTS_WITH_SPECS / f"{repo_owner}_{repo_name}"
        lone_dir = CONTRACTS_ONLY / f"{repo_owner}_{repo_name}"
        spec_dir = SPEC_ONLY / f"{repo_owner}_{repo_name}"

        pairs_dir.mkdir(parents=True, exist_ok=True)
        lone_dir.mkdir(parents=True, exist_ok=True)
        spec_dir.mkdir(parents=True, exist_ok=True)

        # Download pairs
        for pair in result["pairs"]:
            spec_path = pair["spec"]
            contract_path = pair["contract"]

            # Download spec
            spec_dest = pairs_dir / Path(spec_path).name
            if download_file(repo_owner, repo_name, branch, spec_path, spec_dest):
                if verbose:
                    log(f"    ✓ Downloaded spec: {Path(spec_path).name}", 1)

            # Download contract
            contract_dest = pairs_dir / Path(contract_path).name
            if download_file(repo_owner, repo_name, branch, contract_path, contract_dest):
                if verbose:
                    log(f"    ✓ Downloaded contract: {Path(contract_path).name}", 1)

        # Download lone specs
        for spec_path in result["lone_specs"]:
            spec_dest = spec_dir / Path(spec_path).name
            if download_file(repo_owner, repo_name, branch, spec_path, spec_dest):
                if verbose:
                    log(f"    ✓ Downloaded lone spec: {Path(spec_path).name}", 1)

        # Download lone sols
        for sol_path in result["lone_sols"]:
            sol_dest = lone_dir / Path(sol_path).name
            if download_file(repo_owner, repo_name, branch, sol_path, sol_dest):
                if verbose:
                    log(f"    ✓ Downloaded lone contract: {Path(sol_path).name}", 1)

        return len(result["pairs"]), len(result["lone_specs"]), len(result["lone_sols"])

    total_pairs = 0
    total_lone_specs = 0
    total_lone_sols = 0

    for name, info in mapping.items():
        if not info["repo"]:
            if verbose:
                log(f"No repo mapping for {name}", 1)
            continue
        owner = info["repo"]["owner"]
        repo = info["repo"]["repo"]

        if verbose:
            log(f"\n=== {name} -> {owner}/{repo} ===")

        p, ls, lso = old_scrape_repo(owner, repo)
        total_pairs += p
        total_lone_specs += ls
        total_lone_sols += lso

        if verbose:
            log(f"  Summary: {p} pairs, {ls} lone specs, {lso} lone sols", 1)

    return total_pairs, total_lone_specs, total_lone_sols


def scrape_from_annotation_urls(mapping=None, verbose=True):
    """
    Scrape repos from annotation URLs in PDFs.
    Each URL points to a specific branch/path with specs.
    Tries the annotation branch first, then falls back to default branch.

    Reads URLs from repo_mapping.json (extracted_urls) to avoid re-parsing PDFs
    on every run (which is slow and spams FontBBox warnings).
    """
    global VERBOSE

    if mapping is None:
        mapping = load_mapping()

    all_annotation_urls = set()
    for name, info in mapping.items():
        for url in info.get("extracted_urls", []):
            if url:
                all_annotation_urls.add(url)

    if verbose:
        log(f"Collected {len(all_annotation_urls)} unique annotation URLs from mapping", 1)

    # Parse into (owner, repo, branch, path) tuples
    parsed_urls = []
    for url in all_annotation_urls:
        parsed = parse_github_url(url)
        if parsed and parsed["owner"] and parsed["repo"]:
            parsed_urls.append(parsed)

    if verbose:
        log(f"Parsed {len(parsed_urls)} GitHub URLs", 1)

    # De-duplicate by (owner, repo, branch)
    seen = set()
    unique_targets = []
    for p in parsed_urls:
        key = (p["owner"], p["repo"], p["branch"])
        if key not in seen:
            seen.add(key)
            unique_targets.append(p)

    if verbose:
        log(f"Unique (owner, repo, branch) targets: {len(unique_targets)}", 1)

    total_pairs = 0
    total_lone_specs = 0
    total_lone_sols = 0

    for target in unique_targets:
        owner = target["owner"]
        repo = target["repo"]
        branch = target["branch"]

        if verbose:
            log(f"\n=== {owner}/{repo} @{branch or '(default)'} ===")

        # Try annotation branch first
        result = find_spec_pairs_in_tree(owner, repo, branch, verbose)

        # If annotation branch failed (no files), try default branch
        if not result["pairs"] and not result["lone_specs"] and not result["lone_sols"]:
            if verbose:
                log(f"    Annotation branch failed, trying default branch...", 1)
            default_branch = get_default_branch(owner, repo)
            result = find_spec_pairs_in_tree(owner, repo, default_branch, verbose)
            branch = default_branch

        # Download files
        pairs_dir = CONTRACTS_WITH_SPECS / f"{owner}_{repo}"
        lone_dir = CONTRACTS_ONLY / f"{owner}_{repo}"
        spec_dir = SPEC_ONLY / f"{owner}_{repo}"

        pairs_dir.mkdir(parents=True, exist_ok=True)
        lone_dir.mkdir(parents=True, exist_ok=True)
        spec_dir.mkdir(parents=True, exist_ok=True)

        # Download pairs
        for pair in result["pairs"]:
            spec_path = pair["spec"]
            contract_path = pair["contract"]

            spec_dest = pairs_dir / Path(spec_path).name
            if download_file(owner, repo, branch, spec_path, spec_dest):
                if verbose:
                    log(f"    ✓ Spec: {Path(spec_path).name}", 1)

            contract_dest = pairs_dir / Path(contract_path).name
            if download_file(owner, repo, branch, contract_path, contract_dest):
                if verbose:
                    log(f"    ✓ Contract: {Path(contract_path).name}", 1)

        # Download lone specs
        for spec_path in result["lone_specs"]:
            spec_dest = spec_dir / Path(spec_path).name
            if download_file(owner, repo, branch, spec_path, spec_dest):
                if verbose:
                    log(f"    ✓ Lone spec: {Path(spec_path).name}", 1)

        # Download lone sols
        for sol_path in result["lone_sols"]:
            sol_dest = lone_dir / Path(sol_path).name
            if download_file(owner, repo, branch, sol_path, sol_dest):
                if verbose:
                    log(f"    ✓ Lone contract: {Path(sol_path).name}", 1)

        total_pairs += len(result["pairs"])
        total_lone_specs += len(result["lone_specs"])
        total_lone_sols += len(result["lone_sols"])

        if verbose:
            log(f"  Summary: {len(result['pairs'])} pairs, {len(result['lone_specs'])} lone specs, {len(result['lone_sols'])} lone sols", 1)

    return total_pairs, total_lone_specs, total_lone_sols


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="Certora dataset scraper v2")
    parser.add_argument("--list-reports", action="store_true")
    parser.add_argument("--download-reports", action="store_true")
    parser.add_argument("--map-repos", action="store_true")
    parser.add_argument("--scrape-all", action="store_true", help="Scrape from repo_mapping.json (default branches)")
    parser.add_argument("--scrape-urls", action="store_true", help="Scrape from PDF annotation URLs (specific branches)")
    parser.add_argument("--verbose", action="store_true", help="Show detailed output")
    parser.add_argument("--year", default=None, help="Filter by year")
    parser.add_argument(
        "--base",
        default=None,
        help="Dataset base directory. Falls back to the CERTORA_DATASET_ROOT "
             "environment variable, then the repository root inferred from this "
             "module's location.",
    )

    args = parser.parse_args()
    VERBOSE = args.verbose

    configure_paths(resolve_base(args.base))

    if args.list_reports:
        reports = list_all_reports()
        for r in reports:
            log(f"{r['year']}/{r['name']}")
        log(f"\nTotal: {len(reports)} reports")
        return

    if args.download_reports:
        reports = list_all_reports()
        if args.year:
            reports = [r for r in reports if r["year"] == args.year]
        download_reports(reports)
        log(f"Downloaded {len(reports)} reports to {REPORTS_DIR}")
        return

    if args.map_repos:
        reports = list_all_reports()
        mapping = build_repo_mapping(reports)
        save_mapping(mapping)
        mapped = sum(1 for v in mapping.values() if v["repo"])
        log(f"Mapped {mapped}/{len(mapping)} reports")
        for name, info in mapping.items():
            if info["repo"]:
                log(f"  {name} -> {info['repo']['owner']}/{info['repo']['repo']}")
            else:
                log(f"  {name} -> NO MAPPING")
        return

    if args.scrape_all:
        mapping = load_mapping()
        if not mapping:
            log("No mapping found. Run --map-repos first.", 1)
            return

        if VERBOSE:
            log("=== Scraping from repo_mapping.json (default branches) ===")
        total_pairs, total_lone_specs, total_lone_sols = scrape_from_mapping(mapping, VERBOSE)
        log(f"\n=== Summary (scrape-all) ===")
        log(f"Total contract/spec pairs: {total_pairs}")
        log(f"Total lone specs: {total_lone_specs}")
        log(f"Total lone contracts: {total_lone_sols}")
        return

    if args.scrape_urls:
        if VERBOSE:
            log("=== Scraping from PDF annotation URLs (specific branches) ===")
        mapping = load_mapping()
        if not mapping:
            log("No mapping found. Run --map-repos first.", 1)
            return
        total_pairs, total_lone_specs, total_lone_sols = scrape_from_annotation_urls(mapping, VERBOSE)
        log(f"\n=== Summary (scrape-urls) ===")
        log(f"Total contract/spec pairs: {total_pairs}")
        log(f"Total lone specs: {total_lone_specs}")
        log(f"Total lone contracts: {total_lone_sols}")
        return

    parser.print_help()


# Import functions from original scraper for backward compatibility
from importlib import import_module
import sys as _sys

# Re-define build_repo_mapping and save_mapping/load_mapping if not present
if "build_repo_mapping" not in globals():
    def build_repo_mapping(reports):
        """
        Build mapping of report -> GitHub repo.
        First tries to extract URLs from PDFs, then falls back to known mappings.
        """
        mapping = {}

        # Known mappings from project name -> GitHub repo (curated)
        KNOWN = {
            "radicle": ("drips-network", "contracts"),
            "aave": ("aave", "aave-v3-core"),
            "lido": ("lidofinance", "lido-dao"),
            "compound": ("compound-finance", "compound-protocol"),
            "balancer": ("balancer", "balancer-v3-monorepo"),
            "sushi": ("sushiswap", "sushiswap"),
            "makerdao": ("makerdao", "dss"),
            "synthetix": ("Synthetixio", "synthetix"),
            "uniswap": ("Uniswap", "v4-core"),
            "euler": ("euler-xyz", "euler-vaults"),
            "curve": ("curvefi", "curve-contract"),
            "layerzero": ("LayerZero-Labs", "LayerZero-v2"),
            "gmx": ("gmxpv", "gmx-helper"),
            "coinbase": ("coinbase", "smart-wallet"),
            "frax": ("fraxfinance", "frax-bamm"),
            "liquity": ("liquity", "bold"),
            "silo": ("silo-finance", "silo-v2"),
            "jito": ("jito-foundation", "jito-restaking"),
            "symbiotic": ("symbioticfi", "core"),
            "ether.fi": ("etherfi-protocol", "etherfi-contracts"),
            "kamino": ("Kamino-Finance", "kamino-lending"),
            "paraswap": ("paraswap", "Paraswap-v6"),
            "tokemak": ("Tokenlon", "tokemak-v2"),
            "tether": ("tetherto", "USDt"),
            "squads": ("sqds", "squads-v4"),
            "gyro": ("balancer", "gyroscope-pools-v2"),
            "mzero": ("m-zero-labs", "protocol-contracts"),
            "seamless": ("SeamlessProtocol", "seamless-protocol-contracts"),
            "cables": ("cables-protocol", "cables-contracts"),
            "huma": ("humanedefi", "huma-contracts"),
            "slender": ("slender-finance", "slender-contracts"),
            "manifest": ("manifest-protocol", "manifest-contracts"),
            "glow": ("glow-labs", "glow-contracts"),
            "sonic": ("Sonic-Network", "gateway"),
            "fragmetric": ("fragmetric", "fragmetric-contracts"),
            "alluvial": ("alluvial", "liquid-collective"),
            "reflector": ("reflector-finance", "reflector-contracts"),
            "eigenlayer": ("Layer-Edge", "eigenlayer-contracts"),
            "ajna": ("ajna-finance", "ajna"),
            "blend": ("blend-capital", "blend-v1"),
            "delv_hyperdrive": ("delvtech", "hyperdrive"),
            "protofire": ("protofire", "launchpad"),
            "kinto": ("kintoxyz", "kinto"),
            "tether_token": ("tetherto", "USDt"),
            "origin_dollar": ("OriginProtocol", "origin-dollar"),
            "lulo": ("lulolabs", "lulo"),
            "veda": ("veda", "veda-contracts"),
            "relend": ("relend-network", "relend-contracts"),
            "cozy": ("cozy-finance", "cozy-protocol"),
            "kleros": ("kleros", "kleros-v2"),
            "suilend": ("suilend", "suilend-protocol"),
            "templar": ("templar", "templar-contracts"),
            "cozy_finance": ("cozy-finance", "cozy-protocol"),
            "1delta": ("1delta", "1delta-contracts"),
            "calastone": ("calastone", "ctd-contracts"),
            "light": ("lightprotocol", "light-contracts"),
            "orbt": ("orbt-finance", "orbt-contracts"),
            "apyx": ("apyx-finance", "apxUSD"),
            "moviepass": ("moviepass", "msx-contracts"),
            "whetstone": ("whetstone", "doppler"),
            "twamm": ("twamm", "twamm-hook"),
            "dcspark": ("dcspark", "sidechain-bridge"),
            "traderjoe": ("traderjoe-xyz", "rocket-joe"),
            "benqi": ("Benqi", "benqi-contracts"),
            "lyra": ("lyra-finance", "lyra-option-market"),
            "notional": ("notional-finance", "notional-monorepo"),
            "opens Zeppelin": ("OpenZeppelin", "openzeppelin-contracts"),
            "furucombo": ("Furucombo", "furucombo-contracts"),
            "celo": ("celo-org", "celo-monorepo"),
            "orchid": ("OrchidTechnologies", "orchid"),
            "keep": ("keep-network", "keep-core"),
            "opyn": ("opynfinance", "gamma-protocol"),
            "dforce": ("dforce-xyz", "dforce-protocol"),
            "popsicle": ("Popsicle-Finance", "v3-optimizer"),
            "rolla": ("rolla-finance", "rolla-contracts"),
            "pintu": ("pintu", "pintu-token"),
            "zesty": ("zesty-market", "zesty-contracts"),
            "origin_ousd": ("OriginProtocol", "origin-dollar"),
            "openzeppelin_governance": ("OpenZeppelin", "openzeppelin-contracts"),
            "openzeppelin_erc1155_extensions": ("OpenZeppelin", "openzeppelin-contracts"),
            "gho_stability_module": ("aave", "aave-v3-core"),
            "token2022fv": ("solana-labs", "solana-program-library"),
            "aquarius": ("balancer", "balancer-v3-monorepo"),
            "mayan_fastmctp": ("mayan-finance", "mayan-contracts"),
            "texture2.0": ("texture-money", "texture-contracts"),
            "claynosaurz_nft": ("claynosaurz", "claynosaurz-contracts"),
            "polygon_vault_bridge": ("maticnetwork", "polygon-vault"),
            "safe": ("safe-global", "safe-smart-account"),
            "1inch_cross_chain_swap": ("1inch", "1inch-contracts"),
            "grove_alm": ("grove-defi", "grove-contracts"),
            "reserve_fixed_lib": ("reserve-protocol", "reserve-contracts"),
            "tradeport_gold_strategy": ("tradeport", "tradeport-contracts"),
            "umia": ("umia-finance", "umia-contracts"),
            "sem_finance": ("sem-finance", "sem-contracts"),
            "saturn_dollar": ("saturn-finance", "saturn-contracts"),
            "den_mls_wallet": ("den-finance", "den-contracts"),
            "spark_alm_controller": ("spark-protocol", "spark-contracts"),
            "reytscad": ("reytscad", "reytscad-contracts"),
            "solana": ("solana-labs", "solana-program-library"),
            "certora_spectrabridge": ("certora", "SpectraBridge"),
            "royco_dawn": ("royco", "royco-contracts"),
            "spectra_bridge": ("spectra", "spectra-contracts"),
            "mezzanine": ("mezzanine-finance", "mezzanine-contracts"),
            "royco": ("royco", "royco-contracts"),
        }

        for r in reports:
            name = r["name"].lower()
            m = re.match(r'\d{2}_\d{2}_\d{4}_(.+?)(?:-FV|-MR|-FV-MR|-DesignReview|-Coverage)?\.pdf', name)
            project = m.group(1) if m else name

            repo = None
            for key, val in KNOWN.items():
                if key in project:
                    repo = {"owner": val[0], "repo": val[1]}
                    break

            pdf_path = REPORTS_DIR / r["name"]
            extracted = set()
            if pdf_path.exists():
                extracted = extract_github_url(pdf_path)

            mapping[r["name"]] = {
                "project": project,
                "year": r["year"],
                "repo": repo,
                "extracted_urls": list(extracted),
            }

        return mapping


if __name__ == "__main__":
    main()
