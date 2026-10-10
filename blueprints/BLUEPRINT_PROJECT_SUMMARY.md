# Blueprint (BP) project — state summary for review

*Prepared 2026-10-10 for a code/design review. Please recommend changes and next steps; see "Review requests" at the end.*

## 1. Goal

Build a library of **blueprints (BPs)**: one normalised "skeleton picture" of every NQ futures session
(18:00 ET → 16:30 ET next day) with metadata (price action, Fib retracements, volume, delta, VWAP, SMA100, time of day).
The live use case: every ~5 minutes, match the session-so-far against the library, superimpose the closest BPs
semi-transparently on the real chart (web and, later, NinjaTrader 8), show a match %, and track whether the matched
BPs' "rest of day" predicted the actual move.

The user is a discretionary futures trader who works visually. Working agreement: **one step at a time, each step
verified visually before moving on**, no conclusions about edge from proxy rules, no look-ahead in anything that claims
to be a prediction.

## 2. Data

| Item | Detail |
|---|---|
| Source | Postgres `market_data.ticks_nq` (`instrument, ts_utc, price, volume, bid, ask, side`), recorded/imported from NinjaTrader exports |
| Contracts | NQ 12-25, 03-26, 06-26, 09-26, 12-26 — 2025-09-22 → 2026-10-02 |
| Session | 18:00 ET (previous evening) → 16:30 ET; per trade date the contract with the most trades is used |
| Built | **250 sessions** |
| Skipped | 11 holidays/half days; **8 roll-gap days** (2025-12-16..19, 2026-06-15..18) where the new front contract's ticks are missing from Postgres (see `library/build_log.txt`) |
| Delta | every trade classified against its own bid/ask: `price >= ask` → buy, `price <= bid` → sell, otherwise ignored |
| Env | Windows 11; Python via conda env `my_quant_lab` (pandas 2.2, numpy 1.26, psycopg2, playwright); Postgres password from `.env` / `POSTGRES_PASSWORD` |

## 3. Code (all in `blueprints/`)

| File | Role | Status |
|---|---|---|
| `bp_core.py` | `build_bp()` for ONE session: 30-pt zigzag legs + per-leg metrics, coarse skeleton, 5-min series, features | current |
| `bp_library.py` | builds all sessions from Postgres (resumable, `--force`), categorises, nearest neighbours, writes Postgres `bp_library`, `library_index.json`, `index.html`, one overlay page per day | current |
| `bp_query.py` | as-of matching CLI (`--like DATE --as-of HH:MM`, `--cat TAGS…`) | current |
| `bp_predict.py` | match %, walk-forward predictions → Postgres `bp_predictions`, per-BP track record; patches `library_index.json` / `index.html` | current |
| `make_match_page.py` + `match_viewer_template.html` | real day(s) with the K closest earlier BPs superimposed | current |
| `make_cd_preview.py` + `bp_viewer_template.html` | single-day page with cumulative-delta experiments (see §6) | current, **previews only** |
| `library_template.html` | library review page (filter/sort/detail/neighbours/track record) | current |
| `make_blueprint.py`, `make_blueprint_v2.py`, `BP_2026-10-02*.{png,json,html}` | first prototypes (v1 PNG, v2 overlay) built from a tick export file | legacy |

Outputs: `library/bp/BP_<date>.json` (full BP), `library/view/BP_<date>.html` (per-day overlay page),
`library/library_index.json`, `library/index.html`, `library/predictions.json`, `matches/*.html`, `preview/*.html`. ~79 MB.

Run order matters: `bp_library.py` rewrites `library_index.json` **without** the track record, so `bp_predict.py` must be
re-run after every library build.

## 4. BP definition

**Normalisation.** x = clock time (minutes since 18:00; 270 five-minute buckets). y = position in the session range
(0 = session low, 1 = session high); RTH (09:30–16:30) also normalised to its own range. Absolute prices are kept so a BP
can be laid on a real chart.

**Two leg layers.**
- `legs`: zigzag on every trade with a fixed **30-point reversal** → every swing ≥ 30 pts is a leg (Oct 2 = 114 legs).
  Per leg: pts, minutes, speed, Fib ratio vs previous leg + zone (23.6/38.2/50/61.8/78.6), delta ratio, volume and pace,
  % of trades above VWAP / SMA100 and end distance, mid price, mid-point crosses (+ per minute), deepest pullback,
  biggest 1-min range %, most 1-min travel %, efficiency.
