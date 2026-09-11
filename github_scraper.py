#!/usr/bin/env python3
"""
GitHub Repo Scraper for Solidity & Certora Spec Files

Takes a GitHub repo URL, scans the entire repo tree via the GitHub API,
finds all .sol and .spec files, and downloads them into this directory.

Usage:
  # Single repo
  python3 github_scraper.py https://github.com/owner/repo
  python3 github_scraper.py https://github.com/owner/repo --flat
  python3 github_scraper.py https://github.com/owner/repo --branch dev
  python3 github_scraper.py https://github.com/owner/repo --dry-run

  # Batch mode from repo_mapping.json
  python3 github_scraper.py --from-mapping
  python3 github_scraper.py --from-mapping --dry-run
  python3 github_scraper.py --from-mapping --skip-existing
  python3 github_scraper.py --from-mapping --mapping-file /path/to/mapping.json

Requirements:
  - `gh` CLI installed and authenticated (https://cli.github.com/)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
DEFAULT_MAPPING = BASE_DIR / "repo_mapping.json"
RESULTS_LOG = BASE_DIR / "scrape_results.json"


# ── GitHub API helpers ──────────────────────────────────────────────────────

def gh_api(endpoint, jq=None):
    """Call `gh api` and return parsed JSON."""
    cmd = ["gh", "api", endpoint]
    if jq:
        cmd += ["--jq", jq]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[ERROR] gh api {endpoint}: {result.stderr.strip()}", file=sys.stderr)
        return None
    if jq:
        return result.stdout.strip()
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()


def parse_github_url(url):
    """
    Parse a GitHub URL into (owner, repo, branch).
    Supports:
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/branch/path
      https://github.com/owner/repo/commit/sha
      github.com/owner/repo
      owner/repo
    Returns (owner, repo, branch_or_None).
    """
    url = url.strip().rstrip("/")

    # Full URL with tree/branch
    m = re.match(
        r"(?:https?://)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/tree/([^/]+)(?:/.*)?$",
        url,
    )
    if m:
        return m.group(1), m.group(2), m.group(3)

    # URL with commit SHA
    m = re.match(
        r"(?:https?://)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/commit/([a-f0-9]+)$",
        url,
    )
    if m:
        return m.group(1), m.group(2), m.group(3)

    # Plain URL (no branch)
    m = re.match(
        r"(?:https?://)?github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/.*)?$", url
    )
    if m:
        return m.group(1), m.group(2), None

    # Short form: owner/repo
    m = re.match(r"^([^/]+)/([^/]+)$", url)
    if m:
        return m.group(1), m.group(2), None

    return None, None, None


def get_default_branch(owner, repo):
    """Fetch the default branch name for a repo."""
    branch = gh_api(f"repos/{owner}/{repo}", jq=".default_branch")
    return branch if branch else "main"


def get_repo_tree(owner, repo, branch):
    """
    Use the Git Trees API with ?recursive=1 to fetch the ENTIRE file tree
    in a single API call. Much faster than recursive /contents calls.
    Returns a list of file path strings.
    """
    data = gh_api(f"repos/{owner}/{repo}/git/trees/{branch}?recursive=1")
    if not data or not isinstance(data, dict) or "tree" not in data:
        print(f"[ERROR] Could not fetch tree for {owner}/{repo}@{branch}", file=sys.stderr)
        return []

    if data.get("truncated"):
        print("[WARNING] Tree was truncated (repo is very large). Some files may be missing.", file=sys.stderr)

    return [item["path"] for item in data["tree"] if item["type"] == "blob"]


# ── Download logic ──────────────────────────────────────────────────────────

def download_file(owner, repo, branch, file_path, dest_path):
    """Download a single file from GitHub via raw.githubusercontent.com."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        ["wget", "-q", url, "-O", str(dest_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  [FAIL] {file_path}: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def scrape_single_repo(owner, repo, branch=None, flat=False, dry_run=False, output_dir=None):
    """
    Scrape a single repo for .sol and .spec files.
    Returns a dict with stats: {owner, repo, branch, sol_count, spec_count, success, failed}
    """
    print(f"\n{'─' * 60}")
    print(f"  Repo:   {owner}/{repo}")

    # ── Resolve branch
    if not branch:
        branch = get_default_branch(owner, repo)
    print(f"  Branch: {branch}")

    # ── Fetch tree
    print(f"  ⏳ Fetching repo tree...")
    all_files = get_repo_tree(owner, repo, branch)
    if not all_files:
        print(f"  [SKIP] No files found or API error for {owner}/{repo}", file=sys.stderr)
        return {
            "owner": owner, "repo": repo, "branch": branch,
            "sol_count": 0, "spec_count": 0, "success": 0, "failed": 0,
            "status": "error",
        }

    print(f"  Total files in repo: {len(all_files)}")

    # ── Filter .sol and .spec
    # Exclude .sol files under lib/, test/, script/, foundry/ directories (including nested)
    excluded_dirs = ("lib", "test", "script", "foundry", "tests", "mocks", "dependencies", "openzeppelin", "libraries")
    sol_files = sorted([
        f for f in all_files
        if f.endswith(".sol") and not any(f.startswith(d) or f"{d}" in f for d in excluded_dirs)
    ])
    spec_files = sorted([f for f in all_files if f.endswith(".spec")])

    print(f"  📄 {len(sol_files)} .sol files | 📋 {len(spec_files)} .spec files")

    if not sol_files and not spec_files:
        print(f"  ⚠️  No .sol or .spec files found.")
        return {
            "owner": owner, "repo": repo, "branch": branch,
            "sol_count": 0, "spec_count": 0, "success": 0, "failed": 0,
            "status": "no_files",
        }

    # ── Dry run
    if dry_run:
        if sol_files:
            print(f"  .sol files:")
            for f in sol_files:
                print(f"    {f}")
        if spec_files:
            print(f"  .spec files:")
            for f in spec_files:
                print(f"    {f}")
        return {
            "owner": owner, "repo": repo, "branch": branch,
            "sol_count": len(sol_files), "spec_count": len(spec_files),
            "success": 0, "failed": 0, "status": "dry_run",
        }

    # ── Determine output directory
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = BASE_DIR / "Raw_Scraper" / f"{owner}_{repo}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Determine paired output directory (for model training)
    paired_dir = BASE_DIR / "Paired_Dataset" / f"{owner}_{repo}"
    paired_dir.mkdir(parents=True, exist_ok=True)

    # ── Download
    target_files = sol_files + spec_files
    success = 0
    failed = 0

    for i, file_path in enumerate(target_files, 1):
        ext = ".sol" if file_path.endswith(".sol") else ".spec"
        tag = "SOL " if ext == ".sol" else "SPEC"

        if flat:
            dest = out_dir / Path(file_path).name
            # Handle name collisions in flat mode
            if dest.exists():
                stem = Path(file_path).stem
                suffix = Path(file_path).suffix
                counter = 1
                while dest.exists():
                    dest = out_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
        else:
            dest = out_dir / file_path

        print(f"  [{i:3d}/{len(target_files)}] {tag} {file_path}")
        if download_file(owner, repo, branch, file_path, dest):
            success += 1
        else:
            failed += 1

    print(f"  ✅ {success} downloaded" + (f" | ❌ {failed} failed" if failed else ""))

    # ── Create paired dataset (only .spec files with matching .sol)
    # Match by filename stem (e.g., Contract.sol ↔ Contract.spec)
    sol_stems = {Path(f).stem for f in sol_files}
    paired_count = 0
    for spec_file in spec_files:
        spec_stem = Path(spec_file).stem
        if spec_stem in sol_stems:
            # Find the matching .sol file
            matching_sol = next((f for f in sol_files if Path(f).stem == spec_stem), None)
            if matching_sol:
                # Download both to paired directory (preserving relative paths)
                for src_file in [matching_sol, spec_file]:
                    dest = paired_dir / src_file
                    if download_file(owner, repo, branch, src_file, dest):
                        paired_count += 1

    print(f"  🔗 {paired_count // 2} paired .sol/.spec files copied to Paired_Dataset/")

    return {
        "owner": owner, "repo": repo, "branch": branch,
        "sol_count": len(sol_files), "spec_count": len(spec_files),
        "success": success, "failed": failed,
        "paired_count": paired_count // 2,
        "status": "done",
        "output_dir": str(out_dir),
        "paired_dir": str(paired_dir),
    }


