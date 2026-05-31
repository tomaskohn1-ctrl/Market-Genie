"""
youtube_setup.py — ONE-TIME local OAuth setup for Market Genie YouTube poster
──────────────────────────────────────────────────────────────────────────────
Run this script ONCE on your local machine (not Railway).
It opens a browser, you log into Google and allow access,
then prints the 3 env var values to paste into Railway.

Prerequisites:
  pip install google-api-python-client google-auth-oauthlib

Steps:
  1. Go to https://console.cloud.google.com/
  2. Create a new project (e.g. "market-genie")
  3. Enable YouTube Data API v3
       APIs & Services → Library → search "YouTube Data API v3" → Enable
  4. Create OAuth credentials
       APIs & Services → Credentials → Create Credentials → OAuth client ID
       Application type: Desktop app
       Name: Market Genie
       Download the JSON file → rename it "client_secret.json"
       Place it in the same folder as this script
  5. On the OAuth consent screen, set:
       App name: Market Genie
       User support email: your email
       Scopes: add .../auth/youtube.upload
       Test users: add your YouTube account email
  6. Run:  python youtube_setup.py

The script will print 3 lines starting with YOUTUBE_CLIENT_ID=,
YOUTUBE_CLIENT_SECRET=, and YOUTUBE_TOKEN_JSON= — copy all three
into Railway → Variables.
"""

import json, base64, os, sys
from pathlib import Path

CLIENT_SECRET_FILE = "client_secret.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main():
    if not Path(CLIENT_SECRET_FILE).exists():
        print(f"\n❌  {CLIENT_SECRET_FILE} not found in current directory.")
        print("    Download it from Google Cloud Console → APIs & Services → Credentials")
        print("    and place it here:", Path(CLIENT_SECRET_FILE).resolve())
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        print("\n❌  Missing packages. Run:")
        print("    pip install google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    print("\n[Setup] Opening browser for Google OAuth...")
    print("        Log in with your YouTube channel account and click Allow.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    # Read client ID + secret from the secret file
    with open(CLIENT_SECRET_FILE) as f:
        client_data = json.load(f)
    installed = client_data.get("installed") or client_data.get("web") or {}
    client_id     = installed.get("client_id", "")
    client_secret = installed.get("client_secret", "")

    # Build token dict
    token_dict = {
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     client_id,
        "client_secret": client_secret,
        "scopes":        list(creds.scopes or SCOPES),
    }
    token_b64 = base64.b64encode(json.dumps(token_dict).encode()).decode()

    print("\n" + "=" * 70)
    print("✅  Auth complete!  Copy these 3 lines into Railway → Variables:\n")
    print(f"YOUTUBE_CLIENT_ID={client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={client_secret}")
    print(f"YOUTUBE_TOKEN_JSON={token_b64}")
    print("=" * 70)
    print("\nAfter adding the variables, redeploy on Railway.")
    print("Market Genie will post Shorts automatically at 9:15 AM, 12:00 PM, 4:15 PM ET.")


if __name__ == "__main__":
    main()
