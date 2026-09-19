#!/usr/bin/env bash
# Cloud-environment setup script for AI Primer 2.0.
# Paste the contents of this file into the "Setup script" box of the Claude Code cloud environment.
# It runs once; the environment cache keeps what it installs.
set -euo pipefail

echo "[setup] installing system tools (OCR, PDF, pandoc, ffmpeg)"
export DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  sudo -n true 2>/dev/null && SUDO=sudo || SUDO=""
  $SUDO apt-get update -qq || true
  $SUDO apt-get install -y -qq tesseract-ocr poppler-utils pandoc ffmpeg || echo "[setup] apt install failed (continuing)"
fi

echo "[setup] installing python dependencies"
# The OS image ships a system copy of `cryptography` that pip cannot uninstall and that lacks its cffi
# backend; install a working pair beside it first (/usr/local takes precedence on sys.path).
python3 -m pip install --quiet --disable-pip-version-check --ignore-installed "cffi>=1.16" "cryptography>=42" || true
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt
python3 -m pip install --quiet --disable-pip-version-check -e . || true

echo "[setup] done"
