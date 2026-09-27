#!/usr/bin/env bash
# SessionStart hook: make Google clients work behind the session proxy and report which
# settings are present (names only, never values). Output is shown to the agent.
set -u

# httplib2 (used by google-api-python-client) needs to be told where the proxy CA bundle is.
if [ -z "${HTTPLIB2_CA_CERTS:-}" ]; then
  for f in "${SSL_CERT_FILE:-}" "${REQUESTS_CA_BUNDLE:-}" /root/.ccr/ca-bundle.crt; do
    if [ -n "$f" ] && [ -f "$f" ]; then export HTTPLIB2_CA_CERTS="$f"; break; fi
  done
fi

# Safety net: the cloud setup script has a five-minute budget; if it could not finish installing the Python
# packages, install them now in the background and tell the agent to wait for it.
if ! python3 -c "import typer, pydantic, googleapiclient, apify_client, fitz, pandas, rapidfuzz" >/dev/null 2>&1; then
  if [ -f requirements.txt ]; then
    mkdir -p .cache
    nohup python3 -m pip install --disable-pip-version-check --no-input -q -r requirements.txt >.cache/pip-install.log 2>&1 &
    echo "AI Primer pipeline: Python packages missing; installing them in the background (log: .cache/pip-install.log). Before any pipeline command, wait until this passes: python3 -c 'import pandas, fitz, googleapiclient'"
  fi
fi

missing=()
b2_keys=$(env | grep -o '^B2_APPLICATION_KEY_[A-Z0-9_]*' | tr '\n' ' ')
if [ -n "${AIPRIMER_NOTION_PAGE_ID:-}" ] || [ -n "${b2_keys:-}" ]; then
  mode="v2: Notion page $( [ -n "${AIPRIMER_NOTION_PAGE_ID:-}" ] && echo set || echo MISSING ), Backblaze keys: ${b2_keys:-none}"
  [ -n "${AIPRIMER_NOTION_PAGE_ID:-}" ] || missing+=("AIPRIMER_NOTION_PAGE_ID")
  [ -n "${b2_keys:-}" ] || missing+=("B2_KEY_ID_0001 B2_APPLICATION_KEY_0001")
  for k in $b2_keys; do
    n=${k#B2_APPLICATION_KEY_}
    [ -n "$(eval echo "\${B2_KEY_ID_${n}:-}")" ] || missing+=("B2_KEY_ID_${n}")
  done
elif [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ] || [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON_RAW:-}" ] || [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON_SUMMARY:-}" ]; then
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
if [ -z "${AIPRIMER_NOTION_PAGE_ID:-}" ] && [ -z "${b2_keys:-}" ]; then
  [ -n "${AIPRIMER_CONTROL_SHEET_ID:-}" ] || missing+=("AIPRIMER_CONTROL_SHEET_ID (v1) or AIPRIMER_NOTION_PAGE_ID (v2)")
fi

echo "AI Primer pipeline: credentials -> ${mode}"
if [ ${#missing[@]} -gt 0 ]; then
  echo "AI Primer pipeline: MISSING settings -> ${missing[*]} (see README.md, section Setup). Run /setup for details."
else
  echo "AI Primer pipeline: settings present. Run /setup for a health check, /ingest to add resources, /summarize <author> to build summaries."
fi
