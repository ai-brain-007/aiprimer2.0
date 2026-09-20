#!/usr/bin/env bash
# SessionStart hook: make Google clients work behind the session proxy, retry the python install if the
# environment setup script did not finish it, and report which settings are present (names only, never values).
# Output is shown to the agent.
set -u

# httplib2 (used by google-api-python-client) needs to be told where the proxy CA bundle is.
if [ -z "${HTTPLIB2_CA_CERTS:-}" ]; then
  for f in "${SSL_CERT_FILE:-}" "${REQUESTS_CA_BUNDLE:-}" /root/.ccr/ca-bundle.crt; do
    if [ -n "$f" ] && [ -f "$f" ]; then export HTTPLIB2_CA_CERTS="$f"; break; fi
  done
fi

# The cloud setup script (scripts/setup-env.sh) installs requirements.txt once per environment build. If a
# session starts without them (fresh image, failed build), retry in the background so the session is not blocked.
repo="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd)"
if ! python3 -c "import typer, pydantic, googleapiclient" >/dev/null 2>&1; then
  if [ -f "$repo/requirements.txt" ]; then
    nohup python3 -m pip install --quiet --disable-pip-version-check --no-input -r "$repo/requirements.txt" \
      >>/tmp/aiprimer-pip.log 2>&1 &
    echo "AI Primer pipeline: python dependencies missing -> installing requirements.txt in the background (log: /tmp/aiprimer-pip.log). If 'python -m pipeline' fails with ModuleNotFoundError, wait a minute or run: bash scripts/setup-env.sh"
  else
    echo "AI Primer pipeline: python dependencies missing and requirements.txt not found next to this hook; run: bash scripts/setup-env.sh"
  fi
fi

missing=()
if [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ] || [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON_RAW:-}" ] || [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY:-}" ]; then
  mode="service account key(s) present"
  [ -n "${AIPRIMER_RAW_DRIVE_ID:-}" ] || missing+=("AIPRIMER_RAW_DRIVE_ID")
  [ -n "${AIPRIMER_SUMMARY_DRIVE_ID:-}" ] || missing+=("AIPRIMER_SUMMARY_DRIVE_ID")
else
  tokens=$(env | grep -o '^GOOGLE_REFRESH_TOKEN_[A-Z0-9_]*' | tr '\n' ' ')
  mode="refresh tokens: ${tokens:-none}"
  for v in GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET; do
    [ -n "${!v:-}" ] || missing+=("$v")
  done
  [ -n "${tokens:-}" ] || missing+=("GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_REFRESH_TOKEN_*")
fi
[ -n "${AIPRIMER_CONTROL_SHEET_ID:-}" ] || missing+=("AIPRIMER_CONTROL_SHEET_ID")

echo "AI Primer pipeline: Google credentials -> ${mode}"
if [ ${#missing[@]} -gt 0 ]; then
  echo "AI Primer pipeline: MISSING settings -> ${missing[*]} (see README.md, section Setup). Run /setup for details."
else
  echo "AI Primer pipeline: settings present. Run /setup for a health check, /ingest to add resources, /summarize <author> to build summaries."
fi
