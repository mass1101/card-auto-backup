#!/bin/bash
# 构建 card-backup Docker 镜像并导出为 tar 文件

IMAGE_NAME="card-backup"
VERSION="1.0.0"

echo "=== 构建 Docker 镜像 ==="
docker compose build

echo ""
echo "=== 导出镜像 ==="
docker save -o ${IMAGE_NAME}_${VERSION}.tar ${IMAGE_NAME}:latest

echo ""
echo "=== 完成 ==="
echo "镜像文件: ${IMAGE_NAME}_${VERSION}.tar"
echo ""
echo "=== 在其他机器上导入 ==="
echo "  docker load -i ${IMAGE_NAME}_${VERSION}.tar"
echo "  docker compose up -d"
