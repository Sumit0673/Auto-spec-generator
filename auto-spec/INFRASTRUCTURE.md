# Infrastructure Setup Guide

Complete guide for setting up training data distribution infrastructure.

## Option 1: GitHub Releases (Recommended - Free, Simple)

### Cost: $0 (Free)

### Setup Time: 5 minutes

### Setup Steps

1. **Nothing to do!** - GitHub Actions is already configured
   - File: `.github/workflows/release.yml`
   - Triggers on git tag push

2. **Push a release:**
   ```bash
   git tag -a v1.0.0 -m "Release v1.0.0"
   git push origin v1.0.0
   ```

3. **Done!** GitHub Actions automatically:
   - ✓ Archives chroma_db
   - ✓ Creates GitHub Release
   - ✓ Generates metadata with checksums

### User Downloads

```bash
export CHROMA_DB_REMOTE_URL=https://github.com/yourusername/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz
auto-spec setup
```

### Limits

- ✅ Unlimited releases
- ✅ Unlimited download counts
- ⚠️ ~500MB per release
- ✅ GitHub Releases stays indefinitely

---

## Option 2: AWS S3 (Fast, Scalable)

### Cost: ~$5-50/month (depending on traffic)

### Setup Time: 20 minutes

### Prerequisites

```bash
# Install AWS CLI
pip install boto3
# or
brew install awscli
# or
sudo apt install awscli

# Configure AWS credentials
aws configure
# Enter: Access Key, Secret Key, Region, Output format
```

### Step 1: Create S3 Bucket

```bash
# Choose a unique bucket name
BUCKET_NAME="my-auto-spec-db"

# Create bucket
aws s3 mb s3://$BUCKET_NAME

# Make it public for reading
aws s3api put-bucket-acl --bucket $BUCKET_NAME --acl public-read

# Optional: Enable versioning
aws s3api put-bucket-versioning \
  --bucket $BUCKET_NAME \
  --versioning-configuration Status=Enabled

# Optional: Add lifecycle rule to clean old versions (keep last 5)
aws s3api put-bucket-lifecycle-configuration \
  --bucket $BUCKET_NAME \
  --lifecycle-configuration file://lifecycle.json
```

### Step 2: Upload First Database

```bash
# Manual upload
python scripts/upload_to_s3.py \
  --bucket $BUCKET_NAME \
  --prefix auto-spec-db/v1.0.0

# This outputs something like:
# export CHROMA_DB_REMOTE_URL=https://my-auto-spec-db.s3.amazonaws.com/auto-spec-db/v1.0.0
```

### Step 3: Test Download

```bash
export CHROMA_DB_REMOTE_URL=https://my-auto-spec-db.s3.amazonaws.com/auto-spec-db/v1.0.0
rm -rf ~/.auto-spec/chroma_db
auto-spec setup  # Should download successfully
```

### Step 4: Automate with GitHub Actions (Optional)

1. Create AWS IAM User for S3-only access:
   ```bash
   # Via AWS Console or:
   aws iam create-user --user-name auto-spec-s3
   aws iam attach-user-policy --user-name auto-spec-s3 \
     --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
   ```

2. Add GitHub Secrets:
   - Go to: Settings → Secrets and variables → Actions
   - Create:
     - `AWS_ACCESS_KEY_ID` = (from IAM user)
     - `AWS_SECRET_ACCESS_KEY` = (from IAM user)

3. Add GitHub Variable:
   - `S3_BUCKET` = my-auto-spec-db

4. Now releases upload automatically!

### User Downloads

```bash
export CHROMA_DB_REMOTE_URL=https://my-auto-spec-db.s3.amazonaws.com/auto-spec-db/v1.0.0
auto-spec setup
```

### Pricing Estimate

```
Storage:     0.023 $/GB/month
Data OUT:    0.09 $/GB/month (first 1GB free)
Requests:    $0.0004 per 1000 requests

Example: 1000 downloads of 500MB
Storage:     $0.01/month
Transfer:    $45/month (1000 × 500MB × $0.09)
Total:       ~$45/month
```

**Optimization:** Add CloudFront CDN to reduce transfer costs by 70%+

---

## Option 3: Hybrid (GitHub + S3 with Fallback)

