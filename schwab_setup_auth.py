"""
One-time Schwab OAuth bootstrap.

Run this ONCE (directly in a real terminal, since it needs to prompt you
interactively -- not from an automated tool) to create schwab_token.json:

    & "C:\\Users\\tonya\\anaconda3\\envs\\my_quant_lab\\python.exe" schwab_setup_auth.py

It will print a URL, you open it, log into Schwab, approve the app, land on
the "can't be reached" callback page, and paste that page's full address
back in when prompted. That writes schwab_token.json next to this script.

After that, schwab_client.py (via easy_client) finds that token file and
just loads/refreshes it automatically -- no more browser flow, no more
worrying about the callback URL needing a port number. This script uses
client_from_manual_flow(), which -- unlike easy_client()'s default browser
flow -- works fine with a bare "https://127.0.0.1" callback (no port).

You'll need to redo this (rerun this script) once every 7 days, since
that's how long Schwab's refresh token lasts.
"""

import os

from dotenv import load_dotenv
from schwab.auth import client_from_manual_flow

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def main():
    api_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
    token_path = os.environ.get(
        "SCHWAB_TOKEN_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "schwab_token.json"),
    )

    if not api_key or not app_secret:
        raise SystemExit(
            "Missing credentials. Paste your App Key and App Secret into "
            "the .env file next to this script (SCHWAB_APP_KEY / "
            "SCHWAB_APP_SECRET) before running this."
        )

    client_from_manual_flow(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=callback_url,
        token_path=token_path,
    )
    print(f"\nDone -- token written to {token_path}")
    print("You can now run schwab_client.py normally.")


if __name__ == "__main__":
    main()
