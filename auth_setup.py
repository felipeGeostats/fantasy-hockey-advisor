"""One-time Yahoo OAuth login. Prints the refresh token the agent needs.

Usage:
  export YAHOO_CLIENT_ID=...  YAHOO_CLIENT_SECRET=...
  python auth_setup.py
"""
import base64
import os
import urllib.parse
import webbrowser

import requests

cid = os.environ.get("YAHOO_CLIENT_ID") or input("Yahoo Client ID: ").strip()
secret = os.environ.get("YAHOO_CLIENT_SECRET") or input("Yahoo Client Secret: ").strip()
redirect = os.environ.get("YAHOO_REDIRECT_URI", "https://localhost:8080")

url = "https://api.login.yahoo.com/oauth2/request_auth?" + urllib.parse.urlencode(
    {"client_id": cid, "redirect_uri": redirect, "response_type": "code"})
print("\n1. Log in and approve access here:\n  ", url)
print(f"2. Your browser will then fail to load {redirect} — that's expected.")
print("   Copy the full URL from the address bar (it contains ?code=...).\n")
webbrowser.open(url)

pasted = input("Paste the URL (or just the code): ").strip()
code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [pasted])[0]

basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
r = requests.post(
    "https://api.login.yahoo.com/oauth2/get_token",
    headers={"Authorization": f"Basic {basic}",
             "Content-Type": "application/x-www-form-urlencoded"},
    data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
    timeout=30)
r.raise_for_status()
print("\nSuccess. Store this as YAHOO_REFRESH_TOKEN (treat it like a password):\n")
print(r.json()["refresh_token"])
