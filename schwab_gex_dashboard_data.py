"""
Fetches NDX/SPX/SPY/QQQ option-chain + price data for the gamma-wall
dashboard, and computes per-strike GEX and raw open-interest levels.

Two separate per-strike level series are produced:

    oiLevelsByExp: {"current_day": [...], "next_day": [...], "current_month": [...]}
        oi(strike) = call.openInterest + put.openInterest, kept PER
        EXPIRATION (not summed across expirations). This is what drives the
        chart's heatmap tiers, and the chart always uses whichever
        expiration's list matches the table tab currently selected --
        summing across expirations here made the chart's top-ranked strikes
        disagree with the OI numbers visibly sitting in the on-screen table
        whenever a tab other than "next day" was open (this happened -- see
        conversation history). Raw OI, not GEX, within a single expiration --
        GEX (gamma * OI) can rank strikes *differently* than raw OI, because
        gamma itself varies by strike, which is the reason OI (not GEX)
        drives the heatmap at all.

    levels: GEX, still computed and shown per-contract in the table --
        callGEX(strike) = call.gamma * call.openInterest * multiplier
        putGEX(strike)  = put.gamma  * put.openInterest  * multiplier
        intensity = abs(callGEX) + abs(putGEX)
        A magnitude (concentration) measure, not signed net dealer exposure;
        deliberately sidesteps the dealer-long/short sign-convention debate
        different GEX tools disagree on. Still summed across current-day +
        next-day (this one is a standalone summary tile, not something a
        user visually cross-checks strike-by-strike against a single table
        tab, so the combined view is the more useful default here).

Table (separate from the levels) still shows one expiration at a time:
current day / next day / current month, same as before.

Run manually, or on a schedule (see run_gex_refresh.bat / Task Scheduler):
    & "C:\\Users\\tonya\\anaconda3\\envs\\my_quant_lab\\python.exe" schwab_gex_dashboard_data.py
"""

import datetime
import json
import os

import pandas as pd

from schwab_client import get_client

SYMBOLS = {"NDX": "$NDX", "SPX": "$SPX", "SPY": "SPY", "QQQ": "QQQ"}
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gex_dashboard_data.json")
STRIKE_COUNT = 60  # per side of ATM, per expiration fetched


def pick_expirations(expiration_list, today):
    by_date = {e["expirationDate"]: e for e in expiration_list}

    current_day = by_date.get(today.isoformat())

    next_day = None
    for delta in range(1, 5):
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
                "gamma": c.get("gamma", 0.0) or 0.0,
                "delta": c.get("delta", 0.0) or 0.0,
                "multiplier": c.get("multiplier", 100.0) or 100.0,
                "inTheMoney": c.get("inTheMoney", False),
            })
    out.sort(key=lambda r: r["strike"])
    return out


def fetch_expiration_slice(client, symbol, exp_date):
    resp = client.get_option_chain(
        symbol,
        contract_type=client.Options.ContractType.ALL,
        from_date=exp_date,
        to_date=exp_date,
        include_underlying_quote=True,
        strike_count=STRIKE_COUNT,
    )
    resp.raise_for_status()
    data = resp.json()
    calls = parse_exp_date_map(data.get("callExpDateMap", {}))
    puts = parse_exp_date_map(data.get("putExpDateMap", {}))
    return data, calls, puts


def gamma_exposure_by_strike(calls, puts):
    """callGEX/putGEX/combined |GEX| per strike, for one expiration's calls+puts."""
    by_strike = {}
    for row in calls:
        s = by_strike.setdefault(row["strike"], {"callGEX": 0.0, "putGEX": 0.0})
        s["callGEX"] += row["gamma"] * row["openInterest"] * row["multiplier"]
    for row in puts:
        s = by_strike.setdefault(row["strike"], {"callGEX": 0.0, "putGEX": 0.0})
        s["putGEX"] += row["gamma"] * row["openInterest"] * row["multiplier"]
    return by_strike


