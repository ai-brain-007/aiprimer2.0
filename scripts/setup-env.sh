#!/usr/bin/env bash
# Cloud-environment setup script for AI Primer 2.0.
# Paste the contents of this file into the "Setup script" box of the Claude Code cloud environment.
# It runs once per environment build; the environment cache keeps what it installs. It never fails the
# build: every problem is logged and the session-start hook retries the python install in the background.
set -u
start=$(date +%s)
log() { echo "[setup +$(( $(date +%s) - start ))s] $*"; }

PACKAGES='
typer>=0.12
pydantic>=2.6
pyyaml>=6.0
python-frontmatter>=1.1
jinja2>=3.1
rapidfuzz>=3.6
python-slugify>=8.0
charset-normalizer>=3.3
tenacity>=8.2
google-api-python-client>=2.120
google-auth>=2.28
google-auth-httplib2>=0.2
google-auth-oauthlib>=1.2
cffi>=1.16
cryptography>=42
pysocks>=1.7
apify-client>=1.6
pymupdf>=1.24
python-docx>=1.1
openpyxl>=3.1
pandas>=2.2
pytest>=8.0
'

REPO=""
for d in "$PWD" "${CLAUDE_PROJECT_DIR:-}" /home/user/aiprimer2.0 "$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd)"; do
  if [ -n "$d" ] && [ -f "$d/requirements.txt" ] && [ -d "$d/pipeline" ]; then REPO="$d"; break; fi
done
if [ -z "$REPO" ]; then
  REPO="$(find /home /workspace /root /mnt -maxdepth 4 -type f -name requirements.txt -path '*aiprimer*' 2>/dev/null | head -n 1 | xargs -r dirname)"
fi
log "cwd: $PWD | repo: ${REPO:-not found (using the built-in package list)} | python: $(command -v python3) ($(python3 --version 2>&1))"

(
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    SUDO=""
    if [ "$(id -u)" != "0" ] && sudo -n true 2>/dev/null; then SUDO=sudo; fi
    $SUDO apt-get update -qq >/tmp/aiprimer-apt.log 2>&1 || true
    if $SUDO apt-get install -y -qq --no-install-recommends tesseract-ocr poppler-utils >>/tmp/aiprimer-apt.log 2>&1; then
      log "system tools installed: tesseract-ocr poppler-utils"
    else
      log "WARNING: apt install failed (continuing); last lines of /tmp/aiprimer-apt.log:"
      tail -n 5 /tmp/aiprimer-apt.log 2>/dev/null || true
    fi
  else
    log "apt-get not found; skipping system tools"
  fi
) &
apt_pid=$!

PIP="python3 -m pip --disable-pip-version-check --no-input"
$PIP install -q --ignore-installed "cffi>=1.16" "cryptography>=42" >/tmp/aiprimer-pip.log 2>&1 || true
printf '%s\n' "$PACKAGES" >/tmp/aiprimer-requirements.txt
if $PIP install -q -r /tmp/aiprimer-requirements.txt >>/tmp/aiprimer-pip.log 2>&1; then
  log "python packages installed"
else
  log "WARNING: pip install failed; last lines of /tmp/aiprimer-pip.log:"
  tail -n 15 /tmp/aiprimer-pip.log 2>/dev/null || true
  log "the session-start hook retries the install in the background; or run: python3 -m pip install -r requirements.txt"
fi
if [ -n "$REPO" ]; then
  $PIP install -q -r "$REPO/requirements.txt" >>/tmp/aiprimer-pip.log 2>&1 || log "note: requirements.txt of the repo has extras that failed (see /tmp/aiprimer-pip.log)"
  (cd "$REPO" && $PIP install -q -e . >>/tmp/aiprimer-pip.log 2>&1) || log "note: 'pip install -e .' failed (python -m pipeline still works from the repo root)"
fi

wait "$apt_pid" 2>/dev/null || true

python3 - <<'EOF' || true
import importlib
mods = ["typer", "pydantic", "yaml", "googleapiclient", "google.oauth2.service_account", "google_auth_oauthlib",
        "apify_client", "fitz", "docx", "openpyxl", "pandas", "rapidfuzz", "frontmatter", "jinja2",
        "cryptography.hazmat.primitives.serialization"]
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as exc:
        missing.append(f"{m} ({type(exc).__name__})")
print("[setup] python modules missing: " + (", ".join(missing) if missing else "none"))
EOF
for t in tesseract pdftoppm ffprobe pandoc; do
  if command -v "$t" >/dev/null 2>&1; then echo "[setup] tool present: $t"; else echo "[setup] tool absent (optional): $t"; fi
done
log "done"
exit 0
