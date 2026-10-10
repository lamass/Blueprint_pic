"""
Reports gaps in the "ticks" table for one instrument: days with zero ticks,
and intraday stretches with no ticks longer than a threshold (since NQ
trades ~24/5 on Globex, a multi-hour silent stretch during the week usually
means NinjaTrader wasn't connected/running, not a real quiet market).

Usage
-----
    python check_tick_gaps.py                 # instrument "NQ 09-26", 15-min threshold
    python check_tick_gaps.py "NQ 09-26" 30    # custom instrument / gap-minutes threshold
"""

import os
import sys
from datetime import timedelta

import psycopg2
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "market_data"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ["POSTGRES_PASSWORD"],
    )


def table_for_instrument(instrument):
    # matches the same per-root-symbol table naming TickRecorder.cs and
    # import_nt_tick_export.py use ("NQ 09-26" -> "ticks_nq")
    root = instrument.split(" ")[0]
    root = "".join(c for c in root if c.isalnum()).lower()
    return f"ticks_{root}"


def main():
    instrument = sys.argv[1] if len(sys.argv) > 1 else "NQ 09-26"
    gap_minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    table = table_for_instrument(instrument)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(f"SELECT MIN(ts_utc), MAX(ts_utc) FROM {table} WHERE instrument = %s", (instrument,))
    min_ts, max_ts = cur.fetchone()
    if min_ts is None:
        sys.exit(f"No rows at all for instrument '{instrument}' in table '{table}'.")
    print(f"Instrument '{instrument}' (table '{table}'): data spans {min_ts} to {max_ts} (UTC)\n")

    # --- missing calendar days (UTC) ---
    cur.execute(
        f"""
        SELECT d::date
        FROM generate_series(%s::date, %s::date, interval '1 day') AS d
        LEFT JOIN (
            SELECT DISTINCT date_trunc('day', ts_utc)::date AS day
            FROM {table} WHERE instrument = %s
        ) present ON present.day = d::date
        WHERE present.day IS NULL
        ORDER BY 1
        """,
        (min_ts.date(), max_ts.date(), instrument),
    )
    missing_days = [row[0] for row in cur.fetchall()]
    if missing_days:
        print(f"Fully missing days ({len(missing_days)}):")
        for d in missing_days:
            print(f"  {d}  <- likely weekend (Sat/Sun have no Globex session) unless this is a weekday")
    else:
        print("No fully missing calendar days in range.")
    print()

    # --- intraday gaps beyond threshold ---
    cur.execute(
        f"""
        SELECT ts_utc, prev_ts, ts_utc - prev_ts AS gap
        FROM (
            SELECT ts_utc, LAG(ts_utc) OVER (ORDER BY ts_utc) AS prev_ts
            FROM {table} WHERE instrument = %s
        ) t
        WHERE prev_ts IS NOT NULL AND ts_utc - prev_ts > %s
        ORDER BY ts_utc
        """,
        (instrument, timedelta(minutes=gap_minutes)),
    )
    gaps = cur.fetchall()
    if gaps:
        print(f"Intraday gaps longer than {gap_minutes} minutes ({len(gaps)}):")
        for ts_utc, prev_ts, gap in gaps:
            print(f"  {prev_ts}  ->  {ts_utc}   (gap: {gap})")
    else:
        print(f"No intraday gaps longer than {gap_minutes} minutes.")

    conn.close()


if __name__ == "__main__":
    main()
