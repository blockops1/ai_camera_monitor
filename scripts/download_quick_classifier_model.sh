#!/usr/bin/env bash
# download_quick_classifier_model.sh — one-time model download for the motion gate.
#
# Downloads YOLOv8n (COCO-pretrained, ~12MB) into models/ so infra/quick_classifier.py
# can run it via onnxruntime. The .onnx file is gitignored (see .gitignore).
#
# Run after clone:
#     bash scripts/download_quick_classifier_model.sh
#
# Or download manually:
#     curl -sSL -L -o models/yolov8n.onnx \
#       https://github.com/yoobright/yolo-onnx/raw/master/yolov8n.onnx
#
# Why this model:
#   - YOLOv8n: industry-standard object detector, smallest variant (~12MB)
#   - COCO-pretrained: detects person/car/truck/bicycle/dog/cat/horse/sheep/cow
#     out of the box — the classes we care about for the motion gate
#   - ONNX format: runs via onnxruntime (already in our venv) with CoreML
#     execution provider on macOS (Apple Silicon GPU/ANE acceleration)
#   - No torch install needed (torch is ~1.5GB; onnxruntime is already there)
#
# Verification after download:
#     source .venv/bin/activate
#     python3 scripts/probe_quick_classifier.py --frames 5

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODEL_DIR="$PROJECT_ROOT/models"
MODEL_PATH="$MODEL_DIR/yolov8n.onnx"
EXPECTED_SIZE_MB=12

mkdir -p "$MODEL_DIR"

if [ -f "$MODEL_PATH" ]; then
    existing_mb=$(du -m "$MODEL_PATH" | cut -f1)
    if [ "$existing_mb" -ge "$EXPECTED_SIZE_MB" ]; then
        echo "✓ Model already present: $MODEL_PATH (${existing_mb}MB)"
        exit 0
    fi
    echo "Model file exists but looks too small (${existing_mb}MB < ${EXPECTED_SIZE_MB}MB). Re-downloading..."
fi

echo "Downloading YOLOv8n ONNX (~$EXPECTED_SIZE_MB MB) to $MODEL_PATH..."
curl -sSL -L -o "$MODEL_PATH" \
    "https://github.com/yoobright/yolo-onnx/raw/master/yolov8n.onnx"

actual_size=$(stat -f%z "$MODEL_PATH" 2>/dev/null || stat -c%s "$MODEL_PATH")
actual_mb=$((actual_size / 1024 / 1024))
if [ "$actual_mb" -lt "$EXPECTED_SIZE_MB" ]; then
    echo "ERROR: Downloaded file is only ${actual_mb}MB (expected ~${EXPECTED_SIZE_MB}MB)"
    echo "URL may be wrong or file truncated. Check $MODEL_PATH"
    exit 1
fi

echo "✓ Downloaded: $MODEL_PATH (${actual_mb}MB)"
echo ""
echo "Verify with:"
echo "    source .venv/bin/activate"
echo "    python3 scripts/probe_quick_classifier.py --frames 5"
