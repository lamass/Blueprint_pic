"""
Phase 1 of the VPIN order-flow project (see project_vpin_orderflow memory):
validate whether VPIN (volume-synchronized probability of informed trading)
actually distinguishes informed/big-money NQ flow from noise, BEFORE it's
trusted as a live feature for the RL system.

Method
------
1. Pull ticks from Postgres (price, volume, side -- side is the tick-rule
   classification TickRecorder.cs/import_nt_tick_export.py already computed
   and stored per tick, so it's reused here rather than reclassified).
2. Bucket by VOLUME, not time (the "V" in VPIN): each bucket accumulates
   ticks until BUCKET_VOLUME contracts have traded, regardless of how much
   wall-clock time that takes. BUCKET_VOLUME is derived from the data itself
   -- average daily volume / TARGET_BUCKETS_PER_DAY -- rather than a fixed
   guessed constant, so it stays sensible if traded volume shifts over the
   life of a contract.
3. Per bucket: buyVolume/sellVolume from the side column ('unknown' ticks
   count toward neither side and are excluded from the imbalance, not
   split 50/50 -- they're a small minority per the tick-rule fallback logic
   in TickRecorder.cs, and guessing a side for them would bias the signal).
   order_imbalance = |buyVolume - sellVolume|.
4. VPIN = rolling mean of order_imbalance over VPIN_WINDOW_BUCKETS buckets,
   divided by BUCKET_VOLUME -- lands in [0, 1] by construction.
5. Event study: label each bucket's ending timestamp "extreme" (top
   EXTREME_PERCENTILE of VPIN readings) or "normal" (the rest), then compare
   forward return and forward realized volatility at each horizon in
   FORWARD_HORIZONS_MIN between the two groups (mean, std, Mann-Whitney U
   test on returns -- nonparametric, no normality assumption, appropriate
   for fat-tailed financial returns).

This is a validation script, not a signal generator: if extreme-VPIN buckets
don't show a statistically and economically meaningful difference in forward
returns/volatility, VPIN doesn't get promoted to a live feature -- that's
the explicit gate from the project's own plan, not a foregone conclusion.

Usage
-----
    python vpin_event_study.py                  # instrument "NQ 09-26"
    python vpin_event_study.py "NQ 09-26" 50 50  # instrument, buckets/day, VPIN window
"""

import os
import sys

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from scipy import stats

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

TABLE = "ticks_nq"

TARGET_BUCKETS_PER_DAY = 50   # standard VPIN convention (Easley/Lopez de Prado/O'Hara)
VPIN_WINDOW_BUCKETS = 50      # rolling window length, in buckets, for the VPIN average
EXTREME_PERCENTILE = 0.90     # top decile of VPIN readings = "extreme" bucket
FORWARD_HORIZONS_MIN = [5, 15, 30]


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ.get("POSTGRES_DB", "market_data"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ["POSTGRES_PASSWORD"],
    )


CHUNK_SIZE = 500_000  # rows per server-side-cursor fetch -- keeps peak memory
                       # bounded regardless of total tick count (28M+ ticks with
                       # a string "side" column blew past available RAM when
                       # loaded as one pandas DataFrame -- see conversation)

SIDE_CODE = {"buy": 1, "sell": -1, "unknown": 0}


