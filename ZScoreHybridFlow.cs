#region Using declarations
using System;
using System.Collections.Generic;
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
//  ZScoreHybridFlow  --  ZScoreHybrid.cs + order flow + time-of-day
//
//  Same base formula as ZScoreHybrid.cs (HLC3 z-score vs EMA/ATR),
//  with two additions, each toggleable so you can A/B against the
//  original:
//
//  1. ORDER FLOW (replaces the direction-blind volume multiplier)
//     Classifies each bar's volume into buy/sell using the same BVC
//     (Bulk Volume Classification) math already validated in
//     VolumeBucketVpin.cs: buyFrac = NormalCdf(priceDiff / sigma).
//     ImbalanceRatio = 2*buyFrac - 1, range -1..+1.
//     ParticipationMultiplier amplifies Raw when order flow AGREES
//     with price direction (real conviction) and dampens it when
//     order flow DISAGREES (price moved but flow didn't confirm --
//     the exact shape of the Price/Z-Score divergences you've been
//     hand-annotating).
//
//  2. TIME OF DAY, via BAR DURATION not volume (session anchored at
//     18:00 NY ET -- CME Globex open for index futures)
//     IMPORTANT: this runs on 1000-Volume bars, where every bar has
//     ~the same volume by construction (that's what a volume bar
//     IS) -- volume itself carries almost no information here
//     (checked on real data: std of Volume/SMA(Volume) was ~0.007,
//     i.e. a dead multiplier stuck at ~1.0 always). Confirmed the
//     first version of this file made exactly that mistake -- it
//     bucketed VOLUME by time of day, which is a no-op on volume
//     bars and explains why it looked identical to the original.
//     The thing that actually varies with time on volume bars is
//     BAR DURATION: how long it takes to trade 1000 contracts.
//     Checked on real data: ~700-900 sec/bar during 18:00-23:00 ET,
//     ~45-110 sec/bar during NY cash hours -- a huge, real signal.
//     So: bucket the clock into TimeBucketMinutes-wide slots from
//     18:00 ET, keep a slowly-adapting EMA baseline of bar duration
//     PER BUCKET, and use (baseline duration / this bar's duration)
//     as an "intensity" ratio -- a bar that fills much FASTER than
//     normal for this time of day is unusually intense and
//     amplifies the signal; a bar that fills much SLOWER dampens it.
//     ASSUMPTION: Time[0] is already NY Eastern (NT8's default for
//     CME instruments) -- this project hit a real +8h timestamp bug
//     once already, so verify your chart's instrument timezone
//     rather than assume it.
//
//  Both additions multiply into the same "Scaled" step the original
//  formula already had -- nothing about the core Raw z-score changed.
// ================================================================

namespace NinjaTrader.NinjaScript.Indicators
{
    public class ZScoreHybridFlow : Indicator
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
        private double _intensityThreshold = 1.3;
        private bool   _useIntensityGate   = true;

        private bool   _useOrderFlow     = true;
        private double _orderFlowWeight  = 0.6;
        private int    _orderFlowWindow  = 30;

        private bool   _useTimeOfDay        = true;
        private int    _sessionStartHour    = 18;   // 18:00 NY ET Globex open
        private int    _timeBucketMinutes   = 15;
        private double _timeBaselineAlpha   = 0.05; // slow adaptation across many days
        private double _maxBarDurationSeconds = 900; // clip session-gap outliers before they corrupt a bucket's baseline

        // ── Internal series ───────────────────────────────────────────────────
        private Series<double> hlc3Series;
        private Series<double> rawSeries;

        // ── Indicator references ──────────────────────────────────────────────
        private EMA emaPrice;
        private ATR atrInd;
        private EMA emaSignal;
        private EMA emaTrend;

        // ── Order flow state (BVC, same approach as VolumeBucketVpin.cs) ──────
        private List<double> recentPriceDiffs;

        // ── Time-of-day baseline state (bar DURATION, not volume -- see header) ─
        private int      numBuckets;
        private double[] bucketDurationMean;
        private bool[]   bucketInitialized;

