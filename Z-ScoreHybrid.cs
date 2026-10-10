#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
#endregion

// ================================================================
//  ZScoreHybrid  –  NinjaTrader 8.1.3.1
//
//  HYBRID FORMULA WITH VOLUME
//  ──────────────────────────
//  Step 1: HLC3        = (High + Low + Close) / 3
//  Step 2: Mean        = EMA(HLC3, Period)
//  Step 3: Spread      = ATR(Period)
//  Step 4: Raw         = (HLC3 - Mean) / Spread
//  Step 5: VolumeRatio = Volume / SMA(Volume, Period)
//             > 1.0 means above-average volume
//             < 1.0 means below-average volume
//  Step 6: Scaled      = Raw * VolumeRatio
//             High volume amplifies the signal
//             Low volume dampens it
//  Step 7: Signal      = EMA(Scaled, SignalSmooth)
//             Smoothed to reduce whipsaws
//  Step 8: Trend filter via EMA(Close, TrendPeriod)
//
//  SIGNAL TRIANGLES
//  ─────────────────
//  BUY  : Signal crosses above OsStrong (-2) from below
//         AND Close > Trend EMA (uptrend filter)
//         AND VolumeRatio >= VolumeThreshold (volume gate)
//
//  SELL : Signal crosses below ObStrong (+2) from above
//         AND Close < Trend EMA (downtrend filter)
//         AND VolumeRatio >= VolumeThreshold (volume gate)
//
//  The plotted line itself is volume-scaled so you can visually
//  see conviction — a spike on high volume reaches further from
//  zero than the same price move on thin volume.
// ================================================================

namespace NinjaTrader.NinjaScript.Indicators
{
    public class ZScoreHybrid : Indicator
    {
        // ── Backing fields ────────────────────────────────────────────────────
        private int    _period          = 20;
        private int    _signalSmooth    = 3;
        private int    _trendPeriod     = 200;
        private bool   _useTrendFilter  = true;
        private double _obStrong        =  2.0;
        private double _obMild          =  1.0;
        private double _osStrong        = -2.0;
        private double _osMild          = -1.0;
        private double _volumeThreshold = 1.5;   // volume must be 1.5x average for triangle
        private bool   _useVolumeGate   = true;

        // ── Internal series ───────────────────────────────────────────────────
        private Series<double> hlc3Series;    // (H+L+C)/3
        private Series<double> rawSeries;     // volume-scaled raw score
        private Series<double> volumeSeries;  // Volume as double for SMA

        // ── Indicator references ──────────────────────────────────────────────
        private EMA    emaPrice;     // EMA of HLC3
        private ATR    atrInd;       // ATR for spread
        private EMA    emaSignal;    // smoothing EMA on scaled raw
        private EMA    emaTrend;     // long trend filter
        private SMA    smaVolume;    // average volume

        private const int PLOT_SIGNAL = 0;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description  = "Hybrid Z-Score with volume scaling and volume gate. Signal is amplified by above-average volume and triangles require minimum volume confirmation.";
                Name         = "ZScoreHybrid";
                Calculate    = Calculate.OnBarClose;
                IsOverlay    = false;
                IsAutoScale  = true;
                IsSuspendedWhileInactive = true;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;

                AddPlot(new Stroke(Brushes.DodgerBlue, 2), PlotStyle.Line, "Hybrid Signal");

                AddLine(new Stroke(Brushes.Red,       DashStyleHelper.Dash,  1f),  2.0, "Strong OB");
                AddLine(new Stroke(Brushes.Orange,    DashStyleHelper.Dash,  1f),  1.0, "Mild OB");
                AddLine(new Stroke(Brushes.Gray,      DashStyleHelper.Solid, 1f),  0.0, "Zero");
                AddLine(new Stroke(Brushes.LimeGreen, DashStyleHelper.Dash,  1f), -1.0, "Mild OS");
                AddLine(new Stroke(Brushes.Lime,      DashStyleHelper.Dash,  1f), -2.0, "Strong OS");