- `skeleton`: bottom-up piecewise-linear segmentation of 5-min closes (max deviation 7% of range), legs ≥ 12% of range
  = UP/DOWN else SIDEWAYS, turning points snapped to real highs/lows (Oct 2 = 20 legs).

**5-min series (`shape`)**: close/high/low (normalised), VWAP, SMA100 (1-min), relative volume, delta ratio,
mid-point crosses, cumulative delta (normalised + `cum_delta_max_abs` in contracts).

**Categories** (multi-label, 19–25 tags per day; rules in the `bp_library.py` docstring): session and RTH direction
(DECISIVE_UP/UP/SIDEWAYS/MIXED/DOWN/DECISIVE_DOWN, families UP/DOWN/NEUTRAL), overnight→RTH story, per-phase letters
(Asia/London/NY pre/NY open/midday/close), RTH V-reversals, opening-range break, RTH open vs overnight range, phase of
high/low, library-relative thirds (volatility, volume, choppiness, retracement depth, swift vs dense legs, delta),
delta divergence, VWAP position/chop, weekday.

## 5. Matching and predictions

- **Whole-day neighbours** (library page, match page): same session family, RMSE of the 270-point normalised shape.
  Uses the full day (hindsight) — for reviewing fit only.
- **As-of matching** (`bp_query.match_asof`): cut both shapes at the last finished 5-min bar, rescale to range-so-far.
  Hard elimination: overnight direction (±0.25) and, once finished, NY open and NY midday direction (±0.15).
  Soft: +0.03 per differing Asia/London/NY-pre letter. Rank by RMSE. Only earlier days are candidates in walk-forward.
- **Match %** = `100 × (1 − rmse / median random-pair rmse)` (reference ≈ 0.35).
- **Prediction rule** (deliberately simple): matched BP's move from as-of to 16:30 in its range-so-far units × query's
  range-so-far; direction UP/DOWN if ≥ ±25% of range-so-far, else SIDEWAYS; HIT = same direction.
- **Walk-forward result** (1,879 predictions, 210 days, top 3 at 09:30 / 10:30 / 12:00):

| as of | #1 | #2 | #3 | most-common-class baseline |
|---|---|---|---|---|
| 09:30 | 28.1% | 35.7% | 38.6% | 42% |
| 10:30 | 30.6% | 38.0% | 30.2% | 42% |
| 12:00 | 35.7% | 34.8% | 39.1% | 53% |

Match % ≥ 70 → 38% hit (68 predictions). The current rule does not beat the baseline; this is a property of this first
rule/matcher, not a conclusion about the blueprint idea.

## 6. Web pages (all self-contained HTML + inline SVG, no libraries)

Common chart conventions (user requirement): crosshair with price/time labels, drag price axis / time axis to stretch,
double-click axis to reset, wheel zoom, background pan.

- **Overlay page** (`bp_viewer_template.html`, library day pages): BP legs over real candles; drag a joint; drag a line to
  move the whole BP; **double-click + drag a BP line to stretch/squeeze the whole day in time or price** (affine
  `t' = at·t + bt`, `p' = ap·p + bp` around the BP centre, axis locked after 6 px, clamp 0.2–5×); per-joint flexibility
  allowance box; opacity; reset joints / reset all; BP's own VWAP/SMA and panel lines follow the BP.
- **Match page**: two real days, K = 3 earlier BPs each, numbered badges toggle on/off, per-BP opacity, per-BP
  stretch/move/reset, match % and track record, the day's logged predictions.
- **Library page**: 250 cards, multi-select category filters (OR within group, AND across), sorts, detail chart, neighbours.

**Cumulative-delta experiment (current focus; preview pages only, `preview/`):**
- Delta panel with **one shared zero** (middle line): bars = buy − sell contracts per candle (left axis), white thick
  line = cumulative delta (cd) since 18:00 (right axis, contracts), dashed amber = the BP's cd. Symmetric axes, pure
  proportional layout (price 45% / volume 10% / delta 45%; mid-point panel dropped), crosshair readout on both axes.
- **cd SMA editor** in the side panel: any number of SMAs (default **10 yellow, 50 green**), period / colour / on-off,
  unit = candles (follows zoom: 5-min ≥ 4 h view, 1-min below) or minutes (fixed); saved in `localStorage` key
  `bp_cd_sma_v2`.
