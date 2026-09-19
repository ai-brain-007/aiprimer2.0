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

missing=()
for v in GOOGLE_OAUTH_CLIENT_ID GOOGLE_OAUTH_CLIENT_SECRET AIPRIMER_CONTROL_SHEET_ID; do
  [ -n "${!v:-}" ] || missing+=("$v")
done
tokens=$(env | grep -o '^GOOGLE_REFRESH_TOKEN_[A-Z0-9_]*' | tr '\n' ' ')

echo "AI Primer pipeline: settings present -> refresh tokens: ${tokens:-none}"
if [ ${#missing[@]} -gt 0 ]; then
  echo "AI Primer pipeline: MISSING settings -> ${missing[*]} (see README.md, section Setup). Run /setup for details."
else
  echo "AI Primer pipeline: core settings present. Run /setup for a health check, /ingest to add resources, /summarize <author> to build summaries."
fi
