"""
Builds 5-min OHLCV candles for an instrument entirely server-side (3 lightweight
SQL queries), so raw ticks never get pulled into Python memory.

Usage: python build_5min_candles.py "NQ 06-26" [bar_minutes]
Writes nq<slug>_<N>min_candles.json, e.g. nq0626_5min_candles.json
"""
import sys
import os
import re
import json
import psycopg2

TABLE = "ticks_nq"


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "market_data"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ["POSTGRES_PASSWORD"],
    )


def slug(instrument):
    return re.sub(r'[^A-Za-z0-9]', '', instrument)


def build_candles(conn, instrument, bar_minutes):
    interval_sec = bar_minutes * 60
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT to_timestamp(floor(extract(epoch from ts_utc) / %s) * %s) AS bucket,
                   MAX(price) AS h, MIN(price) AS l, SUM(volume) AS v
            FROM {TABLE}
            WHERE instrument = %s
            GROUP BY bucket
            ORDER BY bucket
        """, (interval_sec, interval_sec, instrument))
        hlv = {row[0]: {"h": float(row[1]), "l": float(row[2]), "v": float(row[3])} for row in cur.fetchall()}

        cur.execute(f"""
            SELECT DISTINCT ON (bucket) bucket, price
            FROM (
                SELECT to_timestamp(floor(extract(epoch from ts_utc) / %s) * %s) AS bucket,
                       ts_utc, price
                FROM {TABLE}
                WHERE instrument = %s
            ) t
            ORDER BY bucket, ts_utc ASC
        """, (interval_sec, interval_sec, instrument))
        opens = {row[0]: float(row[1]) for row in cur.fetchall()}

        cur.execute(f"""
            SELECT DISTINCT ON (bucket) bucket, price
            FROM (
                SELECT to_timestamp(floor(extract(epoch from ts_utc) / %s) * %s) AS bucket,
                       ts_utc, price
                FROM {TABLE}
                WHERE instrument = %s
            ) t
            ORDER BY bucket, ts_utc DESC
        """, (interval_sec, interval_sec, instrument))
        closes = {row[0]: float(row[1]) for row in cur.fetchall()}

    candles = []
    for bucket in sorted(hlv.keys()):
        candles.append({
            "t": int(bucket.timestamp() * 1000),
            "o": opens[bucket],
            "h": hlv[bucket]["h"],
            "l": hlv[bucket]["l"],
            "c": closes[bucket],
            "v": hlv[bucket]["v"],
        })
    return candles


def main():
    instrument = sys.argv[1] if len(sys.argv) > 1 else "NQ 09-26"
    bar_minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    s = slug(instrument)

    conn = get_conn()
    try:
        print(f"Building {bar_minutes}-min candles for '{instrument}' from '{TABLE}'...")
        candles = build_candles(conn, instrument, bar_minutes)
    finally:
        conn.close()

    out_path = f"nq{s[-4:]}_{bar_minutes}min_candles.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(candles, f)
    print(f"Wrote {len(candles)} candles to {out_path}")


if __name__ == "__main__":
    main()
