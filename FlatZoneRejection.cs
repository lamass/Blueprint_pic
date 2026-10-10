#region Using declarations
using System;
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
//  FlatZoneRejection  --  2 params, built for a 1-minute chart
//
//  v2: clusters on HIGHS (ceiling test) or LOWS (floor test) only --
//  NOT the full high-low range of each bar. v1 required the entire
//  range of BarsCount bars to be tight, which missed real examples
//  like "price hits ~31281-31282.5 four times in a row (5 ticks
//  apart) while the lows swing 60+ ticks underneath" -- that IS a
//  real, repeated test of the same level, just not a quiet box.
//  Checked against real NQ 1-min data before shipping this change.
//
//  Two independent zones can be live at once -- a ceiling test and
//  a floor test are different things and don't need to exclude
//  each other.
//
//  TOP zone (ceiling test):
//    forms when the highs of the last BarsCount bars are all within
//    TickTolerance of each other. Level = the highest of those highs.
//    invalidates UP (green up-arrow) if a new high clears level+tol
//      -- the ceiling broke, price continued higher.
//    invalidates DOWN (red down-arrow) if a bar CLOSES below
//      level-tol -- not just wicks there (wicking under the ceiling
//      while it's being tested is normal and expected), but a real
//      close away from it, confirming the rejection.
//
//  BOTTOM zone (floor test): the mirror image, using lows and the
//  opposite close condition.
//
//  Params (only 2, by design):
//    BarsCount     -- how many consecutive highs (or lows) must
//                      cluster this tight to count as a level test
//                      (default 4)
//    TickTolerance -- both the cluster width and the break/close
//                      distance that invalidates the line (default
//                      5 ticks -- calibrated against real NQ 1-min
//                      data so it's rare/meaningful, not noise)
// ================================================================

namespace NinjaTrader.NinjaScript.Indicators
{
    public class FlatZoneRejection : Indicator
    {
        private int _barsCount     = 4;
        private int _tickTolerance = 5;

        private bool   topActive,  botActive;
        private double topLevel,   botLevel;
        private int    topStartBar, botStartBar;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Finds repeated tests of the same high or low (BarsCount bars clustered within TickTolerance), draws a line, invalidates with an arrow on a real break/close away. Built for a 1-minute chart.";
                Name        = "FlatZoneRejection";
                Calculate   = Calculate.OnBarClose;
                IsOverlay   = true;
                IsSuspendedWhileInactive = true;

                BarsCount     = 4;
                TickTolerance = 5;
            }
            else if (State == State.DataLoaded)
            {
                topActive = false;
                botActive = false;
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < _barsCount - 1)
                return;

            double tol = _tickTolerance * TickSize;

            // ── look for a new ceiling test if none is active ──────────────────
            if (!topActive)
            {
                double wHighMax = MAX(High, _barsCount)[0];
                double wHighMin = MIN(High, _barsCount)[0];
                if (wHighMax - wHighMin <= tol)
                {
                    topActive   = true;
                    topLevel    = wHighMax;
                    topStartBar = CurrentBar - _barsCount + 1;
                }
            }

            // ── look for a new floor test if none is active ─────────────────────
            if (!botActive)
            {
                double wLowMax = MAX(Low, _barsCount)[0];
                double wLowMin = MIN(Low, _barsCount)[0];
                if (wLowMax - wLowMin <= tol)
                {
                    botActive   = true;
                    botLevel    = wLowMin;
                    botStartBar = CurrentBar - _barsCount + 1;
                }
            }

            if (topActive)
            {
                int startBarsAgo = CurrentBar - topStartBar;
                Draw.Line(this, "FlatZoneTop_" + topStartBar, false,
                    startBarsAgo, topLevel, 0, topLevel,
                    Brushes.Goldenrod, DashStyleHelper.Solid, 2);

                if (High[0] > topLevel + tol)
                {
                    Draw.ArrowUp(this, "FlatZoneTopUp_" + CurrentBar, false, 0, Low[0] - 2 * TickSize, Brushes.Lime);
                    topActive = false;
                }
                else if (Close[0] < topLevel - tol)
                {
                    Draw.ArrowDown(this, "FlatZoneTopDown_" + CurrentBar, false, 0, High[0] + 2 * TickSize, Brushes.Red);
                    topActive = false;
                }
            }

