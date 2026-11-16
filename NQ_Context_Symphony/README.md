# NQ Context Symphony v0.1

A local Python + JavaScript research prototype. The bundled 390-bar data is synthetic, not historical NQ. All plots are deterministic calculations, not AI illustrations. The output is an orbit-based glyph visualization; it is not a conventional pixel-by-pixel fractal boundary rendering.

## Run (Windows, macOS, Linux)
Python 3.10+; use a terminal in this folder:

    python -m pip install -r requirements.txt
    python app.py

The browser opens at http://127.0.0.1:8765. Stop with Ctrl+C. If your system uses `python3`, substitute that. No external APIs or JS dependencies. The server only listens on localhost.

## Use
- Select formulas and independently weight drivers from 0 (excluded from mixed fractal input) to 3. Recalculate. The palette and view selectors redraw without running Python again.
- Click a candle or glyph to inspect a specific bar; use the bar slider to move through time. The 10/20/30 arcs connect the current bar to the lagged bar; arc lift uses the preceding-window distance and signed displacement. These visual overlays remain available even when a context driver is disabled for orbit generation.
- Market palette uses a weighted directional proxy from enabled price, previous, state and context drivers. Volume independently controls glyph size and opacity (also remains a display channel if its formula-driver weight is zero). No claim about actual aggressive buy/sell volume.
- Export feature CSV or save the main canvas as PNG. Neighbor search compares earlier bars only, with a 30-bar exclusion gap, using raw normalized features plus the currently displayed formula descriptors. Driver weights affect fractal inputs, not the direct raw-feature part of the neighbor metric.

## Data
OHLCV CSV: open,high,low,close,volume; optional timestamp (ISO format, one timezone). Without timestamps rows are assumed chronological and labeled starting 09:30. Tick CSV: timestamp,price,volume (executed trade size, not cumulative volume). Tick data is aggregated into one-minute OHLCV, empty minutes dropped. Use a single session, max 1500 bars, max 15 MB per upload. Windows refer to preceding observed bars, not necessarily consecutive clock minutes. No bid/ask delta or order-book information is invented. Real tick feeds can be integrated later.

## Extend the Python engine
In engine.py, add a formula and restart the server:

    @formula('cubic', 'zₙ₊₁ = zₙ³ + cₜ')
    def cubic(z, c, n, s):
        return z**3 + c

Arguments: z = complex array [bars, seeds]; c = market parameter [bars,1]; n = iteration index; s = signed state [bars,1]. Return a broadcastable array of the same shape as z. Existing Mandelbrot/Burning Ship use small initial seed rings (orbit sampling rather than classical z0=0); Julia and Newton use market-shifted seeds; Sierpinski uses a deterministic vertex sequence influenced by state and mixed input. Iteration stops at a bounded escape threshold and display coordinates are compressed by z/(1+|z|). New formulas use market-shifted seeds by default. Add a new driver with:

    @driver('rejection', '10-bar rejection')
    def rejection(f):
        return val(f,'reject_low10') + 1j*val(f,'reject_high10')

Return one complex number per bar. Drivers are averaged using user-selected weights, with bounded transforms recommended. The UI reads the registries automatically. Raw features are generated in features(), using only current/past bars; missing warmup values stay blank in exports and become zero for orbit inputs. Price chart viewport uses full uploaded range for display only, not model features.

## Modeling next
Export rows for multi-day storage, add a session ID and timestamp, and define future return/maximum adverse excursion labels separately. Train tree models on numeric features first, then test a neural model on orbit sequences or glyph tensors. Use chronological session splits, fit scalers on training data only, purge overlapping forecast horizons, and compare to OHLCV-only baselines. This prototype does not train an agent or establish predictive value. Accumulation/distribution/resistance are hypotheses, not labels proven from price/volume.

## Runtime
Default 390 bars × 5 formulas × 12 seeds × 18 iterations = 421,200 updates. NumPy vectorizes over bars and seeds. Browser draws Canvas paths; no per-pixel fractal grids. The Python engine + JSON encoding was tested on the bundled session in under a second in the development environment; local browser timings depend on hardware. The page displays measured calculation and total response/draw times. Target 20–30 seconds is a budget, not a guaranteed SLA.