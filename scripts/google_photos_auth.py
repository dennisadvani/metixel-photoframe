#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2024-2026 Metixel Photoframe Contributors
"""Mint a Google Photos refresh token for Metixel Photoframe.

Google only allows loopback (``http://127.0.0.1``) redirects for installed-app
OAuth clients, and the frame has no browser of its own — so the consent flow is
run once here, on a machine with a browser, and the resulting refresh token is
pasted into the frame's Sources → Google Photos settings.

Prerequisites (one-time, in your own Google Cloud project):

  1. https://console.cloud.google.com → create/select a project.
  2. APIs & Services → Library → enable **Photos Picker API**.
  3. APIs & Services → OAuth consent screen → External, add your own Google
     account under "Test users" (no verification needed for personal use).
  4. APIs & Services → Credentials → Create credentials → OAuth client ID →
     application type **Desktop app**.  Note the client ID and client secret.

Usage:
    # On a machine with a browser (the token works anywhere)
    python scripts/google_photos_auth.py --client-id XXX --client-secret YYY

    # No browser on this machine (e.g. over SSH on the Pi): print the URL,
    # open it elsewhere, then paste the redirected URL (or just the code) back
    python scripts/google_photos_auth.py --client-id XXX --client-secret YYY --manual

Only the standard library is used, so it runs anywhere Python 3.11+ does.
"""

from __future__ import annotations

import argparse
import http.server
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"

#: Default loopback port for the redirect URI.  Any port is allowed for
#: installed-app clients, so this does not need registering in the console.
DEFAULT_PORT = 8723

#: Shown in the browser once the code has been captured.
_DONE_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Metixel</title></head>
<body style="font-family:system-ui;background:#111;color:#eee;padding:3rem">
<h2>Google Photos connected</h2>
<p>You can close this tab and return to the terminal.</p>
</body></html>
"""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler that captures ``?code=`` from the redirect."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — http.server's required name
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        type(self).result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(_DONE_PAGE)))
        self.end_headers()
        self.wfile.write(_DONE_PAGE)

    def log_message(self, *_args) -> None:
        """Silence the default request logging."""


def _build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",  # ask for a refresh token
        "prompt": "consent",  # force a refresh token even on re-consent
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> dict[str, object]:
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 — fixed https endpoint
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"Token exchange failed (HTTP {e.code}): {detail}") from e


def _loopback_flow(client_id: str, client_secret: str, port: int) -> dict[str, object]:
    redirect_uri = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)
    url = _build_auth_url(client_id, redirect_uri, state)

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as e:
        raise SystemExit(f"Could not listen on 127.0.0.1:{port} ({e}) — try --port") from e

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print("\nOpening your browser to approve access…")
    print("If it doesn't open, paste this URL into a browser:\n")
    print(url + "\n")
    with_browser = False
    try:
        with_browser = webbrowser.open(url)
    except Exception:  # noqa: BLE001 — headless machines simply have none
        with_browser = False
    if not with_browser:
        print("(No browser available here — use the URL above, or --manual.)")

    thread.join(timeout=300)
    server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise SystemExit("Timed out waiting for the Google consent redirect.")
    if result.get("state") != state:
        raise SystemExit("Redirect state did not match — aborting.")
    if "error" in result:
        raise SystemExit(f"Google returned an error: {result['error']}")
    code = result.get("code", "")
    if not code:
        raise SystemExit("No authorization code in the redirect.")

    return _exchange_code(client_id, client_secret, code, redirect_uri)


def _manual_flow(client_id: str, client_secret: str, port: int) -> dict[str, object]:
    redirect_uri = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)
    url = _build_auth_url(client_id, redirect_uri, state)

    print("\n1. Open this URL in any browser and approve access:\n")
    print(url + "\n")
    print("2. The browser will fail to load 127.0.0.1 — that is expected.")
    print("   Copy the full address from the address bar (or just the code=… value).\n")
    pasted = input("Paste the redirected URL or code: ").strip()

    code = pasted
    if "code=" in pasted:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        code = (params.get("code") or [""])[0]
        returned_state = (params.get("state") or [""])[0]
        if returned_state and returned_state != state:
            raise SystemExit("Redirect state did not match — aborting.")
    if not code:
        raise SystemExit("No authorization code found in what you pasted.")

    return _exchange_code(client_id, client_secret, code, redirect_uri)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mint a Google Photos refresh token for Metixel Photoframe.",
    )
    parser.add_argument("--client-id", required=True, help="OAuth client ID (Desktop app)")
    parser.add_argument("--client-secret", required=True, help="OAuth client secret")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"loopback port for the redirect URI (default {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="print the URL and paste the result back (no local listener)",
    )
    args = parser.parse_args(argv)

    client_id = args.client_id.strip()
    client_secret = args.client_secret.strip()

    if args.manual:
        tokens = _manual_flow(client_id, client_secret, args.port)
    else:
        tokens = _loopback_flow(client_id, client_secret, args.port)

    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        print(
            "\nGoogle did not return a refresh token.  This happens when the "
            "account has already granted this client: remove the app's access at "
            "https://myaccount.google.com/permissions and run this again.",
            file=sys.stderr,
        )
        return 1

    print("\n" + "=" * 68)
    print("Paste these into the frame's dashboard → Sources → Google Photos:")
    print("=" * 68)
    print(f"Client ID     : {client_id}")
    print(f"Client Secret : {client_secret}")
    print(f"Refresh Token : {refresh_token}")
    print("=" * 68)
    print("Keep the refresh token secret — it grants read access to picked photos.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        raise SystemExit(130) from None
    except OSError as exc:  # pragma: no cover — surfaced to the user
        print(f"Network error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