def combine_levels(*gex_maps):
    """Sum callGEX/putGEX across multiple expirations' gamma_exposure_by_strike()
    outputs, and compute the combined |GEX| intensity used for the heatmap."""
    combined = {}
    for gmap in gex_maps:
        for strike, vals in gmap.items():
            c = combined.setdefault(strike, {"callGEX": 0.0, "putGEX": 0.0})
            c["callGEX"] += vals["callGEX"]
            c["putGEX"] += vals["putGEX"]
    levels = [
        {"strike": k, "callGEX": v["callGEX"], "putGEX": v["putGEX"],
         "intensity": abs(v["callGEX"]) + abs(v["putGEX"])}
        for k, v in combined.items()
    ]
    levels.sort(key=lambda r: -r["intensity"])
    return levels


def oi_by_strike(calls, puts):
    """call OI and put OI per strike (kept separate), for one expiration's
    calls+puts -- the table shows these two numbers in separate columns, so
    the combined total downstream needs to carry the breakdown too, or a
    "combined" total looks unexplainably bigger than either column alone."""
    by_strike = {}
    for row in calls:
        s = by_strike.setdefault(row["strike"], {"callOi": 0, "putOi": 0})
        s["callOi"] += row["openInterest"]
    for row in puts:
        s = by_strike.setdefault(row["strike"], {"callOi": 0, "putOi": 0})
        s["putOi"] += row["openInterest"]
    return by_strike


def combine_oi_levels(*oi_maps):
    """Sum call/put OI across one or more oi_by_strike() outputs (usually
    called with just one expiration's map -- see oi_levels_by_exp in
    fetch_symbol()) into a single sorted list. The combined (call+put) total
    per strike -- not GEX -- is what drives the heatmap's color/tier: GEX
    (gamma * OI) can rank strikes differently than raw OI because gamma
    itself varies by strike, which is exactly the inversion this was built
    to fix (a lower-OI, higher-gamma strike outranking a higher-OI one)."""
    combined = {}
    for omap in oi_maps:
        for strike, vals in omap.items():
            c = combined.setdefault(strike, {"callOi": 0, "putOi": 0})
            c["callOi"] += vals["callOi"]
            c["putOi"] += vals["putOi"]
    levels = [
        {"strike": k, "callOi": v["callOi"], "putOi": v["putOi"], "oi": v["callOi"] + v["putOi"]}
        for k, v in combined.items()
    ]
    levels.sort(key=lambda r: -r["oi"])
    return levels


def fetch_symbol(client, key, api_symbol, today):
    print(f"=== {key} ({api_symbol}) ===")

    exp_resp = client.get_option_expiration_chain(api_symbol)
    exp_resp.raise_for_status()
    expiration_list = exp_resp.json().get("expirationList", [])
    picked = pick_expirations(expiration_list, today)

    result = {"underlyingPrice": None, "expirations": {}}
    gex_maps_for_levels = []
    oi_levels_by_exp = {}

    for label, entry in picked.items():
        if entry is None:
            print(f"  {label}: no matching expiration found, skipping")
            result["expirations"][label] = None
            continue

        exp_date_str = entry["expirationDate"]
        exp_date = datetime.date.fromisoformat(exp_date_str)
        print(f"  {label}: {exp_date_str} (type={entry.get('expirationType')}, "
              f"{entry.get('daysToExpiration')}d out)")

        data, calls, puts = fetch_expiration_slice(client, api_symbol, exp_date)
        if result["underlyingPrice"] is None:
            result["underlyingPrice"] = data.get("underlyingPrice")

        result["expirations"][label] = {
            "date": exp_date_str,
            "daysToExpiration": entry.get("daysToExpiration"),
            "expirationType": entry.get("expirationType"),
            "calls": calls,
            "puts": puts,
        }
        print(f"    -> {len(calls)} call strikes, {len(puts)} put strikes")

        if label in ("current_day", "next_day"):
            gex_maps_for_levels.append(gamma_exposure_by_strike(calls, puts))
        # kept per-expiration (not summed) so the chart can match whichever
        # table tab is selected -- see combine_oi_levels() docstring
        oi_levels_by_exp[label] = combine_oi_levels(oi_by_strike(calls, puts))

    result["levels"] = combine_levels(*gex_maps_for_levels)
    result["oiLevelsByExp"] = oi_levels_by_exp
    print(f"  combined GEX levels (current+next day): {len(result['levels'])} strikes")
    for label, lvls in oi_levels_by_exp.items():
        print(f"  OI levels ({label}): {len(lvls)} strikes")

    print("  fetching recent 1-minute price history...")
    hist_resp = client.get_price_history_every_minute(api_symbol)
    hist_resp.raise_for_status()
    candles = hist_resp.json().get("candles", [])
    MAX_BARS = 2000
    candles = candles[-MAX_BARS:]
    result["priceBars1m"] = [
        [c["datetime"], c["open"], c["high"], c["low"], c["close"], c["volume"]]
        for c in candles
    ]
    print(f"    -> {len(result['priceBars1m'])} 1-minute bars")

    result["priceBars5m"] = resample_5min(result["priceBars1m"])
    print(f"    -> {len(result['priceBars5m'])} 5-minute bars (resampled)")

    return result


