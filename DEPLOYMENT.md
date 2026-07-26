# Deployment Guide

This guide covers how to deploy auto-spec and its vector database (chroma_db) for users.

## Overview

There are **two recommended deployment methods**:

1. **GitHub Releases** - Built-in, free, simple
2. **AWS S3** - Faster downloads, CDN optional, more control

You can use one, both, or combine them with fallbacks.

---

## Method 1: GitHub Releases

### Pros
- ✅ Built-in to GitHub
- ✅ Free hosting
- ✅ Automatic via Actions
- ✅ No AWS account needed

### Cons
- ⚠️ ~500MB per release (GitHub has no size limits for releases)
- ⚠️ Slightly slower than S3

### Setup

#### Step 1: Add GitHub Secrets (optional, for S3 backup)

If you want S3 backup, add these secrets to your GitHub repo:
1. Go to Settings → Secrets and variables → Actions
2. Create new secrets:
   - `AWS_ACCESS_KEY_ID`
   - `AWS_SECRET_ACCESS_KEY`
3. Create new variable:
   - `S3_BUCKET` = your-bucket-name

#### Step 2: Create a Release

```bash
# Tag your release
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0
```

GitHub Actions will automatically:
1. Create chroma_db archive
2. Attach to GitHub Release
3. Create metadata.json with checksums
4. (Optional) Upload to S3

#### Step 3: Users Download

```bash
# Set the remote URL
export CHROMA_DB_REMOTE_URL=https://github.com/yourusername/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz

# Setup auto-spec
auto-spec setup

# Use normally
auto-spec generate contract.sol
```

### Manual Release (without GitHub Actions)

If you prefer to upload manually:

```bash
# 1. Create archive
python scripts/create_release.py --version 1.0.0 --metadata

# 2. Create GitHub Release
gh release create v1.0.0 chroma_db_v1.0.0.tar.gz metadata.json

# 3. Get the download URL from GitHub
# Users then set:
export CHROMA_DB_REMOTE_URL=https://github.com/yourusername/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz
```

---

## Method 2: AWS S3

### Pros
- ✅ Fast downloads (CDN optional)
- ✅ Reliable
- ✅ Scriptable
- ✅ Production-grade

### Cons
- ⚠️ Requires AWS account (~$0-5/month for storage + transfer)
- ⚠️ Slightly more setup

### Setup

#### Step 1: Prepare AWS

```bash
# Install AWS CLI
pip install boto3
# or
brew install awscli
# or
sudo apt install awscli

# Configure credentials
aws configure
# Enter:
# - AWS Access Key ID
# - AWS Secret Access Key
# - Default region (e.g., us-east-1)
# - Default output format (json)
```

#### Step 2: Create S3 Bucket

```bash
# Create bucket (bucket names must be globally unique)
aws s3 mb s3://my-auto-spec-db

# Make it public for reading (or use CloudFront)
aws s3api put-bucket-acl --bucket my-auto-spec-db --acl public-read

# Optional: Enable versioning
aws s3api put-bucket-versioning \
  --bucket my-auto-spec-db \
  --versioning-configuration Status=Enabled
```

#### Step 3: Upload chroma_db

```bash
# Manual upload
python scripts/upload_to_s3.py \
  --bucket my-auto-spec-db \
  --prefix auto-spec-db/v1.0.0

# This outputs:
# export CHROMA_DB_REMOTE_URL=https://my-auto-spec-db.s3.amazonaws.com/auto-spec-db/v1.0.0

# Dry run (check what would be uploaded)
python scripts/upload_to_s3.py \
  --bucket my-auto-spec-db \
  --prefix auto-spec-db/v1.0.0 \
  --dry-run
```

#### Step 4: Set up S3 URL

```bash
# For your users
export CHROMA_DB_REMOTE_URL=https://my-auto-spec-db.s3.amazonaws.com/auto-spec-db/v1.0.0
auto-spec setup
```

#### Step 5: (Optional) Setup Automated S3 Upload

To automate S3 upload in GitHub Actions:

1. Create AWS IAM user for S3-only access
2. Add GitHub Secrets:
   - `AWS_ACCESS_KEY_ID`
   - `AWS_SECRET_ACCESS_KEY`
3. Add GitHub Variable:
   - `S3_BUCKET` = my-auto-spec-db
4. Push tag → GitHub Actions uploads to both GitHub and S3

### Optional: CloudFront CDN

For faster global access, use CloudFront:

```bash
# 1. Create CloudFront distribution
#    Origin: https://my-auto-spec-db.s3.amazonaws.com
#    Cache: 1 year (versioned)

# 2. Get CloudFront URL from AWS Console
# Example: https://d123abc.cloudfront.net

# 3. Users set:
export CHROMA_DB_REMOTE_URL=https://d123abc.cloudfront.net/auto-spec-db/v1.0.0
```

