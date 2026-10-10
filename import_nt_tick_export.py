"""
Bulk-loads NinjaTrader "Last" tick-export .txt files into the same Postgres
"ticks" table that TickRecorder.cs writes to live, so historical (exported)
and live-recorded ticks sit in one table, one schema, ready to backtest
together.

NinjaTrader's exported format (e.g. "NQ 09-26_Tick_Last.txt") is:
    yyyyMMdd HHmmss fffffff;Price;Bid;Ask;Volume
one row per trade print, semicolon-delimited, no header row. The instrument
isn't a column in the file -- it's implied by the filename -- and there's no
"side" column, so both are derived here:
  - instrument: filename with the "_Tick_Last.txt" suffix stripped
  - side: same tick-rule TickRecorder.cs uses live (price>=ask -> buy,
    price<=bid -> sell, else midpoint), applied to each row's own bid/ask

Timezone: NOT fixed across exports -- verified per-file, not assumed.
The original "*_Tick_Last.txt" exports use NinjaTrader's configured display
zone, which for this account was "(UTC-05:00) Eastern Time (US & Canada)"
i.e. America/New_York -- NOT a fixed -5, since it observes DST.

A later export ("NQ 12-26_Sep13-Oct2.txt", a different export path/tool, not
*_Tick_Last.txt) turned out to be in UTC instead: its daily gaps sit at
20:59:59->22:00:00 local-file-clock, and CME's real daily Globex halt is
17:00-18:00 ET -- 21:00-22:00 UTC during EDT (UTC-4) matches exactly, Eastern
would not. Confirmed against the known Friday-17:00-ET-close/Sunday-18:00-ET
-reopen weekly gap too. So: don't assume a zone, check the gap pattern against
real CME hours first, and pass --utc-timestamps when a file turns out to
already be UTC (pass no flag for the original America/New_York-zoned files).
Either way this script converts properly before storing, matching
TickRecorder.cs's ts_utc (true UTC) column so live and backfilled rows line
up correctly.

Usage
-----
    python import_nt_tick_export.py                  # imports every
                                                       # *_Tick_Last.txt in
                                                       # this folder
    python import_nt_tick_export.py "NQ 09-26_Tick_Last.txt"   # one file
    python import_nt_tick_export.py --utc-timestamps "NQ 12-26_Sep13-Oct2.txt"
                                                       # file's timestamps are
                                                       # already UTC -- skip
                                                       # the Eastern conversion
    python import_nt_tick_export.py --force ...       # re-import even if
                                                        # that instrument
                                                        # already has rows
                                                        # (will create
                                                        # duplicates -- only
                                                        # use this if you
                                                        # know what you're
                                                        # doing)
"""

import csv
import glob
import io
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

NT_DISPLAY_TZ = ZoneInfo("America/New_York")
BATCH_SIZE = 200_000
SUFFIX = "_Tick_Last.txt"
INSTRUMENT_PATTERN = re.compile(r"^([A-Za-z]+ \d{2}-\d{2})")


def get_conn():
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "market_data")
    user = os.environ.get("POSTGRES_USER", "postgres")
    password = os.environ.get("POSTGRES_PASSWORD")
    if not password:
        sys.exit(
            "Missing POSTGRES_PASSWORD. Fill it into the .env file next to "
            "this script (same password you set for the postgres user)."
        )
    return psycopg2.connect(host=host, port=port, dbname=db, user=user, password=password)


def instrument_from_filename(path):
    name = os.path.basename(path)
    if name.endswith(SUFFIX):
        return name[: -len(SUFFIX)]
    m = INSTRUMENT_PATTERN.match(name)
    if m:
        return m.group(1)
    return os.path.splitext(name)[0]


def table_for_instrument(instrument):
    # "NQ 09-26" -> "ticks_nq", "ES 12-26" -> "ticks_es" -- matches the same
    # per-root-symbol table naming TickRecorder.cs uses live, so historical
    # imports land in the same table the live recorder writes to.
    root = instrument.split(" ")[0]
    root = "".join(c for c in root if c.isalnum()).lower()
    return f"ticks_{root}"


def parse_ts_to_utc(ts_field, source_is_utc=False):
    # "20260831 040000 2920000" -> date, time, 100ns-tick fraction of that second
    date_str, time_str, frac_str = ts_field.split(" ")
    dt = datetime.strptime(date_str + time_str, "%Y%m%d%H%M%S")
    frac_seconds = int(frac_str) / 1e7
    dt = dt.replace(microsecond=int(round(frac_seconds * 1_000_000)))
    if source_is_utc:
        return dt.replace(tzinfo=timezone.utc)
    dt_eastern = dt.replace(tzinfo=NT_DISPLAY_TZ)
    return dt_eastern.astimezone(timezone.utc)


def classify_side(price, bid, ask):
    mid = (bid + ask) / 2.0
    if ask > 0 and price >= ask:
        return "buy"
    if bid > 0 and price <= bid:
        return "sell"
    if bid > 0 and ask > 0:
        return "buy" if price >= mid else "sell"
    return "unknown"


