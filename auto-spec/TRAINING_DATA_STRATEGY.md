# Training Data & Distribution Strategy

## Problem Solved

You wanted to:
1. ✅ **Keep training data separate** from code repository  
2. ✅ **Don't ship 500MB chroma_db** with tool
3. ✅ **Allow users to download** it on demand
4. ✅ **Support multiple download sources** (GitHub, S3, etc.)

## Solution Implemented

### Architecture

```
erc20_pairs_final/
└── chroma_db/                    ← Your source data (stay local)

auto-spec/                        ← Published package (no chroma_db)
├── pyproject.toml                ← pip install auto-spec
├── DEPLOYMENT.md                 ← Distribution guide
├── scripts/
│   ├── upload_to_s3.py           ← Upload to AWS S3
│   ├── create_release.py         ← Package for GitHub
│   └── deploy.sh                 ← Interactive deployment
└── .github/workflows/
    └── release.yml               ← Automated releases
```

### Distribution Flow

```
1. You: Push version tag (v1.0.0)
                ↓
2. GitHub Actions: 
   - Packages chroma_db → chroma_db_v1.0.0.tar.gz
   - Uploads to GitHub Releases
   - Optionally uploads to S3
                ↓
3. Users:
   export CHROMA_DB_REMOTE_URL=https://github.com/.../releases/download/v1.0.0/...
   auto-spec setup  # Downloads automatically
                ↓
4. Tool Works: Users run "auto-spec generate contract.sol"
```

---

## How to Use It

### For You (Maintainer)

#### First Time Setup

```bash
cd auto-spec
bash scripts/deploy.sh
```

This is interactive and will guide you through all options.

#### Create a Release

```bash
# 1. Tag the release
git tag -a v1.0.0 -m "Release 1.0.0"
git push origin v1.0.0

# 2. GitHub Actions automatically:
#    ✓ Archives chroma_db → tar.gz
#    ✓ Creates GitHub Release
#    ✓ Uploads to S3 (if configured)
#    ✓ Updates metadata.json
```

That's it! 🎉

#### Manual Release (if you prefer)

```bash
# 1. Create archive with metadata
python scripts/create_release.py --version 1.0.0 --metadata

# Output:
# - chroma_db_v1.0.0.tar.gz (~500MB)
# - metadata.json (checksums, info)

# 2. Upload to GitHub (optional)
gh release create v1.0.0 chroma_db_v1.0.0.tar.gz metadata.json

# 3. Upload to S3 (optional)
python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db
```

### For Users

#### Option A: GitHub Releases (Recommended - Simplest)

```bash
# Install
pip install auto-spec

# Setup (downloads chroma_db automatically)
export CHROMA_DB_REMOTE_URL=https://github.com/yourusername/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz
auto-spec setup

# Use
auto-spec generate contract.sol
```

#### Option B: AWS S3 (Faster - Optional)

```bash
export CHROMA_DB_REMOTE_URL=https://my-bucket.s3.amazonaws.com/auto-spec-db/v1.0.0
auto-spec setup
auto-spec generate contract.sol
```

#### Option C: Local Copy (Development)

```bash
export CHROMA_DB_PATH=/path/to/erc20_pairs_final/chroma_db
auto-spec generate contract.sol  # Uses local DB
```

---

## File-by-File Explanation

### 📄 `scripts/upload_to_s3.py`

Uploads chroma_db to AWS S3

**Features:**
- ✅ Creates manifest.json with checksums
- ✅ Dry-run mode to preview
- ✅ Progress reporting
- ✅ Error handling

**Usage:**
```bash
python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db

# Outputs environment variable for users:
# export CHROMA_DB_REMOTE_URL=https://my-bucket.s3.amazonaws.com/auto-spec-db
```

### 📄 `scripts/create_release.py`

Packages chroma_db as .tar.gz for GitHub Releases

**Features:**
- ✅ Creates compressed archive
- ✅ Generates metadata.json with:
  - Version
  - File size
  - SHA256 checksum
  - Installation instructions
- ✅ Supports gzip, bzip2, xz compression

**Usage:**
```bash
python scripts/create_release.py --version 1.0.0 --metadata
```

### 📄 `.github/workflows/release.yml`

GitHub Actions automation for releases