---

## Combined Strategy (Recommended)

### Setup Both

1. **GitHub Releases** = Primary (always available)
2. **S3 + CloudFront** = Backup/faster (optional)

### Configuration

```bash
# Production users
export CHROMA_DB_REMOTE_URL=https://d123abc.cloudfront.net/auto-spec-db/latest

# GitHub users (if CDN goes down)
export CHROMA_DB_REMOTE_URL=https://github.com/yourusername/auto-spec/releases/download/latest/chroma_db_latest.tar.gz
```

### Fallback Logic (Future Feature)

```python
# auto_spec/vector_db.py could try multiple URLs:
DOWNLOAD_URLS = [
    "https://cdn.example.com/chroma_db/latest",  # Primary
    "https://github.com/yourname/auto-spec/releases/download/latest/...",  # Backup
]

for url in DOWNLOAD_URLS:
    if download_db(url):
        break
```

---

## Release Process

### Quick Release Checklist

```bash
# 1. Update version in pyproject.toml
VERSION=1.0.0
sed -i "s/version = .*/version = \"$VERSION\"/" pyproject.toml

# 2. Create git tag
git tag -a v$VERSION -m "Release v$VERSION"
git push origin v$VERSION

# GitHub Actions automatically:
# ✓ Archives chroma_db
# ✓ Creates release
# ✓ Uploads to S3 (if configured)
# ✓ Updates documentation

# 3. Verify release
gh release view v$VERSION
```

### Manual Release Process

```bash
# 1. Create archive
python scripts/create_release.py --version 1.0.0 --metadata

# 2. Upload to GitHub
gh release create v1.0.0 \
  chroma_db_v1.0.0.tar.gz \
  metadata.json

# 3. Upload to S3
python scripts/upload_to_s3.py \
  --bucket my-auto-spec-db \
  --prefix auto-spec-db/v1.0.0

# 4. Copy download URLs to README
```

---

## Testing Deployment

### Test GitHub Release Download

```bash
# Test the release works
export CHROMA_DB_REMOTE_URL=https://github.com/yourusername/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz

# Fresh install (remove old DB first)
rm -rf ~/.auto-spec/chroma_db
auto-spec setup

# Verify it works
auto-spec generate example_contract.sol
```

### Test S3 Download

```bash
export CHROMA_DB_REMOTE_URL=https://my-bucket.s3.amazonaws.com/auto-spec-db/v1.0.0

rm -rf ~/.auto-spec/chroma_db
auto-spec setup

auto-spec generate example_contract.sol
```

### Test Fallback Logic

```bash
# Set bad URL first, should fallback
export CHROMA_DB_REMOTE_URL=https://invalid.example.com/chroma_db

# Should fail cleanly or fallback to backup URL
auto-spec setup
```

---

## AWS Cost Estimation

### Monthly costs (rough estimate)

| Usage | Storage | Transfer | Total |
|-------|---------|----------|-------|
| 1 release | $0.02 | $0.02 | $0.04 |
| 1000 downloads | $0.02 | $5.00 | $5.02 |
| 10000 downloads | $0.02 | $50.00 | $50.02 |

**Note**: First 1GB of transfer is free per month.

### Cost optimization

1. Use S3 Standard storage
2. Put objects in the cheapest region (us-east-1)
3. Use CloudFront for CDN (lowers transfer costs)
4. Enable versioning only if needed
5. Set lifecycle rules to delete old versions

---

## Troubleshooting

### "Download failed"
- Check URL is public/accessible
- Check file exists on S3: `aws s3 ls s3://bucket/path/`
- Check firewall/network

### "Checksum mismatch"
- File corrupted during download
- Wrong file uploaded
- Truncated transfer
- Retry download

### "S3 bucket not accessible"
- Check bucket is public: `aws s3api get-bucket-acl --bucket my-bucket`
- Check IAM permissions
- Check credentials: `aws sts get-caller-identity`

### "GitHub Actions timeout"
- Archive too large
- Network issue
- Increase timeout in .github/workflows/release.yml

---

## Documentation Links

- [boto3 documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)
- [AWS S3 pricing](https://aws.amazon.com/s3/pricing/)
- [GitHub Releases API](https://docs.github.com/en/rest/releases)
- [CloudFront pricing](https://aws.amazon.com/cloudfront/pricing/)

---

## Support

For deployment issues:
1. Check logs: `auto-spec setup --verbose` (if implemented)
2. Open GitHub issue
3. Check AWS console for S3 errors
