#!/usr/bin/env python3
"""One-time Google authorisation for ONE Gmail account.

Run this on your own computer (it needs a browser), once per Gmail account:

    pip install google-auth-oauthlib
    python scripts/auth_local.py --client-id <ID> --client-secret <SECRET>

A browser opens; sign in as the account you want to authorise (e.g. ai.primer.rawfile.0001@gmail.com),
click through "Google hasn't verified this app" (Advanced -> Go to ...), click Allow.
The script prints ONE line: the refresh token. Copy it into the cloud environment's variables as
GOOGLE_REFRESH_TOKEN_RAW01 (raw-file account) or GOOGLE_REFRESH_TOKEN_SUMMARY01 (summary account).

If no browser can open on this machine, add --manual: the script prints a URL to open anywhere;
after clicking Allow the browser lands on an http://localhost/... page that fails to load; copy that
full address from the address bar and paste it back into the script.

Requirements on the Google Cloud side (done once): a project with the Drive, Sheets and Docs APIs
enabled, an OAuth consent screen set to "In production" (in "Testing" the token dies after 7 days),
and an OAuth client of type "Desktop app".
"""

from __future__ import annotations

import argparse
import os
import sys

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", default=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""))
    parser.add_argument("--manual", action="store_true", help="print the URL instead of opening a browser")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.client_id or not args.client_secret:
        print("client id and secret are required (--client-id/--client-secret or env vars)", file=sys.stderr)
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("run: pip install google-auth-oauthlib", file=sys.stderr)
        return 2

    client_config = {
        "installed": {
            "client_id": args.client_id,
            "client_secret": args.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)

    if args.manual:
        flow.redirect_uri = f"http://localhost:{args.port}/"
        url, _state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
        print("\n1. Open this address in any browser, signed in as the account to authorise:\n")
        print(url)
        print("\n2. Click Allow. The browser will land on an http://localhost page that fails to load: that is expected.")
        redirected = input("3. Paste the FULL address of that page here: ").strip()
        flow.fetch_token(authorization_response=redirected)
    else:
        flow.run_local_server(port=args.port, access_type="offline", prompt="consent", open_browser=True)

    creds = flow.credentials
    if not creds.refresh_token:
        print("No refresh token returned. Remove the app's access at myaccount.google.com/permissions and run again.", file=sys.stderr)
        return 1
    print("\nRefresh token (copy the whole line into the cloud environment variables):\n")
    print(creds.refresh_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