# ── Mapping mode ────────────────────────────────────────────────────────────

def extract_repos_from_mapping(mapping_path):
    """
    Read repo_mapping.json and extract unique (owner, repo, branch) tuples.

    Priority:
      1. extracted_urls — these come from the actual PDFs and are more accurate
      2. curated repo field — fallback if no extracted_urls exist

    Returns a list of dicts: [{owner, repo, branch, sources: [report_names]}]
    """
    with open(mapping_path) as f:
        mapping = json.load(f)

    # Deduplicate by (owner, repo) — keep track of which reports reference each
    repo_map = {}  # key: (owner_lower, repo_lower) -> {owner, repo, branch, sources}

    for report_name, info in mapping.items():
        sources_added = False

        # First: try extracted_urls (more accurate)
        for url in info.get("extracted_urls", []):
            owner, repo, branch = parse_github_url(url)
            if owner and repo:
                key = (owner.lower(), repo.lower())
                if key not in repo_map:
                    repo_map[key] = {
                        "owner": owner,
                        "repo": repo,
                        "branch": branch,  # may be None
                        "sources": [],
                    }
                # If this URL has a branch and existing entry doesn't, update it
                if branch and not repo_map[key]["branch"]:
                    repo_map[key]["branch"] = branch
                repo_map[key]["sources"].append(report_name)
                sources_added = True

        # Fallback: curated repo field (only if no extracted_urls yielded a repo)
        if not sources_added and info.get("repo"):
            owner = info["repo"]["owner"]
            repo = info["repo"]["repo"]
            key = (owner.lower(), repo.lower())
            if key not in repo_map:
                repo_map[key] = {
                    "owner": owner,
                    "repo": repo,
                    "branch": None,
                    "sources": [],
                }
            repo_map[key]["sources"].append(report_name)

    # Sort by owner/repo for consistent ordering
    repos = sorted(repo_map.values(), key=lambda r: (r["owner"].lower(), r["repo"].lower()))
    return repos


