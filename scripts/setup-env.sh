#!/usr/bin/env bash
# Cloud-environment setup script for AI Primer 2.0.
# Paste the contents of this file into the "Setup script" box of the Claude Code cloud environment.
#
# Rules of that box (docs → Configure cloud environments → Setup scripts), learned the hard way:
#   - it does NOT start inside the repository folder (a plain `pip install -r requirements.txt` fails with
#     "No such file or directory"), so this script carries its own package list and only uses the repo if it
#     finds it;
#   - it must finish in about five minutes and must exit 0, otherwise the session does not start;
#   - it runs as root on Ubuntu 24.04, once; the environment cache keeps what it installs and the script only
#     runs again when its text or the allowed-domains list changes.
# Keep the PACKAGES list below in sync with requirements.txt.
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

# Locate the repository if it is around (not required).
REPO=""
for d in "$PWD" "${CLAUDE_PROJECT_DIR:-}" /home/user/aiprimer2.0 "$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd)"; do
  if [ -n "$d" ] && [ -f "$d/requirements.txt" ] && [ -d "$d/pipeline" ]; then REPO="$d"; break; fi
done
if [ -z "$REPO" ]; then
  REPO="$(find /home /workspace /root /mnt -maxdepth 4 -type f -name requirements.txt -path '*aiprimer*' 2>/dev/null | head -n 1 | xargs -r dirname)"
fi
log "cwd: $PWD | repo: ${REPO:-not found (using the built-in package list)} | python: $(command -v python3) ($(python3 --version 2>&1))"

# 1) System tools, in the background: tesseract (OCR of scanned pages) and poppler (pdftoppm).
#    ffmpeg and pandoc are optional (video probing, docx fallback); install them from a session if needed:
#    apt-get install -y --no-install-recommends ffmpeg pandoc
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
  if command -v "$t" >/dev/null 2>&1; then echo "[setup] tool present: $t"; else echo "[setup] tool absent (optional): $t"; fi
done
log "done"
exit 0
