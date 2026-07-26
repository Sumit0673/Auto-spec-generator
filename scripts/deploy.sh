#!/bin/bash
# Deployment guide for Auto-Spec

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Auto-Spec Deployment Guide${NC}\n"

# Check prerequisites
echo "Checking prerequisites..."
command -v python3 &> /dev/null || { echo -e "${RED}Python 3 not found${NC}"; exit 1; }
command -v git &> /dev/null || { echo -e "${RED}Git not found${NC}"; exit 1; }

# Option 1: GitHub Release
echo -e "\n${YELLOW}Option 1: Deploy via GitHub Releases${NC}"
echo "This will create a release with chroma_db as an attachment"
echo ""
echo "Steps:"
echo "1. Ensure you have git tags set up:"
echo "   git tag -a v1.0.0 -m 'Release version 1.0.0'"
echo "   git push origin v1.0.0"
echo ""
echo "2. GitHub Actions will automatically:"
echo "   - Create chroma_db archive"
echo "   - Attach to GitHub Release"
echo "   - Make available for download"
echo ""
echo "3. Users can then use:"
echo "   export CHROMA_DB_REMOTE_URL=https://github.com/username/auto-spec/releases/download/v1.0.0/chroma_db_v1.0.0.tar.gz"
echo ""

# Option 2: AWS S3
echo -e "\n${YELLOW}Option 2: Deploy via AWS S3${NC}"
echo "Prerequisites:"
echo "- AWS CLI installed: pip install boto3 or apt install awscli"
echo "- AWS credentials configured: aws configure"
echo "- S3 bucket created"
echo ""
echo "To upload chroma_db to S3:"
echo "   python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db"
echo ""
echo "To set up GitHub Actions S3 upload:"
echo "   1. Set GitHub Secrets: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY"
echo "   2. Set GitHub Variables: S3_BUCKET"
echo "   3. Push tag: git tag -a v1.0.0 -m 'Release' && git push origin v1.0.0"
echo ""

# Option 3: Manual Release Creation
echo -e "\n${YELLOW}Option 3: Manual Release Creation${NC}"
echo "Create chroma_db archive locally:"
echo ""
echo "   # Create archive"
echo "   python scripts/create_release.py --version 1.0.0 --metadata"
echo ""
echo "   # This creates:"
echo "   #   - chroma_db_v1.0.0.tar.gz (~500MB)"
echo "   #   - metadata.json with checksums"
echo ""
echo "   # Upload manually to GitHub or your storage:"
echo "   gh release create v1.0.0 chroma_db_v1.0.0.tar.gz"
echo ""

# Quick start script
echo -e "\n${GREEN}Quick Start: Full Deployment${NC}\n"

read -p "Would you like to create a release now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    read -p "Enter version number (e.g., 1.0.0): " VERSION
    
    echo -e "\nCreating release v${VERSION}...\n"
    
    # Create archive
    echo "Creating chroma_db archive..."
    python scripts/create_release.py \
        --version "$VERSION" \
        --metadata \
        --output "chroma_db_v${VERSION}.tar.gz"
    
    echo -e "\n${GREEN}✓ Release files created:${NC}"
    ls -lh chroma_db_v${VERSION}.tar.gz metadata.json
    
    echo -e "\n${YELLOW}Next steps:${NC}"
    echo "1. Create GitHub Release:"
    echo "   git tag -a v${VERSION} -m 'Release v${VERSION}'"
    echo "   git push origin v${VERSION}"
    echo ""
    echo "2. Upload to GitHub Release (manual):"
    echo "   gh release create v${VERSION} chroma_db_v${VERSION}.tar.gz metadata.json"
    echo ""
    echo "3. Or upload to S3:"
    echo "   python scripts/upload_to_s3.py --bucket my-bucket --prefix auto-spec-db"
    echo ""
fi

echo -e "\n${GREEN}Deployment options ready!${NC}"
echo "For more details, see: DEPLOYMENT.md"