def run_from_mapping(mapping_path, flat=False, dry_run=False, skip_existing=False):
    """Batch scrape all repos from repo_mapping.json."""
    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║  Batch Scraper — from repo_mapping.json                 ║")
    print(f"╚══════════════════════════════════════════════════════════╝")
    print(f"  Mapping: {mapping_path}")

    repos = extract_repos_from_mapping(mapping_path)
    print(f"  Unique repos found: {len(repos)}")
    print()

    # Load previous results if they exist (for resume)
    results = {}
    if RESULTS_LOG.exists():
        with open(RESULTS_LOG) as f:
            results = json.load(f)

    total_sol = 0
    total_spec = 0
    total_success = 0
    total_failed = 0
    total_paired = 0
    skipped = 0
    errors = []

    for idx, repo_info in enumerate(repos, 1):
        owner = repo_info["owner"]
        repo = repo_info["repo"]
        branch = repo_info["branch"]
        repo_key = f"{owner}/{repo}"

        print(f"\n[{idx}/{len(repos)}] ═══ {repo_key} ═══")

        # Skip if already downloaded
        out_dir = BASE_DIR / "Raw_Scraper" / f"{owner}_{repo}"
        if skip_existing and out_dir.exists():
            # Check if it has any .sol or .spec files
            existing = list(out_dir.rglob("*.sol")) + list(out_dir.rglob("*.spec"))
            if existing:
                print(f"  ⏭️  Skipping (already exists with {len(existing)} files)")
                skipped += 1
                continue

        # Scrape
        try:
            stats = scrape_single_repo(
                owner, repo, branch=branch,
                flat=flat, dry_run=dry_run, output_dir=str(out_dir),
            )
        except Exception as e:
            print(f"  [ERROR] {e}", file=sys.stderr)
            stats = {
                "owner": owner, "repo": repo, "branch": branch or "unknown",
                "sol_count": 0, "spec_count": 0, "success": 0, "failed": 0,
                "status": "exception", "error": str(e),
            }
            errors.append(repo_key)

        total_sol += stats.get("sol_count", 0)
        total_spec += stats.get("spec_count", 0)
        total_success += stats.get("success", 0)
        total_failed += stats.get("failed", 0)
        total_paired += stats.get("paired_count", 0)

        # Save result
        results[repo_key] = stats

        # Persist results after each repo (resume-friendly)
        if not dry_run:
            with open(RESULTS_LOG, "w") as f:
                json.dump(results, f, indent=2)

        # Be nice to the GitHub API
        if not dry_run:
            time.sleep(0.5)

    # ── Final summary
    print(f"\n{'═' * 60}")
    print(f"  BATCH SCRAPE {'(DRY RUN) ' if dry_run else ''}COMPLETE")
    print(f"{'═' * 60}")
    print(f"  Total repos processed: {len(repos) - skipped}")
    if skipped:
        print(f"  Skipped (existing):    {skipped}")
    print(f"  Total .sol files:      {total_sol}")
    print(f"  Total .spec files:     {total_spec}")
    if not dry_run:
        print(f"  Downloaded:            {total_success}")
        print(f"  Paired .sol/.spec:     {total_paired}")
        if total_failed:
            print(f"  Failed:                {total_failed}")
        print(f"  Results log:           {RESULTS_LOG}")
    if errors:
        print(f"\n  ⚠️  Errors in {len(errors)} repos:")
        for e in errors:
            print(f"    - {e}")
    print(f"{'═' * 60}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape GitHub repos for .sol and .spec files"
    )

    # Single repo mode
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="GitHub repo URL (e.g. https://github.com/owner/repo)",
    )

    # Batch mode from mapping
    parser.add_argument(
        "--from-mapping",
        action="store_true",
        help="Batch scrape all repos from repo_mapping.json",
    )
    parser.add_argument(
        "--mapping-file",
        default=None,
        help=f"Path to repo_mapping.json (default: {DEFAULT_MAPPING})",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip repos that already have a download directory with files",
    )

    # Common options
    parser.add_argument(
        "--branch",
        default=None,
        help="Branch to scrape (default: repo's default branch)",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help="Download all files into a single flat directory (no subdirs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching files without downloading",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Custom output directory (single repo mode only)",
    )

    args = parser.parse_args()

    # ── Batch mode ──────────────────────────────────────────────────────
    if args.from_mapping:
        mapping_path = args.mapping_file or str(DEFAULT_MAPPING)
        if not Path(mapping_path).exists():
            print(f"[ERROR] Mapping file not found: {mapping_path}", file=sys.stderr)
            sys.exit(1)
        run_from_mapping(
            mapping_path,
            flat=args.flat,
            dry_run=args.dry_run,
            skip_existing=args.skip_existing,
        )
        return

    # ── Single repo mode ────────────────────────────────────────────────
    if not args.url:
        parser.print_help()
        sys.exit(1)

    owner, repo, url_branch = parse_github_url(args.url)
    if not owner or not repo:
        print(f"[ERROR] Could not parse GitHub URL: {args.url}", file=sys.stderr)
        sys.exit(1)

    # CLI --branch overrides URL-extracted branch
    branch = args.branch or url_branch

    print(f"╔══════════════════════════════════════════════════════╗")
    print(f"║  GitHub Repo Scraper — .sol & .spec files           ║")
    print(f"╚══════════════════════════════════════════════════════╝")

    stats = scrape_single_repo(
        owner, repo,
        branch=branch,
        flat=args.flat,
        dry_run=args.dry_run,
        output_dir=args.output_dir,
    )

    print(f"\n{'═' * 55}")
    print(f"📁 Raw_Scraper:  {stats.get('output_dir', BASE_DIR / 'Raw_Scraper' / f'{owner}_{repo}')}")
    print(f"🔗 Paired_Dataset: {stats.get('paired_dir', BASE_DIR / 'Paired_Dataset' / f'{owner}_{repo}')}")
    print(f"{'═' * 55}")


if __name__ == "__main__":
    main()