        private const int PLOT_SIGNAL = 0;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description  = "ZScoreHybrid + order-flow (BVC) participation + time-of-day bar-duration baseline (session anchored 18:00 ET). Built for Volume-bar charts.";
                Name         = "ZScoreHybridFlow";
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
                IntensityThreshold = 1.3;
                UseIntensityGate   = true;

                UseOrderFlow     = true;
                OrderFlowWeight  = 0.6;
                OrderFlowWindow  = 30;

                UseTimeOfDay          = true;
                SessionStartHour      = 18;
                TimeBucketMinutes     = 15;
                TimeBaselineAlpha     = 0.05;
                MaxBarDurationSeconds = 900;
            }
            else if (State == State.DataLoaded)
            {
                hlc3Series = new Series<double>(this);
                rawSeries  = new Series<double>(this);

                emaPrice  = EMA(hlc3Series, _period);
                emaSignal = EMA(rawSeries,  _signalSmooth);

                atrInd   = ATR(_period);
                emaTrend = EMA(Close, _trendPeriod);

                recentPriceDiffs = new List<double>();

                numBuckets         = Math.Max(1, 1440 / Math.Max(1, _timeBucketMinutes));
                bucketDurationMean = new double[numBuckets];
                bucketInitialized  = new bool[numBuckets];
            }
        }

        protected override void OnBarUpdate()
        {
            int minBars = Math.Max(_trendPeriod, Math.Max(_period, _signalSmooth)) + 2;
            if (CurrentBar < minBars)
                return;

            // ── Step 1-4: same base formula as ZScoreHybrid.cs ─────────────────
            double hlc3 = (High[0] + Low[0] + Close[0]) / 3.0;
            hlc3Series[0] = hlc3;

            double mean   = emaPrice[0];
            double spread = atrInd[0];

            double raw = 0;
            if (spread != 0)
                raw = (hlc3 - mean) / spread;

            // ── Time-of-day bucket for this bar ─────────────────────────────────
            int minutesSinceMidnight    = Time[0].Hour * 60 + Time[0].Minute;
            int minutesSinceSessionOpen = ((minutesSinceMidnight - _sessionStartHour * 60) % 1440 + 1440) % 1440;
            int bucket = Math.Min(numBuckets - 1, minutesSinceSessionOpen / Math.Max(1, _timeBucketMinutes));

            // ── Intensity ratio: same-time-of-day bar-DURATION baseline ────────
            // (NOT volume -- on Volume bars every bar has ~the same volume by
            // construction, so a volume-based version of this is a dead no-op.
            // Bar duration is what actually carries time-of-day information here.)
            double intensityRatio;

            if (_useTimeOfDay)
            {
                double durationSeconds = CurrentBar >= 1 ? (Time[0] - Time[1]).TotalSeconds : _maxBarDurationSeconds;
                durationSeconds = Math.Max(0.1, Math.Min(_maxBarDurationSeconds, durationSeconds));

                double priorBucketMean = bucketDurationMean[bucket];
                intensityRatio = (bucketInitialized[bucket] && priorBucketMean > 0)
                    ? priorBucketMean / durationSeconds   // faster than normal for this time -> ratio > 1 (amplify)
                    : 1.0;                                 // no baseline yet for this time slot -- neutral until it builds up

                // update AFTER computing the ratio so this bar doesn't dilute its own surprise
                if (!bucketInitialized[bucket])
                {
                    bucketDurationMean[bucket] = durationSeconds;
                    bucketInitialized[bucket]  = true;
                }
                else
                {
                    bucketDurationMean[bucket] += _timeBaselineAlpha * (durationSeconds - bucketDurationMean[bucket]);
                }
            }
            else
            {
                intensityRatio = 1.0; // time-of-day piece off -- this variant is specifically for testing it on/off
            }

            intensityRatio = Math.Max(0.25, Math.Min(4.0, intensityRatio));

            // ── Order flow (BVC), same classifier as VolumeBucketVpin.cs ───────
            double participationMultiplier = 1.0;
            if (_useOrderFlow)
            {
                double priceDiff = Close[0] - Close[1];

                recentPriceDiffs.Add(priceDiff);
                if (recentPriceDiffs.Count > _orderFlowWindow)
                    recentPriceDiffs.RemoveAt(0);

                double buyFrac = 0.5;
                if (recentPriceDiffs.Count >= 2)
                {
                    double sigma = StdDev(recentPriceDiffs);
                    buyFrac = sigma > 0 ? NormalCdf(priceDiff / sigma) : 0.5;
                }

                double imbalanceRatio = 2.0 * buyFrac - 1.0; // -1..+1, buy pressure vs sell pressure

                // Amplify when order flow AGREES with price direction (raw's sign),
                // dampen when it disagrees -- i.e. price moved without real participation.
                participationMultiplier = 1.0 + _orderFlowWeight * Math.Sign(raw) * imbalanceRatio;
                participationMultiplier = Math.Max(0.3, Math.Min(2.0, participationMultiplier));
            }

            double combinedWeight = intensityRatio * participationMultiplier;
            combinedWeight = Math.Max(0.15, Math.Min(6.0, combinedWeight));

            // ── Scaled + smoothed signal ─────────────────────────────────────
            rawSeries[0] = raw * combinedWeight;

            double signal     = emaSignal[0];
            double prevSignal = emaSignal[1];

            Values[PLOT_SIGNAL][0] = signal;

            if      (signal >=  _obStrong) PlotBrushes[PLOT_SIGNAL][0] = Brushes.Red;
            else if (signal >=  _obMild  ) PlotBrushes[PLOT_SIGNAL][0] = Brushes.Orange;
            else if (signal <= _osStrong ) PlotBrushes[PLOT_SIGNAL][0] = Brushes.Lime;
            else if (signal <= _osMild   ) PlotBrushes[PLOT_SIGNAL][0] = Brushes.LimeGreen;
            else                           PlotBrushes[PLOT_SIGNAL][0] = Brushes.DodgerBlue;

            double trendEma    = emaTrend[0];
            bool   inUptrend   = Close[0] > trendEma;
            bool   inDowntrend = Close[0] < trendEma;

            bool intensityConfirmed = !_useIntensityGate || (intensityRatio >= _intensityThreshold);

            bool buyCondition  = (prevSignal <= _osStrong) && (signal > _osStrong)
                              && (!_useTrendFilter || inUptrend)
                              && intensityConfirmed;

            bool sellCondition = (prevSignal >= _obStrong) && (signal < _obStrong)
                              && (!_useTrendFilter || inDowntrend)
                              && intensityConfirmed;

            if (buyCondition)
            {
                Draw.TriangleUp(this, "FlowBuyPanel_" + CurrentBar, false, 0, _osStrong - 0.3, Brushes.Lime);
                Draw.TriangleUp(this, "FlowBuyChart_" + CurrentBar, false, 0, Low[0] - 2 * TickSize, Brushes.Lime, true);
            }

            if (sellCondition)
            {
                Draw.TriangleDown(this, "FlowSellPanel_" + CurrentBar, false, 0, _obStrong + 0.3, Brushes.Red);
                Draw.TriangleDown(this, "FlowSellChart_" + CurrentBar, false, 0, High[0] + 2 * TickSize, Brushes.Red, true);
            }
        }

        private static double StdDev(List<double> values)
        {
            double mean = 0;
            foreach (double v in values) mean += v;
            mean /= values.Count;

            double sumSq = 0;
            foreach (double v in values) sumSq += (v - mean) * (v - mean);
            return Math.Sqrt(sumSq / (values.Count - 1));
        }

        // Zelen & Severo (1964) rational approximation of the standard normal CDF --
        // same implementation as VolumeBucketVpin.cs, kept identical on purpose.
        private static double NormalCdf(double x)
        {
            double t = 1.0 / (1.0 + 0.2316419 * Math.Abs(x));
            double d = 0.3989422804014327 * Math.Exp(-x * x / 2.0);
            double prob = d * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
            return x >= 0 ? 1.0 - prob : prob;
        }

        // ════════════════════════════════════════════════════════════════════
        //  Properties
        // ════════════════════════════════════════════════════════════════════

        [NinjaScriptProperty]
        [Range(2, int.MaxValue)]
        [Display(Name = "Period", GroupName = "1. Formula Settings", Order = 1,
                 Description = "Lookback for EMA(HLC3) and ATR.")]
        public int Period
        { get { return _period; } set { _period = Math.Max(2, value); } }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Signal Smooth (bars)", GroupName = "1. Formula Settings", Order = 2)]
        public int SignalSmooth
        { get { return _signalSmooth; } set { _signalSmooth = Math.Max(1, value); } }

        [NinjaScriptProperty]
        [Range(10, int.MaxValue)]
        [Display(Name = "Trend EMA Period", GroupName = "1. Formula Settings", Order = 3)]
        public int TrendPeriod
        { get { return _trendPeriod; } set { _trendPeriod = Math.Max(10, value); } }

        [NinjaScriptProperty]
        [Display(Name = "Use Trend Filter", GroupName = "1. Formula Settings", Order = 4)]
        public bool UseTrendFilter
        { get { return _useTrendFilter; } set { _useTrendFilter = value; } }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "Strong OB Level", GroupName = "2. Reference Lines", Order = 1)]
        public double ObStrong
        { get { return _obStrong; } set { _obStrong = value; } }

        [NinjaScriptProperty]
        [Range(0.1, 10.0)]
        [Display(Name = "Mild OB Level", GroupName = "2. Reference Lines", Order = 2)]
        public double ObMild
        { get { return _obMild; } set { _obMild = value; } }

        [NinjaScriptProperty]
        [Range(-10.0, -0.1)]
        [Display(Name = "Strong OS Level", GroupName = "2. Reference Lines", Order = 3)]
        public double OsStrong
        { get { return _osStrong; } set { _osStrong = value; } }

        [NinjaScriptProperty]
        [Range(-10.0, -0.1)]
        [Display(Name = "Mild OS Level", GroupName = "2. Reference Lines", Order = 4)]
        public double OsMild
        { get { return _osMild; } set { _osMild = value; } }

        [NinjaScriptProperty]
        [Range(0.5, 5.0)]
        [Display(Name = "Intensity Gate Threshold", GroupName = "3. Intensity Settings", Order = 1,
                 Description = "Triangles only appear when the intensity ratio (time-of-day-adjusted) is >= this. e.g. 1.3 = bar filled 30% faster than normal for this time of day.")]
        public double IntensityThreshold
        { get { return _intensityThreshold; } set { _intensityThreshold = value; } }

        [NinjaScriptProperty]
        [Display(Name = "Use Intensity Gate", GroupName = "3. Intensity Settings", Order = 2,
                 Description = "When enabled, triangles only appear on bars that filled unusually fast for this time of day.")]
        public bool UseIntensityGate
        { get { return _useIntensityGate; } set { _useIntensityGate = value; } }

        [NinjaScriptProperty]
        [Display(Name = "Use Order Flow (BVC)", GroupName = "4. Order Flow", Order = 1,
                 Description = "Weight Scaled by buy/sell imbalance (BVC), not just raw volume.")]
        public bool UseOrderFlow
        { get { return _useOrderFlow; } set { _useOrderFlow = value; } }

        [NinjaScriptProperty]
        [Range(0.0, 2.0)]
        [Display(Name = "Order Flow Weight", GroupName = "4. Order Flow", Order = 2,
                 Description = "How strongly buy/sell imbalance amplifies (agreement) or dampens (divergence) the signal.")]
        public double OrderFlowWeight
        { get { return _orderFlowWeight; } set { _orderFlowWeight = value; } }

        [NinjaScriptProperty]
        [Range(2, int.MaxValue)]
        [Display(Name = "Order Flow Window (bars)", GroupName = "4. Order Flow", Order = 3,
                 Description = "Rolling window for the BVC sigma estimate.")]
        public int OrderFlowWindow
        { get { return _orderFlowWindow; } set { _orderFlowWindow = Math.Max(2, value); } }

        [NinjaScriptProperty]
        [Display(Name = "Use Time-of-Day Intensity Baseline", GroupName = "5. Time of Day", Order = 1,
                 Description = "Compare each bar's fill duration to a same-clock-time baseline (Volume bars: duration carries time-of-day info, raw volume does not).")]
        public bool UseTimeOfDay
        { get { return _useTimeOfDay; } set { _useTimeOfDay = value; } }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "Session Start Hour (ET, 0-23)", GroupName = "5. Time of Day", Order = 2,
                 Description = "18 = 18:00 NY ET, CME Globex open for index futures.")]
        public int SessionStartHour
        { get { return _sessionStartHour; } set { _sessionStartHour = Math.Max(0, Math.Min(23, value)); } }

        [NinjaScriptProperty]
        [Range(1, 240)]
        [Display(Name = "Time Bucket Width (minutes)", GroupName = "5. Time of Day", Order = 3,
                 Description = "Width of each same-clock-time baseline bucket.")]
        public int TimeBucketMinutes
        { get { return _timeBucketMinutes; } set { _timeBucketMinutes = Math.Max(1, value); } }

        [NinjaScriptProperty]
        [Range(0.001, 0.5)]
        [Display(Name = "Time Baseline Adapt Rate", GroupName = "5. Time of Day", Order = 4,
                 Description = "EMA smoothing rate for each time bucket's duration baseline -- lower = slower to adapt, more days of memory.")]
        public double TimeBaselineAlpha
        { get { return _timeBaselineAlpha; } set { _timeBaselineAlpha = Math.Max(0.001, Math.Min(0.5, value)); } }

        [NinjaScriptProperty]
        [Range(10.0, 3600.0)]
        [Display(Name = "Max Bar Duration (sec)", GroupName = "5. Time of Day", Order = 5,
                 Description = "Clips session-gap outliers (e.g. weekend/holiday gaps) before they corrupt a time bucket's baseline.")]
        public double MaxBarDurationSeconds
        { get { return _maxBarDurationSeconds; } set { _maxBarDurationSeconds = Math.Max(10.0, value); } }
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private ZScoreHybridFlow[] cacheZScoreHybridFlow;
		public ZScoreHybridFlow ZScoreHybridFlow(int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double intensityThreshold, bool useIntensityGate, bool useOrderFlow, double orderFlowWeight, int orderFlowWindow, bool useTimeOfDay, int sessionStartHour, int timeBucketMinutes, double timeBaselineAlpha, double maxBarDurationSeconds)
		{
			return ZScoreHybridFlow(Input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, intensityThreshold, useIntensityGate, useOrderFlow, orderFlowWeight, orderFlowWindow, useTimeOfDay, sessionStartHour, timeBucketMinutes, timeBaselineAlpha, maxBarDurationSeconds);
		}

		public ZScoreHybridFlow ZScoreHybridFlow(ISeries<double> input, int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double intensityThreshold, bool useIntensityGate, bool useOrderFlow, double orderFlowWeight, int orderFlowWindow, bool useTimeOfDay, int sessionStartHour, int timeBucketMinutes, double timeBaselineAlpha, double maxBarDurationSeconds)
		{
			if (cacheZScoreHybridFlow != null)
				for (int idx = 0; idx < cacheZScoreHybridFlow.Length; idx++)
					if (cacheZScoreHybridFlow[idx] != null && cacheZScoreHybridFlow[idx].Period == period && cacheZScoreHybridFlow[idx].SignalSmooth == signalSmooth && cacheZScoreHybridFlow[idx].TrendPeriod == trendPeriod && cacheZScoreHybridFlow[idx].UseTrendFilter == useTrendFilter && cacheZScoreHybridFlow[idx].ObStrong == obStrong && cacheZScoreHybridFlow[idx].ObMild == obMild && cacheZScoreHybridFlow[idx].OsStrong == osStrong && cacheZScoreHybridFlow[idx].OsMild == osMild && cacheZScoreHybridFlow[idx].IntensityThreshold == intensityThreshold && cacheZScoreHybridFlow[idx].UseIntensityGate == useIntensityGate && cacheZScoreHybridFlow[idx].UseOrderFlow == useOrderFlow && cacheZScoreHybridFlow[idx].OrderFlowWeight == orderFlowWeight && cacheZScoreHybridFlow[idx].OrderFlowWindow == orderFlowWindow && cacheZScoreHybridFlow[idx].UseTimeOfDay == useTimeOfDay && cacheZScoreHybridFlow[idx].SessionStartHour == sessionStartHour && cacheZScoreHybridFlow[idx].TimeBucketMinutes == timeBucketMinutes && cacheZScoreHybridFlow[idx].TimeBaselineAlpha == timeBaselineAlpha && cacheZScoreHybridFlow[idx].MaxBarDurationSeconds == maxBarDurationSeconds && cacheZScoreHybridFlow[idx].EqualsInput(input))
						return cacheZScoreHybridFlow[idx];
			return CacheIndicator<ZScoreHybridFlow>(new ZScoreHybridFlow(){ Period = period, SignalSmooth = signalSmooth, TrendPeriod = trendPeriod, UseTrendFilter = useTrendFilter, ObStrong = obStrong, ObMild = obMild, OsStrong = osStrong, OsMild = osMild, IntensityThreshold = intensityThreshold, UseIntensityGate = useIntensityGate, UseOrderFlow = useOrderFlow, OrderFlowWeight = orderFlowWeight, OrderFlowWindow = orderFlowWindow, UseTimeOfDay = useTimeOfDay, SessionStartHour = sessionStartHour, TimeBucketMinutes = timeBucketMinutes, TimeBaselineAlpha = timeBaselineAlpha, MaxBarDurationSeconds = maxBarDurationSeconds }, input, ref cacheZScoreHybridFlow);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.ZScoreHybridFlow ZScoreHybridFlow(int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double intensityThreshold, bool useIntensityGate, bool useOrderFlow, double orderFlowWeight, int orderFlowWindow, bool useTimeOfDay, int sessionStartHour, int timeBucketMinutes, double timeBaselineAlpha, double maxBarDurationSeconds)
		{
			return indicator.ZScoreHybridFlow(Input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, intensityThreshold, useIntensityGate, useOrderFlow, orderFlowWeight, orderFlowWindow, useTimeOfDay, sessionStartHour, timeBucketMinutes, timeBaselineAlpha, maxBarDurationSeconds);
		}

		public Indicators.ZScoreHybridFlow ZScoreHybridFlow(ISeries<double> input , int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double intensityThreshold, bool useIntensityGate, bool useOrderFlow, double orderFlowWeight, int orderFlowWindow, bool useTimeOfDay, int sessionStartHour, int timeBucketMinutes, double timeBaselineAlpha, double maxBarDurationSeconds)
		{
			return indicator.ZScoreHybridFlow(input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, intensityThreshold, useIntensityGate, useOrderFlow, orderFlowWeight, orderFlowWindow, useTimeOfDay, sessionStartHour, timeBucketMinutes, timeBaselineAlpha, maxBarDurationSeconds);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.ZScoreHybridFlow ZScoreHybridFlow(int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double intensityThreshold, bool useIntensityGate, bool useOrderFlow, double orderFlowWeight, int orderFlowWindow, bool useTimeOfDay, int sessionStartHour, int timeBucketMinutes, double timeBaselineAlpha, double maxBarDurationSeconds)
		{
			return indicator.ZScoreHybridFlow(Input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, intensityThreshold, useIntensityGate, useOrderFlow, orderFlowWeight, orderFlowWindow, useTimeOfDay, sessionStartHour, timeBucketMinutes, timeBaselineAlpha, maxBarDurationSeconds);
		}

		public Indicators.ZScoreHybridFlow ZScoreHybridFlow(ISeries<double> input , int period, int signalSmooth, int trendPeriod, bool useTrendFilter, double obStrong, double obMild, double osStrong, double osMild, double intensityThreshold, bool useIntensityGate, bool useOrderFlow, double orderFlowWeight, int orderFlowWindow, bool useTimeOfDay, int sessionStartHour, int timeBucketMinutes, double timeBaselineAlpha, double maxBarDurationSeconds)
		{
			return indicator.ZScoreHybridFlow(input, period, signalSmooth, trendPeriod, useTrendFilter, obStrong, obMild, osStrong, osMild, intensityThreshold, useIntensityGate, useOrderFlow, orderFlowWeight, orderFlowWindow, useTimeOfDay, sessionStartHour, timeBucketMinutes, timeBaselineAlpha, maxBarDurationSeconds);
		}
	}
}

#endregion
