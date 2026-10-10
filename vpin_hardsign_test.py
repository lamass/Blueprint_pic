"""
Ablation test on the BVC VPIN signal validated in vpin_event_study.py: does BVC's
probabilistic classification (buy_frac = NormalCDF(priceDiff/sigma)) actually matter,
or would a trivial hard-sign classification (100% buy if price rose, 100% sell if it
fell, 50/50 on an exact tie) produce the same extreme-VPIN -> bigger-forward-move
effect? Everything else (bucket construction, rolling VPIN window, event-study
methodology) is held identical -- this isolates the classification step alone.

Motivated by github.com/Aryan-core/flow-toxicity-vpin, which found BVC classification
correlates poorly (even negatively) with ground-truth exchange-reported aggressor side
on Binance BTCUSDT data, and that BVC-VPIN's apparent predictive power collapsed to
chance once conditioned on realized volatility. Our own BVC-VPIN result (p<0.0001,
3 independent NQ periods) is real and already recorded -- this test asks whether that
effect is actually about BVC's sophistication, or just about rolling directional
persistence, which a dead-simple sign rule would also capture.

Runs entirely against the cached vpin_buckets_<INSTRUMENT>.csv files from the original
event study (already have price, total_volume, and forward returns/vol computed) --
no Postgres access, no re-streaming ticks.

Usage: python vpin_hardsign_test.py
"""
import os
import numpy as np
import pandas as pd

from vpin_event_study import event_study, FORWARD_HORIZONS_MIN, EXTREME_PERCENTILE

HERE = os.path.dirname(os.path.abspath(__file__))

PERIODS = [
    ("NQ 09-26", "vpin_buckets_NQ_09-26.csv"),
    ("NQ 06-26", "vpin_buckets_NQ_06-26.csv"),
    ("NQ 03-26", "vpin_buckets_NQ_03-26.csv"),
]

VPIN_WINDOW_BUCKETS = 50  # must match the window used when the cached CSVs were built


def add_hardsign_imbalance(buckets):
    """Same VPIN formula skeleton as BVC, but the classification step is a trivial
    hard sign rule instead of NormalCDF(priceDiff/sigma) -- isolates whether BVC's
    probabilistic weighting is actually necessary for the effect."""
    buckets = buckets.copy()
    price_diff = buckets["price"].diff()
    buy_frac = np.where(price_diff > 0, 1.0, np.where(price_diff < 0, 0.0, 0.5))
    buckets["order_imbalance_hardsign"] = (buckets["total_volume"] * (2 * buy_frac - 1)).__abs__()
    return buckets


def compute_vpin_hardsign(buckets, bucket_volume, window):
    buckets = buckets.copy()
    buckets["vpin_hardsign"] = buckets["order_imbalance_hardsign"].rolling(window).sum() / (window * bucket_volume)
    return buckets


def main():
    for instrument, csv_name in PERIODS:
        csv_path = os.path.join(HERE, csv_name)
        buckets = pd.read_csv(csv_path)

        bucket_volume = buckets["total_volume"].median()

        buckets = add_hardsign_imbalance(buckets)
        buckets = compute_vpin_hardsign(buckets, bucket_volume, VPIN_WINDOW_BUCKETS)

        print(f"\n{'='*70}\n{instrument} -- hard-sign classification, {VPIN_WINDOW_BUCKETS}-bucket window\n{'='*70}")
        event_study(buckets, FORWARD_HORIZONS_MIN, EXTREME_PERCENTILE, "vpin_hardsign", "Hard-sign VPIN (BVC ablation)")

        # side-by-side reminder of the already-recorded BVC result for direct comparison
        if "vpin_bvc" in buckets.columns:
            event_study(buckets, FORWARD_HORIZONS_MIN, EXTREME_PERCENTILE, "vpin_bvc", "BVC VPIN (original, for comparison)")


if __name__ == "__main__":
    main()
