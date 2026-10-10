"""
Schwab (thinkorswim) API auth + client helper, built on schwab-py.

One-time setup
---------------
1. Your app in the Schwab Developer Portal must show status "Ready For Use"
   (not "Approved - Pending") before any of this will work.
2. Open the ".env" file next to this script (plain text, already gitignored)
   and paste your App Key / App Secret into it -- do NOT hard code them in
   this .py file, and never paste them into chat or commit them anywhere.
   ".env" is loaded automatically below via python-dotenv, so once it's
   filled in you don't need to set anything by hand in the terminal again.

3. Run schwab_setup_auth.py ONCE, directly in a terminal (it needs to
   prompt you interactively):
       python schwab_setup_auth.py
   Follow the printed instructions: open the URL it gives you, log in with
   your real Schwab brokerage credentials (not the developer-portal login),
   approve the app, then paste the FULL address of the resulting "can't be
   reached" callback page back into the terminal when it asks. This writes
   schwab_token.json next to this script. (This uses schwab-py's manual
   copy-paste flow, which is what actually works with a bare
   "https://127.0.0.1" callback URL with no port number -- the automatic
   browser-capture flow below requires a port, e.g. "https://127.0.0.1:8182"
   matched in both the Developer Portal and SCHWAB_CALLBACK_URL, if you'd
   rather set that up instead.)
4. From then on, just run this file (schwab_client.py) normally -- it finds
   schwab_token.json and silently loads/refreshes it, no browser step. The
   refresh token lasts 7 days; after that, rerun schwab_setup_auth.py once
   to get a new one.

Coverage note
-------------
Schwab's Market Data API does cover CME futures (e.g. /NQ, /ES) -- use the
standard "/SYMBOL" notation. Per schwab-py's own docs, get_quote() (singular)
is "not recommended" for symbols with non-alphanumeric characters like the
leading slash; use get_quotes() (plural, takes a list) instead, which is
built to handle them. This does NOT mean it replicates the Bookmap tick-file
pipeline used elsewhere in this project -- it's live top-of-book quote data
from your brokerage feed, not raw last-trade ticks with bid/ask on every
print, and NinjaTrader/your Globex feed remains the source for that.
"""

import os
import sys

from dotenv import load_dotenv
from schwab.auth import easy_client

# Loads SCHWAB_APP_KEY / SCHWAB_APP_SECRET / SCHWAB_CALLBACK_URL from the
# ".env" file next to this script into the environment, if present. An
# already-set real environment variable (e.g. from $env:... in the terminal)
# still takes precedence over .env.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def get_client():
    api_key = os.environ.get("SCHWAB_APP_KEY")
    app_secret = os.environ.get("SCHWAB_APP_SECRET")
    callback_url = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
    token_path = os.environ.get(
        "SCHWAB_TOKEN_PATH",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "schwab_token.json"),
    )

    if not api_key or not app_secret:
        sys.exit(
            "Missing credentials. Paste your App Key and App Secret into "
            "the .env file next to this script (SCHWAB_APP_KEY / "
            "SCHWAB_APP_SECRET), or set them as environment variables."
        )

    return easy_client(
        api_key=api_key,
        app_secret=app_secret,
        callback_url=callback_url,
        token_path=token_path,
    )


if __name__ == "__main__":
    client = get_client()

    # Smoke test: pull a quote to confirm the token/auth pipeline works end
    # to end. get_quotes() (plural) is what schwab-py recommends for symbols
    # with a leading slash like futures -- get_quote() (singular) is fine for
    # plain equity tickers such as "AAPL" but not for "/NQ".
    symbols = ["/NQ", "AAPL"]
    resp = client.get_quotes(symbols)
    resp.raise_for_status()
    print(f"Quotes for {symbols}:")
    print(resp.json())