def get_span_and_bucket_volume(conn, instrument, target_buckets_per_day):
    """Single cheap aggregate query instead of loading all ticks just to
    compute avg daily volume."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MIN(ts_utc), MAX(ts_utc), SUM(volume) FROM {TABLE} WHERE instrument = %s",
            (instrument,),
        )
        min_ts, max_ts, total_volume = cur.fetchone()
    span_days = (max_ts - min_ts).total_seconds() / 86400
    avg_daily_volume = float(total_volume) / max(span_days, 1)
    bucket_volume = max(1, round(avg_daily_volume / target_buckets_per_day))
    return min_ts, max_ts, bucket_volume


def stream_chunks(conn, instrument, chunk_size=CHUNK_SIZE):
    """Yields lists of (ts_utc, price, volume, side) row tuples, fetched via
    a named (server-side) cursor -- a plain psycopg2 cursor buffers the
    ENTIRE result set client-side regardless of how pandas chunks it
    afterward, which is exactly what exhausted memory before; a named
    cursor actually streams `itersize` rows at a time from Postgres."""
    cur = conn.cursor(name="vpin_tick_stream")
    cur.itersize = chunk_size
    cur.execute(
        f"SELECT ts_utc, price, volume, side FROM {TABLE} WHERE instrument = %s ORDER BY ts_utc",
        (instrument,),
    )
    try:
        while True:
            rows = cur.fetchmany(chunk_size)
            if not rows:
                break
            yield rows
    finally:
        cur.close()


def build_buckets_and_price_series(conn, instrument, bucket_volume, chunk_size=CHUNK_SIZE):
    """Streams ticks from Postgres in bounded-memory chunks, incrementally
    building (a) the volume buckets (small -- a few thousand rows) and
    (b) a compact (ts, price) array for later forward-return lookups
    (two float columns, no strings -- far lighter than the full tick table).
    Cumulative volume carries across chunk boundaries so bucket assignment
    is identical to doing it in one pass over the full ordered stream."""
    bucket_totals = {}    # bucket_id -> [total_volume, buy_volume, sell_volume]
    bucket_last = {}      # bucket_id -> (ts_utc, price) of its last tick seen so far
    price_ts_chunks = []
    price_val_chunks = []
    running_cum_volume = 0
    rows_seen = 0

    for rows in stream_chunks(conn, instrument, chunk_size):
        ts = np.array([r[0] for r in rows], dtype="datetime64[ns]")
        price = np.array([r[1] for r in rows], dtype="float64")
        volume = np.array([r[2] for r in rows], dtype="int64")
        side_code = np.array([SIDE_CODE.get(r[3], 0) for r in rows], dtype="int8")

        cum_volume = running_cum_volume + np.cumsum(volume)
        running_cum_volume = cum_volume[-1]
        bucket_id = (cum_volume - 1) // bucket_volume

        chunk_df = pd.DataFrame({
            "bucket_id": bucket_id,
            "volume": volume,
            "buy_volume": np.where(side_code == 1, volume, 0),
            "sell_volume": np.where(side_code == -1, volume, 0),
        })
        grouped = chunk_df.groupby("bucket_id").agg(
            total_volume=("volume", "sum"),
            buy_volume=("buy_volume", "sum"),
            sell_volume=("sell_volume", "sum"),
        )
        for bid, row in grouped.iterrows():
            acc = bucket_totals.setdefault(bid, [0, 0, 0])
            acc[0] += row["total_volume"]
            acc[1] += row["buy_volume"]
            acc[2] += row["sell_volume"]

        # last tick per bucket_id IN THIS CHUNK -- overwrites any earlier
        # chunk's entry for the same bucket_id, which is correct since
        # chunks arrive in ts_utc order (a later chunk's value is the truly
        # final one for that bucket)
        last_idx_per_bucket = pd.Series(np.arange(len(bucket_id))).groupby(bucket_id).last()
        for bid, idx in last_idx_per_bucket.items():
            bucket_last[bid] = (ts[idx], price[idx])

        price_ts_chunks.append(ts)
        price_val_chunks.append(price)
        rows_seen += len(rows)
        print(f"  ...{rows_seen:,} ticks streamed", flush=True)

    bucket_ids_sorted = sorted(bucket_totals.keys())
    buckets = pd.DataFrame({
        "total_volume": [bucket_totals[b][0] for b in bucket_ids_sorted],
        "buy_volume": [bucket_totals[b][1] for b in bucket_ids_sorted],
        "sell_volume": [bucket_totals[b][2] for b in bucket_ids_sorted],
        "ts_utc": [bucket_last[b][0] for b in bucket_ids_sorted],
        "price": [bucket_last[b][1] for b in bucket_ids_sorted],
    })
    buckets["order_imbalance"] = (buckets["buy_volume"] - buckets["sell_volume"]).abs()

    # drop the final partial bucket, same reasoning as before
    if len(buckets) > 1 and buckets["total_volume"].iloc[-1] < buckets["total_volume"].iloc[:-1].median() * 0.5:
        buckets = buckets.iloc[:-1]

    price_ts = np.concatenate(price_ts_chunks)
    price_val = np.concatenate(price_val_chunks)
    return buckets.reset_index(drop=True), price_ts, price_val


def compute_vpin(buckets, bucket_volume, window):
    buckets = buckets.copy()
    buckets["vpin"] = buckets["order_imbalance"].rolling(window).sum() / (window * bucket_volume)
    return buckets


def add_bvc_imbalance(buckets):
    """Bulk Volume Classification (Easley/Lopez de Prado/O'Hara) -- the
    method the actual VPIN literature uses, as opposed to the tick-rule
    'side' column TickRecorder.cs stores per tick. Instead of classifying
    each trade, each BUCKET's volume is split fractionally into buy/sell
    using the standard-normal CDF of that bucket's standardized price
    change: buy_frac = Phi(delta_price / sigma). A bucket where price rose
    a lot relative to typical bucket-to-bucket moves is treated as
    mostly-buy volume, one where price barely moved as ~50/50, purely from
    price action -- no bid/ask or tick-rule classification needed at all.
    sigma is the full-sample std of bucket-to-bucket price changes, per the
    original VPIN papers' convention."""
    buckets = buckets.copy()
    price_diff = buckets["price"].diff()
    sigma = price_diff.std()
    buy_frac = stats.norm.cdf(price_diff / sigma)
    buckets["buy_volume_bvc"] = buckets["total_volume"] * buy_frac
    buckets["sell_volume_bvc"] = buckets["total_volume"] * (1 - buy_frac)
    buckets["order_imbalance_bvc"] = (buckets["total_volume"] * (2 * buy_frac - 1)).abs()
    return buckets


def compute_vpin_bvc(buckets, bucket_volume, window):
    buckets = buckets.copy()
    buckets["vpin_bvc"] = buckets["order_imbalance_bvc"].rolling(window).sum() / (window * bucket_volume)
    return buckets


def forward_returns(buckets, price_series_ts, price_series_val, horizons_min):
    """For each bucket's end timestamp, looks up price at ts + horizon (nearest
    available tick at/after that time) and computes log return + realized
    volatility (std of 1-min log returns) over the horizon window. Uses
    searchsorted to slice the window directly (O(log n) + window size)
    instead of masking the full tick array per bucket (which is O(n) per
    bucket and was far too slow at 28M+ ticks x thousands of buckets).
    price_series_ts/price_series_val are plain numpy arrays (from the
    streaming builder), not pandas Series -- no full tick DataFrame is ever
    held in memory."""
    ts_arr = np.asarray(price_series_ts, dtype="datetime64[ns]")
    price_arr = np.asarray(price_series_val, dtype="float64")
    n = len(ts_arr)
    bucket_ts = buckets["ts_utc"].values.astype("datetime64[ns]")
    bucket_price = buckets["price"].values

    start_idx = np.searchsorted(ts_arr, bucket_ts)  # index of each bucket's own end time

    out = {}
    for h in horizons_min:
        delta = np.timedelta64(h, "m")
        target_ts = bucket_ts + delta
        end_idx = np.searchsorted(ts_arr, target_ts)

        ret = np.full(len(buckets), np.nan)
        vol = np.full(len(buckets), np.nan)
        for i in range(len(buckets)):
            j = end_idx[i]
            if j >= n:
                continue
            p1 = price_arr[j]
            p0 = bucket_price[i]
            if p0 > 0 and p1 > 0:
                ret[i] = np.log(p1 / p0)
            window_prices = price_arr[start_idx[i]:j + 1]
            if len(window_prices) > 2:
                vol[i] = np.std(np.diff(np.log(window_prices)))

        out[f"ret_{h}m"] = ret
        out[f"vol_{h}m"] = vol

    for k, v in out.items():
        buckets[k] = v
    return buckets


def event_study(buckets, horizons_min, extreme_pct, vpin_col="vpin", label="tick-rule VPIN"):
    valid = buckets.dropna(subset=[vpin_col])
    threshold = valid[vpin_col].quantile(extreme_pct)
    extreme = valid[valid[vpin_col] >= threshold]
    normal = valid[valid[vpin_col] < threshold]

    print(f"\n=== {label} ===")
    print(f"Threshold for top {(1 - extreme_pct) * 100:.0f}% ('extreme'): {threshold:.4f}")
    print(f"Extreme buckets: {len(extreme)}, Normal buckets: {len(normal)}\n")

    print(f"{'Horizon':>8} | {'Extreme ret mean':>17} | {'Normal ret mean':>16} | "
          f"{'Extreme vol mean':>17} | {'Normal vol mean':>16} | {'Mann-Whitney p':>15}")
    for h in horizons_min:
        ret_col, vol_col = f"ret_{h}m", f"vol_{h}m"
        e_ret = extreme[ret_col].dropna()
        n_ret = normal[ret_col].dropna()
        e_vol = extreme[vol_col].dropna()
        n_vol = normal[vol_col].dropna()

        p_value = np.nan
        if len(e_ret) > 5 and len(n_ret) > 5:
            _, p_value = stats.mannwhitneyu(e_ret.abs(), n_ret.abs(), alternative="greater")

        print(f"{h:>6}m  | {e_ret.mean():>17.6f} | {n_ret.mean():>16.6f} | "
              f"{e_vol.mean():>17.6f} | {n_vol.mean():>16.6f} | {p_value:>15.4f}")

    return extreme, normal


def main():
    args = sys.argv[1:]
    instrument = args[0] if len(args) > 0 else "NQ 09-26"
    target_buckets_per_day = int(args[1]) if len(args) > 1 else TARGET_BUCKETS_PER_DAY
    vpin_window = int(args[2]) if len(args) > 2 else VPIN_WINDOW_BUCKETS

    conn = get_conn()
    try:
        print(f"Computing bucket size for '{instrument}' from '{TABLE}'...")
        min_ts, max_ts, bucket_volume = get_span_and_bucket_volume(conn, instrument, target_buckets_per_day)
        print(f"  data spans {min_ts} -> {max_ts}")
        print(f"  Bucket volume: {bucket_volume} contracts "
              f"(avg daily volume / {target_buckets_per_day} target buckets/day)")

        print(f"\nStreaming ticks in {CHUNK_SIZE:,}-row chunks (bounded memory)...")
        buckets, price_ts, price_val = build_buckets_and_price_series(conn, instrument, bucket_volume)
        print(f"  {len(buckets):,} volume buckets built, {len(price_ts):,} ticks streamed total")

        buckets = compute_vpin(buckets, bucket_volume, vpin_window)
        print(f"  Tick-rule VPIN computed (rolling {vpin_window}-bucket window); "
              f"{buckets['vpin'].notna().sum()} buckets have a valid reading")

        buckets = add_bvc_imbalance(buckets)
        buckets = compute_vpin_bvc(buckets, bucket_volume, vpin_window)
        print(f"  BVC VPIN computed (rolling {vpin_window}-bucket window); "
              f"{buckets['vpin_bvc'].notna().sum()} buckets have a valid reading")

        buckets = forward_returns(buckets, price_ts, price_val, FORWARD_HORIZONS_MIN)

        event_study(buckets, FORWARD_HORIZONS_MIN, EXTREME_PERCENTILE, "vpin", "Tick-rule VPIN")
        event_study(buckets, FORWARD_HORIZONS_MIN, EXTREME_PERCENTILE, "vpin_bvc", "BVC VPIN (literature method)")

        out_path = os.path.join(HERE, f"vpin_buckets_{instrument.replace(' ', '_')}.csv")
        buckets.to_csv(out_path, index=False)
        print(f"\nFull bucket-level data (for charting/further analysis) written to {out_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
