# Release Checklist

Quick reference for releasing Auto-Spec versions.

## Pre-Release

- [ ] Update version in `pyproject.toml`
- [ ] Update `CHANGELOG.md` (if exists)
- [ ] Test locally: `auto-spec setup && auto-spec generate test_contract.sol`
- [ ] Run tests: `pytest tests/`
- [ ] Check code quality: `black auto_spec/` and `flake8 auto_spec/`

## Release to GitHub + S3 (Automated)

```bash
# 1. Commit changes
git add .
git commit -m "Bump version to 1.0.0"

# 2. Create tag (triggers GitHub Actions)
git tag -a v1.0.0 -m "Release v1.0.0: [description]"

# 3. Push tag (GitHub Actions automatically:
#    - Archives chroma_db
#    - Creates GitHub Release
#    - Uploads to S3 if configured
#    - Generates metadata)
git push origin v1.0.0

# 4. Verify release
#    - Check GitHub Releases page
#    - Verify chroma_db download works
#    - Test S3 upload (if configured)
```

## Release Manually (If Needed)

```bash
# 1. Create archive
python scripts/create_release.py --version 1.0.0 --metadata

# 2. Upload to GitHub
gh release create v1.0.0 \
  chroma_db_v1.0.0.tar.gz \
  metadata.json \
  -t "Release v1.0.0" \
  -n "Release notes here..."

# 3. Upload to S3 (optional)
python scripts/upload_to_s3.py \
  --bucket my-bucket \
  --prefix auto-spec-db/v1.0.0
```

## Post-Release

- [ ] Test download from GitHub: 
  ```bash
  export CHROMA_DB_REMOTE_URL="https://github.com/yourusername/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz"
  rm -rf ~/.auto-spec/chroma_db
  auto-spec setup
  ```
- [ ] Test S3 download (if applicable)
- [ ] Update documentation with new release URL
- [ ] Announce on GitHub Discussions/Issues
- [ ] Optionally publish to PyPI: `twine upload dist/*`

## Troubleshooting

**GitHub Actions failed?**
- Check workflow logs: Settings → Actions
- Common issue: Large file size timing out
- Solution: Increase timeout in .github/workflows/release.yml

**S3 upload failed?**
- Check credentials: `aws sts get-caller-identity`
- Check bucket exists: `aws s3 ls`
- Check permissions: IAM policy allows s3:PutObject

**Checksum mismatch on download?**
- Network interrupted
- File corrupted
- Solution: User retries `auto-spec setup`

## Version Numbering

Use semantic versioning: `MAJOR.MINOR.PATCH`

- `v1.0.0` - Major release (breaking changes)
- `v1.1.0` - Minor release (new features)
- `v1.0.1` - Patch release (bug fixes)

Example progression:
```
v1.0.0 → v1.1.0 (add features) → v1.1.1 (fix bugs) → v2.0.0 (breaking)
```

## One-Liner Reference

```bash
# Everything in one go
VERSION=1.0.0 && git tag -a v$VERSION -m "Release v$VERSION" && git push origin v$VERSION && echo "✓ Release created - check GitHub Actions!"
```
