#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

//  ─────────────────────────────────────────────────────────────────────────────
//  DirectionalCandleCounter  (v10)
//  ─────────────────────────────────────────────────────────────────────────────
//  Core rule:
//    A candle is numbered ONLY if its Close is more than PipThreshold ticks
//    HIGHER (up) or LOWER (down) than the last NUMBERED candle's close.
//    lastNumberedClose updates ONLY when a candle is actually numbered.
//
//  Single candle filter:
//    A direction change "1" is drawn immediately but stays pending.
//    If the next bar does not confirm → label erased, state reverted.
//
//  Debug:
//    Prints to Output window ONLY when a candle is numbered, showing
//    the close and lastNumberedClose so you can spot any bad counts.
//  ─────────────────────────────────────────────────────────────────────────────

namespace NinjaTrader.NinjaScript.Indicators
{
    public class DirectionalCandleCounter : Indicator
    {
        // ── active state ──────────────────────────────────────────────────────
        private int    upCount;
        private int    downCount;
        private bool   directionIsUp;
        private bool   directionSet;
        private double lastNumberedClose;

        // ── pending state ─────────────────────────────────────────────────────
        private bool   pendingChange;
        private bool   pendingIsUp;
        private int    pendingBarIndex;
        private int    savedUpCount;
        private int    savedDownCount;
        private bool   savedDirectionIsUp;
        private double savedLastNumberedClose;

