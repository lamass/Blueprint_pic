"""
Stochastic-extreme entry backtest with SMA/MACD/RSI/Momentum reference indicators.
Mirrors StochExtremesStrategy.cs's Stochastic-only path (with ReverseAllSignals) so NT8
findings can be reproduced/swept in Python over the full contract history.

Entry: fresh %K cross above --overbought -> short (or long if --reverse); fresh cross below
--oversold -> long (or short if --reverse).
Exit: fixed-point profit target / stop loss, whichever a later bar's high/low touches first.
One position at a time; a signal while already in a trade is ignored.

Usage: python stoch_extremes_backtest.py "NQ 09-26" [--candles nq0926_5min_candles.json]
           [--k-period 30] [--d-period 7] [--k-smooth 3] [--overbought 90] [--oversold 10]
           [--profit-target 60] [--stop-loss 30] [--reverse]

Writes nq<slug>_indicators.json (full bar series + indicators) and
stoch_trades_NQ<slug>.json (per-trade records) for the chart template.
"""
import argparse
import json
import re
import pandas as pd
import numpy as np

SMA_PERIOD = 20
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_PERIOD = 10
MOMENTUM_PERIOD = 10


def slug(instrument):
    return re.sub(r'[^A-Za-z0-9]', '', instrument)


def compute_indicators(df, k_period, k_smooth, d_period):
    df['sma'] = df['c'].rolling(SMA_PERIOD).mean()

    low_k = df['l'].rolling(k_period).min()
    high_k = df['h'].rolling(k_period).max()
    raw_k = 100 * (df['c'] - low_k) / (high_k - low_k)
    df['stoch_k'] = raw_k.rolling(k_smooth).mean()
    df['stoch_d'] = df['stoch_k'].rolling(d_period).mean()

    ema_fast = df['c'].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = df['c'].ewm(span=MACD_SLOW, adjust=False).mean()
    df['macd'] = ema_fast - ema_slow
    df['macd_signal'] = df['macd'].ewm(span=MACD_SIGNAL, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    delta = df['c'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_PERIOD, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['rsi'] = 100 - (100 / (1 + rs))

    df['momentum'] = df['c'].diff(MOMENTUM_PERIOD)
    return df


def entry_allowed_mask(t_ms, session_cutoff=None, session_resume=None, period_end_ts=None):
    """True for bars where a NEW entry may be taken. Bars are assumed already corrected so
    their epoch value, read at face value in UTC, equals the true ET clock time -- matching
    an NT8 session template that takes no new entries between session_cutoff and
    session_resume (both (hour, minute) ET tuples) every day, e.g. no new trades after
    16:15 ET, trading resumes at 18:00 ET for the next session. Existing open positions are
    unaffected -- only new entries are gated; this does not truncate the data or block exits.

    period_end_ts (epoch ms, optional): the requested backtest period's own last day has no
    "next session" to resume into, so once session_cutoff is reached on or after this
    timestamp's calendar day, entries stay blocked for good (no 18:00 resume that evening) --
    without this, the daily mask alone would incorrectly reopen entries at 18:00 on the final
    evening even though that's already past the period the caller asked for."""
    if session_cutoff is None or session_resume is None:
        return np.ones(len(t_ms), dtype=bool)
    minutes = ((t_ms // 60000) % 1440).astype(int)  # minutes since UTC midnight == ET midnight (data pre-shifted)
    cutoff_min = session_cutoff[0] * 60 + session_cutoff[1]
    resume_min = session_resume[0] * 60 + session_resume[1]
    blocked = (minutes >= cutoff_min) & (minutes < resume_min)
    if period_end_ts is not None:
        last_day_start = (period_end_ts // 86400000) * 86400000
        last_day_cutoff_ts = last_day_start + cutoff_min * 60000
        blocked = blocked | (t_ms >= last_day_cutoff_ts)
    return ~blocked


def backtest(df, overbought, oversold, profit_target, stop_loss, reverse,
             session_cutoff=None, session_resume=None, period_end_ts=None):
    prev_k = df['stoch_k'].shift(1)
    cross_up = (prev_k < overbought) & (df['stoch_k'] >= overbought)   # normally short
    cross_dn = (prev_k > oversold) & (df['stoch_k'] <= oversold)       # normally long

    if reverse:
        long_signal, short_signal = cross_up, cross_dn
    else:
        short_signal, long_signal = cross_up, cross_dn

    trades = []
    in_position = False
    n = len(df)
    o, h, l, c, t = df['o'].values, df['h'].values, df['l'].values, df['c'].values, df['t'].values
    stoch_k = df['stoch_k'].values
    can_enter = entry_allowed_mask(t, session_cutoff, session_resume, period_end_ts)

    i = 0
    while i < n:
        if not in_position and can_enter[i] and (short_signal.iloc[i] or long_signal.iloc[i]):
            direction = 'short' if short_signal.iloc[i] else 'long'
            entry_idx, entry_t, entry_p = i, t[i], c[i]
            tp = entry_p - profit_target if direction == 'short' else entry_p + profit_target
            sl = entry_p + stop_loss if direction == 'short' else entry_p - stop_loss

            exit_idx, exit_p, reason = None, None, None
            for j in range(i + 1, n):
                hit_tp = (h[j] >= tp) if direction == 'long' else (l[j] <= tp)
                hit_sl = (l[j] <= sl) if direction == 'long' else (h[j] >= sl)
                if hit_tp and hit_sl:
                    exit_idx, exit_p, reason = j, sl, 'SL(same-bar-both)'
                    break
                elif hit_sl:
                    exit_idx, exit_p, reason = j, sl, 'SL'
                    break
                elif hit_tp:
                    exit_idx, exit_p, reason = j, tp, 'TP'
                    break
            if exit_idx is None:
                exit_idx, exit_p, reason = n - 1, c[n - 1], 'EOD'

            pnl = (entry_p - exit_p) if direction == 'short' else (exit_p - entry_p)
            trades.append({
                'direction': direction,
                'entryIdx': int(entry_idx), 'entryT': int(entry_t), 'entryP': float(entry_p),
                'exitIdx': int(exit_idx), 'exitT': int(t[exit_idx]), 'exitP': float(exit_p),
                'pnl': float(pnl), 'reason': reason, 'stochK': float(stoch_k[entry_idx]),
            })
            i = exit_idx + 1
        else:
            i += 1
    return trades


def run(instrument, candles_path=None, k_period=14, k_smooth=3, d_period=3,
        overbought=80.0, oversold=20.0, profit_target=90.0, stop_loss=90.0, reverse=False,
        session_cutoff=None, session_resume=None, period_end_ts=None):
    s = slug(instrument)
    if candles_path is None:
        candles_path = f'nq{s[-4:]}_5min_candles.json'

    with open(candles_path, 'r', encoding='utf-8') as f:
        candles = json.load(f)
    df = pd.DataFrame(candles)
    df = compute_indicators(df, k_period, k_smooth, d_period)

    trades = backtest(df, overbought, oversold, profit_target, stop_loss, reverse,
                       session_cutoff, session_resume, period_end_ts)

    wins = sum(1 for tr in trades if tr['pnl'] > 0)
    total = sum(tr['pnl'] for tr in trades)
    tp_hits = sum(1 for tr in trades if tr['reason'] == 'TP')
    sl_hits = sum(1 for tr in trades if tr['reason'].startswith('SL'))
    n_long = sum(1 for tr in trades if tr['direction'] == 'long')
    n_short = sum(1 for tr in trades if tr['direction'] == 'short')
    win_rate = 100 * wins / len(trades) if trades else 0.0
    avg = total / len(trades) if trades else 0.0
    print(f"{instrument}: {len(trades)} trades ({n_long} long / {n_short} short), "
          f"win_rate={win_rate:.1f}%, total={total:.1f} pts, "
          f"avg={avg:.1f} pts/trade, TP={tp_hits}, SL={sl_hits}, "
          f"EOD={len(trades)-tp_hits-sl_hits}")

    out_cols = ['t', 'o', 'h', 'l', 'c', 'v', 'sma', 'stoch_k', 'stoch_d',
                'macd', 'macd_signal', 'macd_hist', 'rsi', 'momentum']
    df[out_cols].to_json(f'nq{s[-4:]}_indicators.json', orient='records')
    with open(f'stoch_trades_NQ{s[-4:]}.json', 'w', encoding='utf-8') as f:
        json.dump(trades, f)

    return {
        'instrument': instrument, 'trades': len(trades), 'n_long': n_long, 'n_short': n_short,
        'win_rate': win_rate, 'total': total, 'avg': avg, 'tp_hits': tp_hits, 'sl_hits': sl_hits,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('instrument', nargs='?', default='NQ 09-26')
    p.add_argument('--candles', default=None)
    p.add_argument('--k-period', type=int, default=14)
    p.add_argument('--k-smooth', type=int, default=3)
    p.add_argument('--d-period', type=int, default=3)
    p.add_argument('--overbought', type=float, default=80.0)
    p.add_argument('--oversold', type=float, default=20.0)
    p.add_argument('--profit-target', type=float, default=90.0)
    p.add_argument('--stop-loss', type=float, default=90.0)
    p.add_argument('--reverse', action='store_true')
    args = p.parse_args()

    run(args.instrument, args.candles, args.k_period, args.k_smooth, args.d_period,
        args.overbought, args.oversold, args.profit_target, args.stop_loss, args.reverse)


if __name__ == '__main__':
    main()
