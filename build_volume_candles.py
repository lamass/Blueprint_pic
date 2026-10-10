"""
Builds volume-bucketed OHLCV candles (e.g. NT8's "Volume" bar type at a given size)
by streaming ticks from Postgres in bounded-memory chunks -- volume bucketing needs
an ordered running total, so unlike time-bucketed candles this can't be done as a
single SQL aggregate.

Timestamps: stored ts_utc is mislabeled (+8h vs true NT8 Eastern time, found and
corrected earlier in this project) -- date-range bounds here are given in true NT8
Eastern and converted to the DB's raw stored instant internally (+8h, expressed as
explicit UTC-offset literals so the conversion is unambiguous regardless of session
timezone). Output candle 't' values are shifted -8h so they read correctly (as if
UTC) when loaded directly into the established chart tooling.

Usage: python build_volume_candles.py "NQ 09-26" 1000 2026-09-05 2026-09-16
  (instrument, bucket volume, start date, end date -- both true NT8 Eastern, end exclusive)
"""
import os
import sys
import json
from datetime import datetime, timezone, timedelta

import numpy as np
import psycopg2
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

TABLE = "ticks_nq"
CHUNK_SIZE = 500_000
OFFSET_MS = 8 * 3600 * 1000


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "market_data"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ["POSTGRES_PASSWORD"],
    )


def build_volume_candles(conn, instrument, bucket_volume, start_et, end_et):
    # true-Eastern range -> raw stored instant is +8h, expressed as explicit UTC
    # offsets so Postgres compares true instants regardless of session timezone
    start_raw = (start_et + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S+00")
    end_raw = (end_et + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S+00")

    cur = conn.cursor(name="volbar_tick_stream")
    cur.itersize = CHUNK_SIZE
    cur.execute(
        f"SELECT ts_utc, price, volume FROM {TABLE} "
        f"WHERE instrument = %s AND ts_utc >= %s::timestamptz AND ts_utc < %s::timestamptz "
        f"ORDER BY ts_utc",
        (instrument, start_raw, end_raw),
    )

    candles = []
    bucket_open = bucket_high = bucket_low = bucket_close = None
    bucket_vol = 0
    bucket_ts = None
    rows_seen = 0

    while True:
        rows = cur.fetchmany(CHUNK_SIZE)
        if not rows:
            break
        for ts, price, vol in rows:
            price = float(price)
            vol = int(vol)
            if bucket_open is None:
                bucket_open = price
                bucket_high = price
                bucket_low = price
            else:
                if price > bucket_high: bucket_high = price
                if price < bucket_low: bucket_low = price
            bucket_close = price
            bucket_ts = ts
            bucket_vol += vol

            if bucket_vol >= bucket_volume:
                candles.append({
                    "t": int(bucket_ts.timestamp() * 1000) - OFFSET_MS,
                    "o": round(bucket_open, 2), "h": round(bucket_high, 2),
                    "l": round(bucket_low, 2), "c": round(bucket_close, 2),
                    "v": bucket_vol,
                })
                bucket_open = bucket_high = bucket_low = bucket_close = None
                bucket_vol = 0
        rows_seen += len(rows)
        print(f"  ...{rows_seen:,} ticks streamed, {len(candles):,} bars built", flush=True)

    cur.close()
    return candles


def main():
    args = sys.argv[1:]
    instrument = args[0] if len(args) > 0 else "NQ 09-26"
    bucket_volume = int(args[1]) if len(args) > 1 else 1000
    start_str = args[2] if len(args) > 2 else "2026-09-05"
    end_str = args[3] if len(args) > 3 else "2026-09-16"

    start_et = datetime.strptime(start_str, "%Y-%m-%d")
    end_et = datetime.strptime(end_str, "%Y-%m-%d")

    conn = get_conn()
    try:
        print(f"Building {bucket_volume}-volume bars for '{instrument}', {start_str} to {end_str} (NT8 Eastern)...")
        candles = build_volume_candles(conn, instrument, bucket_volume, start_et, end_et)
    finally:
        conn.close()

    s = instrument.replace(" ", "").replace("-", "")
    out_path = os.path.join(HERE, f"{s.lower()}_{bucket_volume}vol_candles.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(candles, f, separators=(",", ":"))
    print(f"Wrote {len(candles):,} candles to {out_path}")


if __name__ == "__main__":
    main()