def needs_import(ts_utc, coverage):
    """coverage is None (nothing in DB yet) or a (global_min, global_max, gaps)
    tuple, where gaps is a list of (gap_start, gap_end) exclusive intervals
    that are currently NOT covered by any existing row. A timestamp needs
    importing if it's outside the DB's covered span entirely, or falls inside
    one of those known gaps -- anything else is already represented."""
    if coverage is None:
        return True
    global_min, global_max, gaps = coverage
    if ts_utc < global_min or ts_utc > global_max:
        return True
    return any(gap_start < ts_utc < gap_end for gap_start, gap_end in gaps)


def rows_from_file(path, instrument, coverage=None, source_is_utc=False):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ts_field, price_s, bid_s, ask_s, vol_s = line.split(";")
            price, bid, ask = float(price_s), float(bid_s), float(ask_s)
            volume = int(vol_s)
            ts_utc = parse_ts_to_utc(ts_field, source_is_utc=source_is_utc)
            if not needs_import(ts_utc, coverage):
                continue
            side = classify_side(price, bid, ask)
            yield (instrument, ts_utc.isoformat(), price, volume, bid, ask, side)


def copy_batch(conn, table, batch):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(batch)
    buf.seek(0)
    with conn.cursor() as cur:
        cur.copy_expert(
            f"COPY {table} (instrument, ts_utc, price, volume, bid, ask, side) "
            "FROM STDIN WITH (FORMAT csv)",
            buf,
        )
    conn.commit()


def ensure_schema(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                id BIGSERIAL PRIMARY KEY,
                instrument TEXT NOT NULL,
                ts_utc TIMESTAMPTZ NOT NULL,
                price DOUBLE PRECISION,
                volume BIGINT,
                bid DOUBLE PRECISION,
                ask DOUBLE PRECISION,
                side TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_{table}_instrument_time
                ON {table} (instrument, ts_utc);
            """
        )
    conn.commit()


def get_coverage(conn, table, instrument, min_gap_minutes=5):
    """Returns None if the instrument has no rows yet, else
    (global_min_ts, global_max_ts, gaps) where gaps is a list of
    (prev_ts, ts_utc) intervals wider than min_gap_minutes -- i.e. the
    stretches genuinely missing from what's already in Postgres."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM {table} WHERE instrument = %s",
            (instrument,),
        )
        count, min_ts, max_ts = cur.fetchone()
        if not count:
            return None

        cur.execute(
            f"""
            SELECT prev_ts, ts_utc FROM (
                SELECT ts_utc, LAG(ts_utc) OVER (ORDER BY ts_utc) AS prev_ts
                FROM {table} WHERE instrument = %s
            ) t
            WHERE prev_ts IS NOT NULL AND ts_utc - prev_ts > %s
            ORDER BY prev_ts
            """,
            (instrument, timedelta(minutes=min_gap_minutes)),
        )
        gaps = cur.fetchall()
        return min_ts, max_ts, gaps


def import_file(conn, path, force, source_is_utc=False):
    instrument = instrument_from_filename(path)
    table = table_for_instrument(instrument)
    ensure_schema(conn, table)
    coverage = None if force else get_coverage(conn, table, instrument)

    if coverage is not None:
        global_min, global_max, gaps = coverage
        print(
            f"Existing coverage for '{instrument}': {global_min} -> {global_max} UTC, "
            f"with {len(gaps)} known internal gap(s) above 5 min. Import will only "
            f"write rows outside that span or inside one of those gaps -- everything "
            f"else is skipped as already present."
        )
    elif force:
        print(f"--force given: importing every row from {path} with no de-duplication check.")
    else:
        print(f"No existing rows for '{instrument}' -- importing everything.")

    tz_note = "already UTC" if source_is_utc else "America/New_York -> UTC"
    print(f"Importing {path} as instrument '{instrument}' into table '{table}' (timestamps: {tz_note})...")
    batch = []
    total = 0
    for row in rows_from_file(path, instrument, coverage=coverage, source_is_utc=source_is_utc):
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            copy_batch(conn, table, batch)
            total += len(batch)
            print(f"  ...{total} rows")
            batch = []
    if batch:
        copy_batch(conn, table, batch)
        total += len(batch)
    print(f"  done: {total} rows written for '{instrument}' in '{table}'.")


def main():
    args = sys.argv[1:]
    force = "--force" in args
    source_is_utc = "--utc-timestamps" in args
    files = [a for a in args if a not in ("--force", "--utc-timestamps")]
    if not files:
        files = sorted(glob.glob(os.path.join(HERE, f"*{SUFFIX}")))
    if not files:
        sys.exit(f"No *{SUFFIX} files found in {HERE} and none given on the command line.")

    conn = get_conn()
    try:
        for path in files:
            import_file(conn, path, force, source_is_utc=source_is_utc)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