                Period          = 20;
                SignalSmooth    = 3;
                TrendPeriod     = 200;
                UseTrendFilter  = true;
                ObStrong        =  2.0;
                ObMild          =  1.0;
                OsStrong        = -2.0;
                OsMild          = -1.0;
                VolumeThreshold = 1.5;
                UseVolumeGate   = true;
            }
            else if (State == State.DataLoaded)
            {
                hlc3Series   = new Series<double>(this);
                rawSeries    = new Series<double>(this);
                volumeSeries = new Series<double>(this);

                emaPrice  = EMA(hlc3Series,   _period);
                emaSignal = EMA(rawSeries,     _signalSmooth);
                smaVolume = SMA(volumeSeries,  _period);

                atrInd   = ATR(_period);
                emaTrend = EMA(Close, _trendPeriod);
            }
        }

        protected override void OnBarUpdate()
        {
            int minBars = Math.Max(_trendPeriod, Math.Max(_period, _signalSmooth)) + 2;
            if (CurrentBar < minBars)
                return;

            // ── Step 1: HLC3 ──────────────────────────────────────────────────
            double hlc3 = (High[0] + Low[0] + Close[0]) / 3.0;
            hlc3Series[0] = hlc3;

            // ── Step 2 & 3: Mean and Spread ───────────────────────────────────
            double mean   = emaPrice[0];
            double spread = atrInd[0];

            // ── Step 4: Raw score ─────────────────────────────────────────────
            double raw = 0;
            if (spread != 0)
                raw = (hlc3 - mean) / spread;

            // ── Step 5: Volume ratio ──────────────────────────────────────────
            // Feed current volume into our series so SMA can read it
            volumeSeries[0]    = Volume[0];
            double avgVolume   = smaVolume[0];
            double volumeRatio = 1.0;   // default neutral if avgVolume is zero

            if (avgVolume > 0)
                volumeRatio = Volume[0] / avgVolume;

            // Cap the volume ratio at 3x to prevent extreme distortion
            // on news spikes — keeps the signal readable
            volumeRatio = Math.Min(volumeRatio, 3.0);

            // ── Step 6: Volume-scaled raw score ───────────────────────────────
            // High volume → signal reaches further from zero (more conviction)
            // Low volume  → signal stays closer to zero (less conviction)
            rawSeries[0] = raw * volumeRatio;

            // ── Step 7: Smoothed signal ───────────────────────────────────────
            double signal     = emaSignal[0];
            double prevSignal = emaSignal[1];

            Values[PLOT_SIGNAL][0] = signal;

            // Dynamic line color
            if      (signal >=  _obStrong) PlotBrushes[PLOT_SIGNAL][0] = Brushes.Red;
            else if (signal >=  _obMild  ) PlotBrushes[PLOT_SIGNAL][0] = Brushes.Orange;
            else if (signal <= _osStrong ) PlotBrushes[PLOT_SIGNAL][0] = Brushes.Lime;
            else if (signal <= _osMild   ) PlotBrushes[PLOT_SIGNAL][0] = Brushes.LimeGreen;
            else                           PlotBrushes[PLOT_SIGNAL][0] = Brushes.DodgerBlue;

            // ── Step 8: Trend filter ──────────────────────────────────────────
            double trendEma    = emaTrend[0];
            bool   inUptrend   = Close[0] > trendEma;
            bool   inDowntrend = Close[0] < trendEma;

            // ── Volume gate: is current volume above the threshold? ────────────
            bool volumeConfirmed = !_useVolumeGate || (volumeRatio >= _volumeThreshold);

            // ── BUY: signal crosses above OsStrong from below ─────────────────
            bool buyCondition  = (prevSignal <= _osStrong) && (signal > _osStrong)
                              && (!_useTrendFilter || inUptrend)
                              && volumeConfirmed;

            // ── SELL: signal crosses below ObStrong from above ────────────────
            bool sellCondition = (prevSignal >= _obStrong) && (signal < _obStrong)
                              && (!_useTrendFilter || inDowntrend)
                              && volumeConfirmed;

            // ── Draw triangles on INDICATOR panel ────────────────────────────
            // Y values are in indicator scale (around the +-2 level)
            if (buyCondition)
            {
                Draw.TriangleUp(this,
                    "BuyPanel_" + CurrentBar, false,
                    0,
                    _osStrong - 0.3,
                    Brushes.Lime);
            }

            if (sellCondition)
            {
                Draw.TriangleDown(this,
                    "SellPanel_" + CurrentBar, false,
                    0,
                    _obStrong + 0.3,
                    Brushes.Red);
            }

            // ── Draw triangles on PRICE CHART ─────────────────────────────────
            // isGlobal = true places the drawing on the main price panel
            if (buyCondition)
            {
                Draw.TriangleUp(this,
                    "BuyChart_" + CurrentBar, false,
                    0,
                    Low[0] - 2 * TickSize,
                    Brushes.Lime,
                    true);
            }

            if (sellCondition)
            {
                Draw.TriangleDown(this,
                    "SellChart_" + CurrentBar, false,
                    0,
                    High[0] + 2 * TickSize,
                    Brushes.Red,
                    true);
            }
        }

        // ════════════════════════════════════════════════════════════════════
        //  Properties
        // ════════════════════════════════════════════════════════════════════

        [NinjaScriptProperty]
        [Range(2, int.MaxValue)]
        [Display(Name = "Period", GroupName = "1. Formula Settings", Order = 1,
                 Description = "Lookback for EMA(HLC3), ATR and SMA(Volume).")]
        public int Period
        { get { return _period; } set { _period = Math.Max(2, value); } }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Signal Smooth (bars)", GroupName = "1. Formula Settings", Order = 2,
                 Description = "EMA smoothing on the volume-scaled raw score.")]
        public int SignalSmooth
        { get { return _signalSmooth; } set { _signalSmooth = Math.Max(1, value); } }

        [NinjaScriptProperty]
        [Range(10, int.MaxValue)]
        [Display(Name = "Trend EMA Period", GroupName = "1. Formula Settings", Order = 3,
                 Description = "Long EMA trend filter. Buy only above, Sell only below.")]
        public int TrendPeriod
        { get { return _trendPeriod; } set { _trendPeriod = Math.Max(10, value); } }

        [NinjaScriptProperty]
        [Display(Name = "Use Trend Filter", GroupName = "1. Formula Settings", Order = 4,
                 Description = "Filter signals by trend direction.")]
        public bool UseTrendFilter
        { get { return _useTrendFilter; } set { _useTrendFilter = value; } }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "Strong OB Level", GroupName = "2. Reference Lines", Order = 1,
                 Description = "Sell triangle fires when signal crosses below this from above.")]
        public double ObStrong
        { get { return _obStrong; } set { _obStrong = value; } }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "Mild OB Level", GroupName = "2. Reference Lines", Order = 2)]
        public double ObMild
        { get { return _obMild; } set { _obMild = value; } }

        [NinjaScriptProperty]
        [Range(-10.0, -0.1)]
        [Display(Name = "Strong OS Level", GroupName = "2. Reference Lines", Order = 3,
                 Description = "Buy triangle fires when signal crosses above this from below.")]
        public double OsStrong
        { get { return _osStrong; } set { _osStrong = value; } }

        [NinjaScriptProperty]
        [Range(-10.0, -0.1)]
        [Display(Name = "Mild OS Level", GroupName = "2. Reference Lines", Order = 4)]
        public double OsMild
        { get { return _osMild; } set { _osMild = value; } }

        [NinjaScriptProperty]
        [Range(1.0, 5.0)]
        [Display(Name = "Volume Gate Threshold", GroupName = "3. Volume Settings", Order = 1,
                 Description = "Triangles only appear when Volume >= this multiple of average volume. e.g. 1.5 = 50% above average.")]
        public double VolumeThreshold
        { get { return _volumeThreshold; } set { _volumeThreshold = value; } }

        [NinjaScriptProperty]
        [Display(Name = "Use Volume Gate", GroupName = "3. Volume Settings", Order = 2,
                 Description = "When enabled, triangles only appear on above-average volume bars.")]
        public bool UseVolumeGate
        { get { return _useVolumeGate; } set { _useVolumeGate = value; } }
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private ZScoreHybrid[] cacheZScoreHybrid;
		public ZScoreHybrid ZScoreHybrid(int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double volumeThreshold, bool useVolumeGate)
		{
			return ZScoreHybrid(Input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, volumeThreshold, useVolumeGate);
		}

		public ZScoreHybrid ZScoreHybrid(ISeries<double> input, int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double volumeThreshold, bool useVolumeGate)
		{
			if (cacheZScoreHybrid != null)
				for (int idx = 0; idx < cacheZScoreHybrid.Length; idx++)
					if (cacheZScoreHybrid[idx] != null && cacheZScoreHybrid[idx].Period == period && cacheZScoreHybrid[idx].SignalSmooth == signalSmooth && cacheZScoreHybrid[idx].TrendPeriod == trendPeriod && cacheZScoreHybrid[idx].UseTrendFilter == useTrendFilter && cacheZScoreHybrid[idx].ObStrong == obStrong && cacheZScoreHybrid[idx].ObMild == obMild && cacheZScoreHybrid[idx].OsStrong == osStrong && cacheZScoreHybrid[idx].OsMild == osMild && cacheZScoreHybrid[idx].VolumeThreshold == volumeThreshold && cacheZScoreHybrid[idx].UseVolumeGate == useVolumeGate && cacheZScoreHybrid[idx].EqualsInput(input))
						return cacheZScoreHybrid[idx];
			return CacheIndicator<ZScoreHybrid>(new ZScoreHybrid(){ Period = period, SignalSmooth = signalSmooth, TrendPeriod = trendPeriod, UseTrendFilter = useTrendFilter, ObStrong = obStrong, ObMild = obMild, OsStrong = osStrong, OsMild = osMild, VolumeThreshold = volumeThreshold, UseVolumeGate = useVolumeGate }, input, ref cacheZScoreHybrid);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.ZScoreHybrid ZScoreHybrid(int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double volumeThreshold, bool useVolumeGate)
		{
			return indicator.ZScoreHybrid(Input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, volumeThreshold, useVolumeGate);
		}

		public Indicators.ZScoreHybrid ZScoreHybrid(ISeries<double> input , int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double volumeThreshold, bool useVolumeGate)
		{
			return indicator.ZScoreHybrid(input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, volumeThreshold, useVolumeGate);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.ZScoreHybrid ZScoreHybrid(int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double volumeThreshold, bool useVolumeGate)
		{
			return indicator.ZScoreHybrid(Input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, volumeThreshold, useVolumeGate);
		}

		public Indicators.ZScoreHybrid ZScoreHybrid(ISeries<double> input , int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double volumeThreshold, bool useVolumeGate)
		{
			return indicator.ZScoreHybrid(input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, volumeThreshold, useVolumeGate);
		}
	}
}

#endregion
