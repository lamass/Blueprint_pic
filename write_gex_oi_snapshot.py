"""
Writes one row per (symbol, expiration_type, strike) into the shared
"gex_oi_snapshots" Postgres table, from the latest gex_dashboard_data.json
snapshot -- same "market_data" database the NQ/ES tick tables live in, so a
backtest can JOIN GEX/OI history against NQ/ES ticks by timestamp without
crossing databases.

Filter (per-row, so a strike survives if EITHER condition holds -- see the
"GEX=0 but OI matters" discussion this schema is built around):
    |call_gex| >= GEX_FLOOR  OR  |put_gex| >= GEX_FLOOR
    OR call_oi >= OI_FLOOR[expiration_type]  OR put_oi >= OI_FLOOR[expiration_type]
A strike far from the money can have ~zero gamma (so ~zero GEX) while still
sitting on thousands of contracts of resting open interest -- a real level
the GEX-only view would otherwise silently drop. OI_FLOOR is per expiration
type because current_month OI naturally runs much higher than
current_day/next_day (see conversation: current_month OI commonly 1000-3000
vs. a few hundred on the weeklies) -- one fixed floor across all three would
either flood current_month or filter out everything on the weeklies.

generatedAt in the JSON is a naive local timestamp (America/New_York, same
zone as NinjaTrader/TickRecorder) -- converted to true UTC here via zoneinfo
(DST-aware), matching the ts_utc convention already used in ticks_nq/ticks_es.

Usage
-----
    python write_gex_oi_snapshot.py                         # uses gex_dashboard_data.json
                                                              # in this folder
    python write_gex_oi_snapshot.py path\to\other_data.json
"""

import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

NT_DISPLAY_TZ = ZoneInfo("America/New_York")
GEX_FLOOR = 5000
OI_FLOOR = {
    "current_day": 500,
    "next_day": 500,
    "current_month": 1000,
}
TABLE = "gex_oi_snapshots"


def get_conn():
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        sys.exit("Missing POSTGRES_PASSWORD in .env")
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "market_data"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=password,
    )


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id BIGSERIAL PRIMARY KEY,
                snapshot_ts TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                expiration_type TEXT NOT NULL,
                expiration_date DATE NOT NULL,
                days_to_expiration INT,
                underlying_price DOUBLE PRECISION,
                strike DOUBLE PRECISION NOT NULL,
                call_oi BIGINT,
                call_gamma DOUBLE PRECISION,
                call_gex DOUBLE PRECISION,
                call_volume BIGINT,
                put_oi BIGINT,
                put_gamma DOUBLE PRECISION,
                put_gex DOUBLE PRECISION,
                put_volume BIGINT
            );
            CREATE INDEX IF NOT EXISTS idx_{TABLE}_symbol_time
                ON {TABLE} (symbol, snapshot_ts);
            CREATE INDEX IF NOT EXISTS idx_{TABLE}_symbol_exp_strike
                ON {TABLE} (symbol, expiration_type, strike, snapshot_ts);
            """
        )
    conn.commit()


def parse_snapshot_ts(generated_at):
    dt = datetime.fromisoformat(generated_at)
    return dt.replace(tzinfo=NT_DISPLAY_TZ).astimezone(timezone.utc)


def by_strike(options_list):
    out = {}
    for opt in options_list:
        out[opt["strike"]] = opt
    return out


def gex(opt):
    if opt is None:
        return 0.0, 0.0, 0
    g = opt.get("gamma") or 0.0
    oi = opt.get("openInterest") or 0
    mult = opt.get("multiplier") or 100.0
    return g, g * oi * mult, oi


def rows_for_symbol(symbol, sym_data, snapshot_ts):
    underlying_price = sym_data.get("underlyingPrice")
    for exp_type, exp in (sym_data.get("expirations") or {}).items():
        if exp is None:
            continue
        oi_floor = OI_FLOOR.get(exp_type, 500)
        calls = by_strike(exp.get("calls") or [])
        puts = by_strike(exp.get("puts") or [])
        strikes = sorted(set(calls) | set(puts))
        for strike in strikes:
            call, put = calls.get(strike), puts.get(strike)
            call_gamma, call_gex, call_oi = gex(call)
            put_gamma, put_gex, put_oi = gex(put)

            if not (
                abs(call_gex) >= GEX_FLOOR
                or abs(put_gex) >= GEX_FLOOR
                or call_oi >= oi_floor
                or put_oi >= oi_floor
            ):
                continue

            yield (
                snapshot_ts.isoformat(),
                symbol,
                exp_type,
                exp["date"],
                exp.get("daysToExpiration"),
                underlying_price,
                strike,
                call_oi,
                call_gamma,
                call_gex,
                (call or {}).get("volume") or 0,
                put_oi,
                put_gamma,
                put_gex,
                (put or {}).get("volume") or 0,
            )


def write_snapshot(conn, data):
    snapshot_ts = parse_snapshot_ts(data["generatedAt"])
    total = 0
    with conn.cursor() as cur:
        for symbol, sym_data in data["symbols"].items():
            rows = list(rows_for_symbol(symbol, sym_data, snapshot_ts))
            if not rows:
                continue
            cur.executemany(
                f"""
                INSERT INTO {TABLE} (
                    snapshot_ts, symbol, expiration_type, expiration_date,
                    days_to_expiration, underlying_price, strike,
                    call_oi, call_gamma, call_gex, call_volume,
                    put_oi, put_gamma, put_gex, put_volume
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                rows,
            )
            total += len(rows)
            print(f"  {symbol}: {len(rows)} rows")
    conn.commit()
    print(f"Wrote {total} rows for snapshot at {snapshot_ts.isoformat()}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "gex_dashboard_data.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    conn = get_conn()
    try:
        ensure_schema(conn)
        write_snapshot(conn, data)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