        // ── parameters ───────────────────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Pip Threshold (ticks)", Order = 1, GroupName = "Parameters")]
        public int PipThreshold { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Up Label Color", Order = 2, GroupName = "Colors")]
        public Brush UpColor { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Down Label Color", Order = 3, GroupName = "Colors")]
        public Brush DownColor { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Font Size", Order = 4, GroupName = "Display")]
        public int FontSize { get; set; }

        // ─────────────────────────────────────────────────────────────────────
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description         = "Numbers candles only when Close exceeds last numbered close by 4+ ticks.";
                Name                = "DirectionalCandleCounter";
                Calculate           = Calculate.OnBarClose;
                IsOverlay           = true;
                DisplayInDataBox    = false;
                DrawOnPricePanel    = true;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
                PipThreshold        = 4;
                UpColor             = Brushes.DodgerBlue;
                DownColor           = Brushes.OrangeRed;
                FontSize            = 11;
            }
            else if (State == State.DataLoaded)
            {
                upCount                = 0;
                downCount              = 0;
                directionIsUp          = true;
                directionSet           = false;
                lastNumberedClose      = 0;
                pendingChange          = false;
                pendingIsUp            = false;
                pendingBarIndex        = -1;
                savedUpCount           = 0;
                savedDownCount         = 0;
                savedDirectionIsUp     = true;
                savedLastNumberedClose = 0;
            }
        }

        // ─────────────────────────────────────────────────────────────────────
        protected override void OnBarUpdate()
        {
            if (CurrentBar < 2)
                return;

            // Seed lastNumberedClose on very first bar processed
            if (!directionSet && lastNumberedClose == 0)
                lastNumberedClose = Close[0];

            double threshold   = PipThreshold * TickSize;
            bool qualifiesUp   = Close[0] > lastNumberedClose + threshold;
            bool qualifiesDown = Close[0] < lastNumberedClose - threshold;

            // Outside bar — honour stronger move
            if (qualifiesUp && qualifiesDown)
            {
                double upMove   = Close[0] - lastNumberedClose;
                double downMove = lastNumberedClose - Close[0];
                if (upMove >= downMove) qualifiesDown = false;
                else                    qualifiesUp   = false;
            }

            // ── Resolve pending direction change ──────────────────────────────
            if (pendingChange)
            {
                bool confirmed = (pendingIsUp && qualifiesUp) || (!pendingIsUp && qualifiesDown);
                if (!confirmed)
                {
                    RemoveDrawObject((pendingIsUp ? "U_" : "D_") + pendingBarIndex);
                    upCount           = savedUpCount;
                    downCount         = savedDownCount;
                    directionIsUp     = savedDirectionIsUp;
                    lastNumberedClose = savedLastNumberedClose;
                }
                pendingChange = false;

                // Re-evaluate qualification after possible revert
                qualifiesUp   = Close[0] > lastNumberedClose + threshold;
                qualifiesDown = Close[0] < lastNumberedClose - threshold;
                if (qualifiesUp && qualifiesDown)
                {
                    double upMove   = Close[0] - lastNumberedClose;
                    double downMove = lastNumberedClose - Close[0];
                    if (upMove >= downMove) qualifiesDown = false;
                    else                    qualifiesUp   = false;
                }
            }

            if (!qualifiesUp && !qualifiesDown)
                return;

            // ── Up ────────────────────────────────────────────────────────────
            if (qualifiesUp)
            {
                if (!directionSet)
                {
                    upCount = 1; directionIsUp = true; directionSet = true;
                    lastNumberedClose = Close[0];
                    Print(string.Format("BAR {0} {1} | UP #{2} | Close={3} lastRef={4}",
                        CurrentBar, Time[0].ToString("HH:mm"), upCount, Close[0], lastNumberedClose));
                    DrawUp();
                }
                else if (!directionIsUp)
                {
                    // Direction change → pending
                    savedUpCount = upCount; savedDownCount = downCount;
                    savedDirectionIsUp = directionIsUp;
                    savedLastNumberedClose = lastNumberedClose;
                    upCount = 1; directionIsUp = true;
                    lastNumberedClose = Close[0];
                    pendingChange = true; pendingIsUp = true; pendingBarIndex = CurrentBar;
                    Print(string.Format("BAR {0} {1} | UP #1 PENDING | Close={2} lastRef={3}",
                        CurrentBar, Time[0].ToString("HH:mm"), Close[0], savedLastNumberedClose));
                    DrawUp();
                }
                else
                {
                    upCount++;
                    lastNumberedClose = Close[0];
                    Print(string.Format("BAR {0} {1} | UP #{2} | Close={3} lastRef={4}",
                        CurrentBar, Time[0].ToString("HH:mm"), upCount, Close[0], lastNumberedClose));
                    DrawUp();
                }
            }
            // ── Down ──────────────────────────────────────────────────────────
            else
            {
                if (!directionSet)
                {
                    downCount = 1; directionIsUp = false; directionSet = true;
                    lastNumberedClose = Close[0];
                    Print(string.Format("BAR {0} {1} | DN #{2} | Close={3} lastRef={4}",
                        CurrentBar, Time[0].ToString("HH:mm"), downCount, Close[0], lastNumberedClose));
                    DrawDown();
                }
                else if (directionIsUp)
                {
                    // Direction change → pending
                    savedUpCount = upCount; savedDownCount = downCount;
                    savedDirectionIsUp = directionIsUp;
                    savedLastNumberedClose = lastNumberedClose;
                    downCount = 1; directionIsUp = false;
                    lastNumberedClose = Close[0];
                    pendingChange = true; pendingIsUp = false; pendingBarIndex = CurrentBar;
                    Print(string.Format("BAR {0} {1} | DN #1 PENDING | Close={2} lastRef={3}",
                        CurrentBar, Time[0].ToString("HH:mm"), Close[0], savedLastNumberedClose));
                    DrawDown();
                }
                else
                {
                    downCount++;
                    lastNumberedClose = Close[0];
                    Print(string.Format("BAR {0} {1} | DN #{2} | Close={3} lastRef={4}",
                        CurrentBar, Time[0].ToString("HH:mm"), downCount, Close[0], lastNumberedClose));
                    DrawDown();
                }
            }
        }

        // ── draw helpers ──────────────────────────────────────────────────────
        private void DrawUp()
        {
            Draw.Text(this, "U_" + CurrentBar, true, upCount.ToString(), 0, High[0], 8,
                UpColor,
                new NinjaTrader.Gui.Tools.SimpleFont("Arial", FontSize) { Bold = true },
                System.Windows.TextAlignment.Center,
                Brushes.Transparent, Brushes.Transparent, 0);
        }

        private void DrawDown()
        {
            Draw.Text(this, "D_" + CurrentBar, true, downCount.ToString(), 0, Low[0], -8,
                DownColor,
                new NinjaTrader.Gui.Tools.SimpleFont("Arial", FontSize) { Bold = true },
                System.Windows.TextAlignment.Center,
                Brushes.Transparent, Brushes.Transparent, 0);
        }
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private DirectionalCandleCounter[] cacheDirectionalCandleCounter;
		public DirectionalCandleCounter DirectionalCandleCounter(int pipThreshold, Brush upColor, Brush downColor, int fontSize)
		{
			return DirectionalCandleCounter(Input, pipThreshold, upColor, downColor, fontSize);
		}

		public DirectionalCandleCounter DirectionalCandleCounter(ISeries<double> input, int pipThreshold, Brush upColor, Brush downColor, int fontSize)
		{
			if (cacheDirectionalCandleCounter != null)
				for (int idx = 0; idx < cacheDirectionalCandleCounter.Length; idx++)
					if (cacheDirectionalCandleCounter[idx] != null && cacheDirectionalCandleCounter[idx].PipThreshold == pipThreshold && cacheDirectionalCandleCounter[idx].UpColor == upColor && cacheDirectionalCandleCounter[idx].DownColor == downColor && cacheDirectionalCandleCounter[idx].FontSize == fontSize && cacheDirectionalCandleCounter[idx].EqualsInput(input))
						return cacheDirectionalCandleCounter[idx];
			return CacheIndicator<DirectionalCandleCounter>(new DirectionalCandleCounter(){ PipThreshold = pipThreshold, UpColor = upColor, DownColor = downColor, FontSize = fontSize }, input, ref cacheDirectionalCandleCounter);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.DirectionalCandleCounter DirectionalCandleCounter(int pipThreshold, Brush upColor, Brush downColor, int fontSize)
		{
			return indicator.DirectionalCandleCounter(Input, pipThreshold, upColor, downColor, fontSize);
		}

		public Indicators.DirectionalCandleCounter DirectionalCandleCounter(ISeries<double> input , int pipThreshold, Brush upColor, Brush downColor, int fontSize)
		{
			return indicator.DirectionalCandleCounter(input, pipThreshold, upColor, downColor, fontSize);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.DirectionalCandleCounter DirectionalCandleCounter(int pipThreshold, Brush upColor, Brush downColor, int fontSize)
		{
			return indicator.DirectionalCandleCounter(Input, pipThreshold, upColor, downColor, fontSize);
		}

		public Indicators.DirectionalCandleCounter DirectionalCandleCounter(ISeries<double> input , int pipThreshold, Brush upColor, Brush downColor, int fontSize)
		{
			return indicator.DirectionalCandleCounter(input, pipThreshold, upColor, downColor, fontSize);
		}
	}
}

#endregion