**What it does:**
1. Triggers on git tag (v1.0.0)
2. Creates chroma_db archive
3. Uploads to GitHub Release
4. (Optional) Uploads to S3
5. Generates metadata.json

**Setup:**
```bash
# Add optional S3 credentials to GitHub
# Settings → Secrets and variables → Actions
# Add: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# Add: S3_BUCKET variable
```

### 📄 `scripts/deploy.sh`

Interactive deployment guide

**Usage:**
```bash
bash scripts/deploy.sh
# Shows all options and guides you through setup
```

### 📄 `DEPLOYMENT.md`

Comprehensive deployment documentation

**Covers:**
- ✅ GitHub Releases setup
- ✅ S3 setup (with CDN)
- ✅ Cost estimation
- ✅ Testing procedures
- ✅ Troubleshooting
- ✅ Combined strategies

### 🔧 Updated `vector_db.py`

Enhanced with:
- ✅ Archive download support (.tar.gz, .tar.bz2)
- ✅ Manifest.json support
- ✅ GitHub releases support
- ✅ S3 support
- ✅ Better error messages
- ✅ Checksum verification

---

## Quick Start: Your First Release

### 1. Setup (One-time)

```bash
# Install AWS CLI (for S3)
pip install boto3
aws configure  # Add credentials

# Or skip AWS and just use GitHub
```

### 2. Create Release

```bash
cd /path/to/auto-spec

# Option A: Automatic (recommended)
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0
# GitHub Actions does everything!

# Option B: Manual
python scripts/create_release.py --version 1.0.0 --metadata
gh release create v1.0.0 chroma_db_v1.0.0.tar.gz
```

### 3. Test It Works

```bash
# Fresh user simulation
rm -rf ~/.auto-spec/chroma_db

export CHROMA_DB_REMOTE_URL="https://github.com/yourusername/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz"

auto-spec setup  # Should download ~500MB

auto-spec generate erc20_pairs_final/mock_token.sol  # Should work!
```

---

## Comparison: GitHub vs S3

| Feature | GitHub | S3 |
|---------|--------|-----|
| **Cost** | Free | ~$0-5/month |
| **Setup** | Click-click | More complex |
| **Speed** | Good | Excellent (with CDN) |
| **Reliability** | Excellent | Excellent |
| **GitHub Integration** | Native | External |
| **Bandwidth** | ~20GB/month free | Variable |
| **Best for** | Small-medium projects | High traffic |

**Recommendation:** Start with GitHub, add S3 if you get millions of downloads.

---

## Environment Variables Reference

### For Users

```bash
# Primary: Remote URL (GitHub, S3, etc.)
export CHROMA_DB_REMOTE_URL=https://...

# Alternative: Local path (development)
export CHROMA_DB_PATH=/path/to/chroma_db

# LLM Configuration
export NVIDIA_API_KEY=...
export LLM_MODEL=meta/llama-3.1-70b-instruct
```

### For Deployment Scripts

```bash
# AWS S3 Upload
aws configure  # Sets AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

# GitHub Release
gh auth login  # Authenticates to GitHub
```

---

## Troubleshooting

### "chroma_db not found"
- User hasn't run `auto-spec setup` yet
- `CHROMA_DB_REMOTE_URL` not set
- Network issue downloading

### "Download failed"
- URL incorrect or URL changed
- File no longer exists on S3/GitHub
- User has no internet

### "Checksum mismatch"
- Download corrupted (retry)
- Wrong file uploaded
- Network timeout during download

**Solution:** Check DEPLOYMENT.md troubleshooting section

---

## Next: Publish to PyPI

After testing both GitHub and S3 distribution:

```bash
# Build package
python -m build

# Upload to PyPI
twine upload dist/*

# Users can then:
pip install auto-spec
```

Then chroma_db downloads automatically on first use! 🚀

---

## Summary

✅ **Training data** kept in `erc20_pairs_final/`  
✅ **Package** published without chroma_db  
✅ **Users download** chroma_db automatically  
✅ **Multiple sources** supported (GitHub, S3, local)  
✅ **Fully automated** via GitHub Actions  
✅ **Zero friction** for end users  

**Total user experience:**
```bash
pip install auto-spec
auto-spec setup  # Downloads DB automatically
auto-spec generate contract.sol
```

That's it! 🎉