- **Triangles** (on delta panel and price panel): filled ▲▼ = cd crosses 0; thick hollow △▽ = 1st SMA crosses 2nd
  (e.g. 10 × 50); optional thin hollow = an SMA crosses 0 (off by default). cd-crosses-SMA triangles were removed as too
  noisy. All triangle times were verified against independent pandas calculations.
- Observed on a handful of days (anecdotal, hindsight): cd often hovers at 0 overnight (many zero-cross triangles);
  the last pre-open zero-cross set cd's side for the session on Sep 15 and Oct 2 while price moved against it
  (absorption); Sep 16 had an RTH zero-cross cluster at the top two hours before a ~500-pt drop.

## 7. Verification done

- Rebuilt Oct 2 reproduces the hand-verified prototype exactly (114 legs, range 522.25); mid-point crosses identical to v1.
- Extreme days (Jun 5 −1,547, Jul 30 +1,164) checked as real moves, not bad ticks.
- Every page change was driven in a real browser (Playwright + Edge, headless): drags, stretch factors, resets,
  toggles, opacity, axis drags, crosshair values, resize proportionality, console errors. **These QA scripts currently
  live in a temporary scratchpad, not in the repo.**

## 8. Known issues / technical debt

1. **Template drift**: the 250 `library/view/*.html` pages were generated before the cumulative-delta work; only
   `preview/*.html` have it. `match_viewer_template.html` has none of it either. Rollout is waiting for user approval.
2. **Three copies of the chart engine** (`bp_viewer_template`, `match_viewer_template`, `library_template` detail chart)
   with diverging features; recent edits were applied by string-replacement patch scripts.
3. **No automated tests in the repo**; browser QA scripts not committed; no git repository.
4. **Weak tags**: `RETRACE_*` (30-pt zigzag Fib ratios cluster ≈ 1.0) and `DELTA_*_HEAVY` (thirds at ±0.005) don't
   discriminate. "Most 1-min travel"/efficiency are inflated by bid/ask bounce.
5. Fixed thresholds (30 pts, 12%/7% skeleton, ±0.6/0.3 direction, 0.85× range for sideways vs mixed) don't scale with
   volatility (NQ ranged ~24k–31k; daily range 243–980 pts at the 5th–95th percentile).
6. SMA periods in "candles" change meaning with zoom; triangles are evaluated per candle and shift with candle size.
7. Matching is shape-only RMSE on rescaled closes; no time warping, no volume/delta/feature terms, no use of the stretch
   transform the UI already supports.
8. Library build needs ~25 min for a full rebuild; per-day pages ~200 KB each with embedded data.
9. NinjaTrader integration not built. Proposed design: a Python service matches every 5 min from live `ticks_nq`
   (TickRecorder writes every 0.5 s) → table of current matches → (a) custom NT DrawingTool modelled on the built-in
   Rectangle in `@Shapes.cs` (two anchors = native stretch, AreaOpacity) plus an indicator that places the top 3;
   (b) the same matches served to the web page.

## 9. Review requests

1. Architecture: how to unify the three chart implementations (single JS module inlined at build time? a small
   charting lib?) and how to roll the cd panel/editor out to the library and match pages safely.
2. Matching: better similarity for partial days (DTW / time-warp limited by the flexibility allowance, multi-feature
   distance incl. cd shape and volume profile, learned weights) and a fair evaluation protocol vs the baselines above.
3. Prediction target: is "direction of rest-of-day at ±25% of range-so-far" a sensible label? Alternatives (MFE/MAE in
   points, distance to next skeleton turning point, probability distribution from the K matches).
4. Categories: replace the weak tags; volatility-scaled thresholds; which tags should be hard elimination vs soft.
5. cd experiment: a precise, testable definition of an "exhaustion" signal from cd zero-crosses and SMA crossovers, and
   how to evaluate it across the 250 days without look-ahead; whether per-minute SMA units should be the default.
6. Data: filling the 8 roll-gap days; handling contract rolls (back-adjustment not done — each day uses its own contract).
7. Live path: Python service vs NinjaScript AddOn for the matcher; DrawingTool design for the BP overlay in NT8.
8. Engineering hygiene: put the project under git, commit the browser QA scripts, add unit tests for `bp_core`
   (zigzag, segmentation, crossing logic) and a regression test on the Oct 2 session.
