#!/usr/bin/env bash
# Build the AWS Lambda deployment zip: build/todue-relay-lambda.zip
#
# Dependencies are installed inside AWS's own Lambda Python image, so the
# compiled wheels (psycopg, cryptography, pydantic_core) match what Lambda
# actually runs — pip on a Mac would fetch macOS wheels that can't load there.
# Needs Docker. Builds nothing in AWS and uploads nothing.
#
# Usage (from anywhere):
#     backend/scripts/build_lambda.sh
#     ARCH=amd64 backend/scripts/build_lambda.sh     # for an x86_64 function
#
# ARCH must match the function's "architecture" setting (arm64 or x86_64).
# Default arm64: native on Apple-silicon Macs, so no slow emulation.
set -euo pipefail

cd "$(dirname "$0")/.."   # backend/

PY_VERSION="${PY_VERSION:-3.14}"
ARCH="${ARCH:-arm64}"
OUT="build"

rm -rf "$OUT"
mkdir -p "$OUT/pkg"

docker run --rm \
  --platform "linux/$ARCH" \
  --user "$(id -u):$(id -g)" \
  --entrypoint pip \
  -v "$PWD":/src:ro \
  -v "$PWD/$OUT/pkg":/pkg \
  "public.ecr.aws/lambda/python:$PY_VERSION" \
  install --quiet --no-cache-dir --target /pkg -r /src/requirements-lambda.txt

cp -R app lambda_handler.py "$OUT/pkg/"
find "$OUT/pkg" -name __pycache__ -prune -exec rm -rf {} +

(cd "$OUT/pkg" && zip -qr ../todue-relay-lambda.zip .)

echo "Built $OUT/todue-relay-lambda.zip ($(du -h "$OUT/todue-relay-lambda.zip" | cut -f1), python $PY_VERSION, $ARCH)"