def resample_5min(bars_1m):
    """[t,o,h,l,c,v] 1-minute bars -> 5-minute bars, aligned to wall-clock
    5-minute buckets (not just every-5th-row), same convention as the
    earlier NQ tick-chart work in this project."""
    if not bars_1m:
        return []
    df = pd.DataFrame(bars_1m, columns=["t", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["t"], unit="ms")
    df = df.set_index("dt")
    ohlc = df["close"].resample("5min").ohlc()
    ohlc["open"] = df["open"].resample("5min").first()
    ohlc["high"] = df["high"].resample("5min").max()
    ohlc["low"] = df["low"].resample("5min").min()
    ohlc["volume"] = df["volume"].resample("5min").sum()
    ohlc = ohlc.dropna(subset=["open"])
    ohlc["t"] = ohlc.index.astype("int64") // 10**6
    return ohlc[["t", "open", "high", "low", "close", "volume"]].values.tolist()


def main():
    client = get_client()
    today = datetime.date.today()

    out = {
        "generatedAt": datetime.datetime.now().isoformat(),
        "methodology": (
            "GEX per strike = gamma * openInterest * multiplier, summed across "
            "calls and puts and across the current-day + next-day expirations "
            "only. Heatmap intensity = abs(callGEX) + abs(putGEX) (magnitude "
            "of concentration, not signed net dealer exposure)."
        ),
        "symbols": {},
    }
    for key, api_symbol in SYMBOLS.items():
        out["symbols"][key] = fetch_symbol(client, key, api_symbol, today)

    # QQQ tracks NDX (and SPY tracks SPX) as a share-price fraction of the
    # index, not a fixed point spread -- so translating an ETF price to its
    # futures equivalent (NQ, ES) is a multiplication by a ratio, not an
    # additive offset. Computed live each refresh from the two underlying
    # quotes we already have (NDX/SPX's quote itself is fine -- it's only
    # options OI/greeks that Schwab doesn't populate for indices, see
    # conversation history) rather than a hardcoded constant, since the
    # ETF/index ratio drifts slowly over time (dividends, expense drag).
    # NQ/ES themselves track their cash index within a small basis (a
    # handful of points for a near-month future) that this doesn't model.
    FUTURES_PROXY_FOR = {"QQQ": ("NDX", "NQ"), "SPY": ("SPX", "ES")}
    for etf_key, (index_key, futures_label) in FUTURES_PROXY_FOR.items():
        etf_price = out["symbols"].get(etf_key, {}).get("underlyingPrice")
        index_price = out["symbols"].get(index_key, {}).get("underlyingPrice")
        if etf_price and index_price:
            out["symbols"][etf_key]["futuresProxy"] = {
                "label": futures_label,
                "ratio": index_price / etf_price,
            }
        # the index itself gets the same translation, just without the ETF
        # ratio step (it's already the cash index) -- ratio 1.0, same manual
        # basis field ("NQ"/"ES") so entering it once on either the ETF tab
        # or the index tab carries over to the other, since it's the same
        # real futures-vs-cash-index gap either way
        if index_price:
            out["symbols"][index_key]["futuresProxy"] = {
                "label": futures_label,
                "ratio": 1.0,
            }

    # atomic write: a same-directory temp file + os.replace() so a reader
    # (build_html(), possibly a concurrent run of this same script if the
    # 5-minute Task Scheduler cadence ever overlaps a slow fetch) never sees
    # a half-written file -- os.replace() is a single atomic filesystem
    # rename on both Windows and POSIX, unlike writing OUT_PATH directly
    tmp_path = OUT_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp_path, OUT_PATH)
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
