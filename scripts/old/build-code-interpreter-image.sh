#!/bin/bash
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DOCKER_DIR="$PROJECT_ROOT/docker/code-interpreter"

IMAGE_NAME="ai-gateway-code-interpreter"
IMAGE_TAG="latest"

echo "Building Code Interpreter Docker image..."
docker build -t "$IMAGE_NAME:$IMAGE_TAG" "$DOCKER_DIR"
echo "Build complete!"
