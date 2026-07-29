#!/bin/bash
# Build FPK package for FlyNAS
# Usage: ./build_fpk.sh

PKG_NAME="card-backup"
VERSION="1.0.0"
OUTPUT="${PKG_NAME}_${VERSION}.fpk"

mkdir -p data

tar czf "$OUTPUT" \
    Dockerfile \
    docker-compose.yml \
    app.py \
    requirements.txt \
    manifest.json \
    data/

echo "Built: $OUTPUT"
echo ""
echo "=== Deploy on FlyNAS ==="
echo "1. Upload $OUTPUT to your FlyNAS"
echo "2. In Docker panel, click Import and select the .fpk file"
echo ""
echo "=== Or deploy directly with Docker ==="
echo "  docker compose up -d"