### Best of Both Worlds

```bash
# Primary: S3 (fast, CDN-capable)
export CHROMA_DB_REMOTE_URL=https://d123abc.cloudfront.net/auto-spec-db/v1.0.0

# Fallback: GitHub Releases (always available)
# (Code handles fallback automatically in future versions)
```

### Setup

1. Set up S3 (Option 2)
2. Keep GitHub Releases enabled (already done)
3. GitHub Actions uploads to both automatically

---

## Option 4: Google Cloud Storage

### Similar to S3, different provider

```bash
# Install Google Cloud SDK
pip install google-cloud-storage

# Configure
gcloud auth login
gcloud config set project PROJECT_ID

# Create bucket
gsutil mb gs://my-auto-spec-db

# Upload
gsutil -m cp -r erc20_pairs_final/chroma_db gs://my-auto-spec-db/

# Make public (careful!)
gsutil acl ch -u AllUsers:R gs://my-auto-spec-db/...
```

---

## Option 5: Your Own Server/CDN

### For enterprise deployments

```bash
# Upload to your infrastructure
scp -r erc20_pairs_final/chroma_db user@your-server:/data/

# Configure web server (nginx/apache)
# Make available at: https://your-domain.com/auto-spec-db/

# Users set:
export CHROMA_DB_REMOTE_URL=https://your-domain.com/auto-spec-db/v1.0.0
```

---

## Comparison Matrix

| Feature | GitHub | S3 | GCS | Your Server |
|---------|--------|-----|-----|------------|
| **Cost** | Free | $5-50/mo | $5-50/mo | Your cost |
| **Setup** | 0 min | 20 min | 15 min | Variable |
| **Speed** | Good | Fast | Fast | Depends |
| **CDN** | No | Yes | Yes | Yes |
| **Reliability** | 99.9% | 99.99% | 99.9% | Your SLA |
| **Bandwidth** | ∞ | Charged | Charged | Your plan |
| **Best for** | Open source | Production | Production | Enterprise |

---

## Recommended Path

### For Starting Out
1. Use GitHub Releases (already set up!)
2. Test with friends/colleagues
3. Gather feedback

### For Growing Community
1. Add S3 when downloads increase
2. Configure GitHub Actions to upload both
3. Keep GitHub as fallback

### For Production/Enterprise
1. Use S3 + CloudFront
2. Setup automatic scaling
3. Use GitHub for code, S3 for data

---

## Quick Reference

### To Upload New Version

```bash
# GitHub: Just push a tag
git tag -a v1.0.0 -m "Release"
git push origin v1.0.0

# S3: Run script
python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db/v1.0.0

# Both: Already automated!
```

### User Configuration

```bash
# GitHub
export CHROMA_DB_REMOTE_URL=https://github.com/user/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz

# S3
export CHROMA_DB_REMOTE_URL=https://my-bucket.s3.amazonaws.com/auto-spec-db/v1.0.0

# Local (development)
export CHROMA_DB_PATH=/path/to/chroma_db
```

---

## Files Reference

| File | Purpose |
|------|---------|
| `.github/workflows/release.yml` | GitHub Actions automation |
| `scripts/upload_to_s3.py` | Upload to S3 |
| `scripts/create_release.py` | Create GitHub Release package |
| `scripts/deploy.sh` | Interactive setup wizard |
| `DEPLOYMENT.md` | Full deployment guide |
| `TRAINING_DATA_STRATEGY.md` | Overview of all strategies |
| `vector_db.py` | Download logic (supports all methods) |

---

## Testing

### Test GitHub Release Download

```bash
export CHROMA_DB_REMOTE_URL="https://github.com/yourusername/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz"
rm -rf ~/.auto-spec/chroma_db
auto-spec setup
auto-spec generate test_contract.sol
```

### Test S3 Download

```bash
export CHROMA_DB_REMOTE_URL="https://my-bucket.s3.amazonaws.com/auto-spec-db/v1.0.0"
rm -rf ~/.auto-spec/chroma_db
auto-spec setup
auto-spec generate test_contract.sol
```

---

## Support

- **GitHub Issues**: Report problems
- **Discussions**: Ask questions
- **AWS Support**: For S3 issues (if on paid plan)
