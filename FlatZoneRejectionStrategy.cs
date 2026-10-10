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
using NinjaTrader.Gui.SuperDom;
using NinjaTrader.Core.FloatingPoint;
#endregion

// ================================================================
//  FlatZoneRejectionStrategy  --  trades the FlatZoneRejection signal
//
//  SHORT on a ceiling rejection: BarsCount highs cluster within
//    TickTolerance (a tested high), then a bar CLOSES back below
//    level-TickTolerance (rejection confirmed, not just a wick).
//  LONG on a floor rejection: mirror image, using lows.
//
//  Breakout invalidations (ceiling cleared upward / floor broken
//  downward) are NOT traded here -- those are continuation, not
//  rejection, and this strategy is specifically testing the
//  rejection idea.
//
//  Honest backtest before shipping this (nq0926_1min_candles.json,
//  58 days, BarsCount=4/TickTolerance=5, same TargetProfit/StopLoss
//  as the defaults below): 1559 rejection signals, 15.7% win rate
//  against a 14.3% breakeven threshold at 90/15 -- barely positive,
//  +0.10R/trade. Real, not fake, but thin: no trend or time-of-day
//  filter yet, and this ignores commissions/slippage. Treat the
//  defaults as a starting point to test live/in-sim, not a settled
//  edge.
// ================================================================

namespace NinjaTrader.NinjaScript.Strategies
{
    public class FlatZoneRejectionStrategy : Strategy
    {
        private int    _barsCount       = 4;
        private int    _tickTolerance   = 5;
        private double _targetPoints    = 90;
        private double _stopPoints      = 15;

        private bool   topActive,  botActive;
        private double topLevel,   botLevel;
        private int    topStartBar, botStartBar;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Trades FlatZoneRejection's ceiling/floor rejection signal. Short on confirmed ceiling rejection, long on confirmed floor rejection. Built for a 1-minute chart.";
                Name        = "FlatZoneRejectionStrategy";
                Calculate   = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                IsFillLimitOnTouch = false;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
                OrderFillResolution = OrderFillResolution.Standard;
                Slippage = 0;
                StartBehavior = StartBehavior.WaitUntilFlat;
                TimeInForce = TimeInForce.Gtc;
                TraceOrders = false;
                RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
                StopTargetHandling = StopTargetHandling.PerEntryExecution;
                BarsRequiredToTrade = 20;
                IsOverlay = true;

                BarsCount     = 4;
                TickTolerance = 5;
                TargetPoints  = 90;
                StopPoints    = 15;
            }
            else if (State == State.Configure)
            {
            }
            else if (State == State.DataLoaded)
            {
                topActive = false;
                botActive = false;

                SetProfitTarget(CalculationMode.Ticks, _targetPoints / TickSize);
                SetStopLoss(CalculationMode.Ticks, _stopPoints / TickSize);
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < _barsCount - 1)
                return;

            double tol = _tickTolerance * TickSize;

            // ── detect a new ceiling or floor test if none active ──────────────
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

            // ── ceiling: draw while active, short on a confirmed rejection ─────
            if (topActive)
            {
                int startBarsAgo = CurrentBar - topStartBar;
                Draw.Line(this, "FZTop_" + topStartBar, false,
                    startBarsAgo, topLevel, 0, topLevel,
                    Brushes.Goldenrod, DashStyleHelper.Solid, 2);

                if (High[0] > topLevel + tol)
                {
                    // ceiling broke upward -- continuation, not traded here
                    Draw.ArrowUp(this, "FZTopUp_" + CurrentBar, false, 0, Low[0] - 2 * TickSize, Brushes.Lime);
                    topActive = false;
                }
                else if (Close[0] < topLevel - tol)
                {
                    Draw.ArrowDown(this, "FZTopDown_" + CurrentBar, false, 0, High[0] + 2 * TickSize, Brushes.Red);
                    topActive = false;

                    if (Position.MarketPosition == MarketPosition.Flat)
                        EnterShort("FZShort");
                }
            }

            // ── floor: draw while active, long on a confirmed rejection ────────
            if (botActive)
            {
                int startBarsAgo = CurrentBar - botStartBar;
                Draw.Line(this, "FZBot_" + botStartBar, false,
                    startBarsAgo, botLevel, 0, botLevel,
                    Brushes.Goldenrod, DashStyleHelper.Solid, 2);

                if (Low[0] < botLevel - tol)
                {
                    // floor broke downward -- continuation, not traded here
                    Draw.ArrowDown(this, "FZBotDown_" + CurrentBar, false, 0, High[0] + 2 * TickSize, Brushes.Red);
                    botActive = false;
                }
                else if (Close[0] > botLevel + tol)
                {
                    Draw.ArrowUp(this, "FZBotUp_" + CurrentBar, false, 0, Low[0] - 2 * TickSize, Brushes.Lime);
                    botActive = false;

                    if (Position.MarketPosition == MarketPosition.Flat)
                        EnterLong("FZLong");
                }
            }
        }

        // ════════════════════════════════════════════════════════════════════
        //  Properties
        // ════════════════════════════════════════════════════════════════════

        [NinjaScriptProperty]
        [Range(2, 50)]
        [Display(Name = "Bars Count", GroupName = "1. Signal", Order = 1,
                 Description = "Number of consecutive highs (or lows) that must cluster within TickTolerance to count as a level test.")]
        public int BarsCount
        { get { return _barsCount; } set { _barsCount = Math.Max(2, value); } }

        [NinjaScriptProperty]
        [Range(1, 500)]
        [Display(Name = "Tick Tolerance", GroupName = "1. Signal", Order = 2,
                 Description = "Width of the cluster (ticks) to qualify, and how far price may close away before the rejection is confirmed.")]
        public int TickTolerance
        { get { return _tickTolerance; } set { _tickTolerance = Math.Max(1, value); } }

        [NinjaScriptProperty]
        [Range(1.0, 1000.0)]
        [Display(Name = "Target (points)", GroupName = "2. Risk", Order = 1)]
        public double TargetPoints
        { get { return _targetPoints; } set { _targetPoints = Math.Max(0.25, value); } }

        [NinjaScriptProperty]
        [Range(1.0, 1000.0)]
        [Display(Name = "Stop (points)", GroupName = "2. Risk", Order = 2)]
        public double StopPoints
        { get { return _stopPoints; } set { _stopPoints = Math.Max(0.25, value); } }
    }
}
