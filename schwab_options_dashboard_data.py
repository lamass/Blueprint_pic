"""
Fetches option-chain + price data for QQQ and SPY (Nasdaq-100 / S&P 500
proxies for NQ/ES -- Schwab's public API doesn't expose futures options,
confirmed by live testing against /NQ and /ES, which return no data) and
writes options_dashboard_data.json for the companion web dashboard to read.

Table covers three expirations per symbol: the nearest listed expiration
dated today ("current day"), the nearest dated tomorrow ("next day"), and
the nearest standard monthly cycle ("current month", expirationType == "S"
in Schwab's expiration-chain response -- confirmed live against AAPL).

"Activity" for the chart's price-level overlay = volume + open interest,
summed across calls and puts at each strike (see the "Activity metric"
choice made when this was scoped).

Run manually whenever you want fresh data:
    & "C:\\Users\\tonya\\anaconda3\\envs\\my_quant_lab\\python.exe" schwab_options_dashboard_data.py
"""

import datetime
import json
import os

from schwab_client import get_client

SYMBOLS = ["QQQ", "SPY"]
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "options_dashboard_data.json")


def pick_expirations(expiration_list, today):
    """Return {'current_day': entry|None, 'next_day': entry|None, 'current_month': entry|None}."""
    by_date = {e["expirationDate"]: e for e in expiration_list}

    current_day = by_date.get(today.isoformat())

    next_day = None
    for delta in range(1, 5):  # walk forward past a weekend if needed
        candidate = today + datetime.timedelta(days=delta)
        if candidate.isoformat() in by_date:
            next_day = by_date[candidate.isoformat()]
            break

    monthlies = sorted(
        (e for e in expiration_list if e.get("expirationType") == "S"
         and e["expirationDate"] >= today.isoformat()),
        key=lambda e: e["expirationDate"],
    )
    current_month = monthlies[0] if monthlies else None

    return {"current_day": current_day, "next_day": next_day, "current_month": current_month}


def parse_exp_date_map(exp_date_map):
    """Schwab's callExpDateMap/putExpDateMap keys look like '2026-09-04:1'
    (date:daysToExpiration) -> {strike_str: [option_obj]}. Flatten to a
    list of {strike, bid, ask, last, volume, openInterest}."""
    out = []
    for _exp_key, strikes in exp_date_map.items():
        for strike_str, contracts in strikes.items():
            c = contracts[0]
            out.append({
                "strike": float(strike_str),
                "bid": c.get("bid", 0.0),
                "ask": c.get("ask", 0.0),
                "last": c.get("last", 0.0),
                "volume": c.get("totalVolume", 0) or 0,
                "openInterest": c.get("openInterest", 0) or 0,
                "inTheMoney": c.get("inTheMoney", False),
            })
    out.sort(key=lambda r: r["strike"])
    return out


def compute_levels(calls, puts):
    """Combined call+put activity (volume + open interest) per strike."""
    by_strike = {}
    for row in calls:
        by_strike.setdefault(row["strike"], 0)
        by_strike[row["strike"]] += row["volume"] + row["openInterest"]
    for row in puts:
        by_strike.setdefault(row["strike"], 0)
        by_strike[row["strike"]] += row["volume"] + row["openInterest"]
    levels = [{"strike": k, "activity": v} for k, v in by_strike.items()]
    levels.sort(key=lambda r: -r["activity"])
    return levels


def fetch_expiration_slice(client, symbol, exp_date, strike_count=40):
    resp = client.get_option_chain(
        symbol,
        contract_type=client.Options.ContractType.ALL,
        from_date=exp_date,
        to_date=exp_date,
        include_underlying_quote=True,
        strike_count=strike_count,  # N strikes above + N below ATM -- plenty for a chain table
    )
    resp.raise_for_status()
    data = resp.json()
    calls = parse_exp_date_map(data.get("callExpDateMap", {}))
    puts = parse_exp_date_map(data.get("putExpDateMap", {}))
    return data, calls, puts


def fetch_symbol(client, symbol, today):
    print(f"=== {symbol} ===")

    exp_resp = client.get_option_expiration_chain(symbol)
    exp_resp.raise_for_status()
    expiration_list = exp_resp.json().get("expirationList", [])
    picked = pick_expirations(expiration_list, today)

    result = {"underlyingPrice": None, "expirations": {}}

    for label, entry in picked.items():
        if entry is None:
            print(f"  {label}: no matching expiration found, skipping")
            result["expirations"][label] = None
            continue

        exp_date_str = entry["expirationDate"]
        exp_date = datetime.date.fromisoformat(exp_date_str)
        print(f"  {label}: {exp_date_str} (type={entry.get('expirationType')}, "
              f"{entry.get('daysToExpiration')}d out)")

        data, calls, puts = fetch_expiration_slice(client, symbol, exp_date)
        if result["underlyingPrice"] is None:
            result["underlyingPrice"] = data.get("underlyingPrice")

        result["expirations"][label] = {
            "date": exp_date_str,
            "daysToExpiration": entry.get("daysToExpiration"),
            "expirationType": entry.get("expirationType"),
            "calls": calls,
            "puts": puts,
            "levels": compute_levels(calls, puts),
        }
        print(f"    -> {len(calls)} call strikes, {len(puts)} put strikes")

    print("  fetching recent 1-minute price history...")
    hist_resp = client.get_price_history_every_minute(symbol)
    hist_resp.raise_for_status()
    candles = hist_resp.json().get("candles", [])
    # Keep the dashboard payload a reasonable size -- last ~5 trading days'
    # worth of 1-minute bars is plenty for the chart; older bars aren't
    # useful here anyway since the levels chart is about *current* activity.
    MAX_BARS = 2000
    candles = candles[-MAX_BARS:]
    result["priceBars"] = [
        [c["datetime"], c["open"], c["high"], c["low"], c["close"], c["volume"]]
        for c in candles
    ]
    print(f"    -> {len(result['priceBars'])} 1-minute bars (most recent, capped at {MAX_BARS})")

    return result


def main():
    client = get_client()
    today = datetime.date.today()

    out = {
        "generatedAt": datetime.datetime.now().isoformat(),
        "activityMetric": "volume + open interest (combined, summed across calls+puts per strike)",
        "symbols": {},
    }
    for symbol in SYMBOLS:
        out["symbols"][symbol] = fetch_symbol(client, symbol, today)

    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