            if (botActive)
            {
                int startBarsAgo = CurrentBar - botStartBar;
                Draw.Line(this, "FlatZoneBot_" + botStartBar, false,
                    startBarsAgo, botLevel, 0, botLevel,
                    Brushes.Goldenrod, DashStyleHelper.Solid, 2);

                if (Low[0] < botLevel - tol)
                {
                    Draw.ArrowDown(this, "FlatZoneBotDown_" + CurrentBar, false, 0, High[0] + 2 * TickSize, Brushes.Red);
                    botActive = false;
                }
                else if (Close[0] > botLevel + tol)
                {
                    Draw.ArrowUp(this, "FlatZoneBotUp_" + CurrentBar, false, 0, Low[0] - 2 * TickSize, Brushes.Lime);
                    botActive = false;
                }
            }
        }

        // ════════════════════════════════════════════════════════════════════
        //  Properties
        // ════════════════════════════════════════════════════════════════════

        [NinjaScriptProperty]
        [Range(2, 50)]
        [Display(Name = "Bars Count", GroupName = "Parameters", Order = 1,
                 Description = "Number of consecutive highs (or lows) that must cluster within TickTolerance to count as a level test.")]
        public int BarsCount
        { get { return _barsCount; } set { _barsCount = Math.Max(2, value); } }

        [NinjaScriptProperty]
        [Range(1, 500)]
        [Display(Name = "Tick Tolerance", GroupName = "Parameters", Order = 2,
                 Description = "Width of the cluster (in ticks) to qualify, and how far price may break/close from the line before it's invalidated.")]
        public int TickTolerance
        { get { return _tickTolerance; } set { _tickTolerance = Math.Max(1, value); } }
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private FlatZoneRejection[] cacheFlatZoneRejection;
		public FlatZoneRejection FlatZoneRejection(int barsCount, int tickTolerance)
		{
			return FlatZoneRejection(Input, barsCount, tickTolerance);
		}

		public FlatZoneRejection FlatZoneRejection(ISeries<double> input, int barsCount, int tickTolerance)
		{
			if (cacheFlatZoneRejection != null)
				for (int idx = 0; idx < cacheFlatZoneRejection.Length; idx++)
					if (cacheFlatZoneRejection[idx] != null && cacheFlatZoneRejection[idx].BarsCount == barsCount && cacheFlatZoneRejection[idx].TickTolerance == tickTolerance && cacheFlatZoneRejection[idx].EqualsInput(input))
						return cacheFlatZoneRejection[idx];
			return CacheIndicator<FlatZoneRejection>(new FlatZoneRejection(){ BarsCount = barsCount, TickTolerance = tickTolerance }, input, ref cacheFlatZoneRejection);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.FlatZoneRejection FlatZoneRejection(int barsCount, int tickTolerance)
		{
			return indicator.FlatZoneRejection(Input, barsCount, tickTolerance);
		}

		public Indicators.FlatZoneRejection FlatZoneRejection(ISeries<double> input , int barsCount, int tickTolerance)
		{
			return indicator.FlatZoneRejection(input, barsCount, tickTolerance);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.FlatZoneRejection FlatZoneRejection(int barsCount, int tickTolerance)
		{
			return indicator.FlatZoneRejection(Input, barsCount, tickTolerance);
		}

		public Indicators.FlatZoneRejection FlatZoneRejection(ISeries<double> input , int barsCount, int tickTolerance)
		{
			return indicator.FlatZoneRejection(input, barsCount, tickTolerance);
		}
	}
}

#endregion
