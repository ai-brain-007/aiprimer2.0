#!/usr/bin/env bash
# Cloud-environment setup script for AI Primer 2.0.
# Paste the contents of this file into the "Setup script" box of the Claude Code cloud environment.
#
# Rules of that box (docs → Configure cloud environments → Setup scripts):
#   - it must finish in about five minutes, otherwise it is cut off and the session does not start;
#   - it must exit 0, otherwise the session does not start;
#   - it runs as root on Ubuntu 24.04, in the repository root, once; the environment cache keeps what it
#     installs and the script only runs again when its text or the allowed-domains list changes.
# So: install only what ingestion needs now (OCR + PDF rendering + Python packages), run the two installs in
# parallel, never abort, and print the errors instead of hiding them.
set -u
start=$(date +%s)
log() { echo "[setup +$(( $(date +%s) - start ))s] $*"; }

# Work from the repository root even if the box runs elsewhere.
if [ ! -f requirements.txt ]; then
  for d in "${CLAUDE_PROJECT_DIR:-}" "$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd)" /home/user/aiprimer2.0; do
    if [ -n "$d" ] && [ -f "$d/requirements.txt" ]; then cd "$d" && break; fi
  done
fi
log "repo: $(pwd) | python: $(command -v python3) ($(python3 --version 2>&1))"

# 1) System tools, in the background: tesseract (OCR of scanned pages) and poppler (pdftoppm).
#    ffmpeg and pandoc are optional (video probing, docx fallback) and would blow the five-minute budget;
#    install them later from a session if needed: apt-get install -y --no-install-recommends ffmpeg pandoc
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

# 2) Python packages, in the foreground so a failure is visible in the setup log.
#    The OS image ships a system copy of `cryptography` that pip cannot uninstall and that lacks its cffi
#    backend; install a working pair beside it first (/usr/local takes precedence on sys.path).
PIP="python3 -m pip --disable-pip-version-check --no-input"
$PIP install -q --ignore-installed "cffi>=1.16" "cryptography>=42" >/tmp/aiprimer-pip.log 2>&1 || true
if $PIP install -q -r requirements.txt >>/tmp/aiprimer-pip.log 2>&1; then
  log "python packages installed"
else
  log "WARNING: pip install -r requirements.txt failed; last lines of /tmp/aiprimer-pip.log:"
  tail -n 15 /tmp/aiprimer-pip.log 2>/dev/null || true
  log "the session-start hook retries the install in the background; or run: python3 -m pip install -r requirements.txt"
fi
$PIP install -q -e . >>/tmp/aiprimer-pip.log 2>&1 || log "note: 'pip install -e .' failed (python -m pipeline still works from the repo root)"

wait "$apt_pid" 2>/dev/null || true

# 3) Verify, so the setup log says what is usable.
python3 - <<'EOF' || true
import importlib
mods = ["typer", "pydantic", "yaml", "googleapiclient", "google.oauth2.service_account", "google_auth_oauthlib",
        "apify_client", "fitz", "docx", "openpyxl", "pandas", "rapidfuzz", "frontmatter", "jinja2",
        "cryptography.hazmat.primitives.serialization"]
missing = []
for m in mods:
    try:
        importlib.import_module(m)
    except Exception as exc:  # noqa: BLE001
        missing.append(f"{m} ({type(exc).__name__})")
print("[setup] python modules missing: " + (", ".join(missing) if missing else "none"))
EOF
for t in tesseract pdftoppm ffprobe pandoc; do
  if command -v "$t" >/dev/null 2>&1; then echo "[setup] tool present: $t"; else echo "[setup] tool absent (optional unless noted): $t"; fi
done
log "done"
exit 0
