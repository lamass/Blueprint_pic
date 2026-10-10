#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.SuperDom;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.Core.FloatingPoint;
#endregion

// Five-indicator composite entry strategy. Each indicator (SMA, Stochastic, MACD, RSI,
// Momentum) has its own On/Off switch and computes a per-bar directional vote: +1 (Long),
// -1 (Short), or 0 (neutral) against its own adjustable trigger level(s):
//   SMA        : Long when Close > SMA, Short when Close < SMA (the one rule not specified
//                in the request this was built from -- flagged, change if a different rule
//                was intended).
//   Stochastic : event-based, same as the original baseline -- Long on a fresh %K cross
//                below StochOversold, Short on a fresh cross above StochOverbought.
//   MACD       : level-based -- Long when the MACD line > MacdUpperThreshold (default 0),
//                Short when < MacdLowerThreshold (default 0).
//   RSI        : level-based -- Long when RSI < RsiLongTrigger (default 20), Short when
//                RSI > RsiShortTrigger (default 80).
//   Momentum   : level-based -- Long when Momentum > MomentumUpperThreshold (default 0),
//                Short when < MomentumLowerThreshold (default 0).
//
// RequireAllEnabledToAgree (default true) controls how multiple enabled votes combine:
// true = AND/confirmation (every enabled indicator must agree the same direction that bar --
// with only one indicator on, this reduces to that indicator trading alone); false = OR/
// independent (any enabled indicator's own vote can fire a trade on its own, ties where one
// enabled indicator says Long and another says Short on the same bar are skipped either way).
//
// ReverseAllSignals (default false) flips the final Long/Short decision after combination --
// a blanket "trade the opposite of whatever this rule says" switch.
//
// AllowLongs / AllowShorts (both default true) gate the final direction: a signal for a
// disabled direction is simply dropped that bar.
//
// LongEntranceOffset / ShortEntranceOffset (points, default 0, can be negative): on trigger,
// the bar's price is remembered and a resting LIMIT order is placed that many points below
// (Long) or above (Short) it -- positive is a pullback entry, negative chases price instead.
// The order sits until price revisits that level and fills, or until a fresh signal in the
// OPPOSITE direction arrives first, which cancels it (the original setup is invalidated) and
// arms a new pending order for the new direction; a same-direction signal while one is already
// pending is just ignored (still waiting on the first one). With both offsets at 0 this places
// a limit right at the trigger price, closely matching immediate entry.
//
// Change bar period (5min/10min/etc) by applying this strategy to a chart with that period --
// no code change needed. All params below are adjustable from the Strategy's Parameters tab
// or swept in Strategy Analyzer (IsInstantiatedOnEachOptimizationIteration = true).
//
// All five indicators auto-plot on the chart via the five AddChartIndicator calls in
// State.DataLoaded -- comment out any of those lines to stop that one from appearing (the
// trading logic itself is unaffected either way, since it always evaluates all five). Stochastic
// and Momentum aren't built-in NT8 indicator objects (they're computed inline), so their chart
// display comes from two small companion files, StochExtremesStoch.cs and
// StochExtremesMomentum.cs, that duplicate the exact same formulas purely for plotting -- both
// need to sit alongside this file in the same NinjaScript folder to compile.
//
// Descended from stoch_extremes_backtest.py's Stochastic-only baseline (NQ 09-26: 476 trades,
// 48.1% win rate, -1709.0 pts) -- this version generalizes that into a switchable multi-
// indicator composite so combinations can be explored live instead of one Python rewrite
// per idea.

namespace NinjaTrader.NinjaScript.Strategies
{
    public class StochExtremesStrategy : Strategy
    {
        private Series<double> rawK;
        private Series<double> smoothK;
        private Series<double> dLine;

        private SMA sma;
        private MACD macd;
        private RSI rsi;

        private int drawCounter = 0;
        private int tradeEntryBar = -1;
        private double tradeEntryPrice = 0;
        private string tradeDirection = "";

        private bool pendingEntryActive = false;
        private int pendingDirectionSign = 0;
        private Order pendingOrder = null;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Composite entry from switchable SMA/Stochastic/MACD/RSI/Momentum votes (each On/Off with its own trigger level), AND or OR combination, optional global reverse, Long/Short allow toggles, offset limit entries; fixed points TP/SL.";
                Name = "StochExtremesStrategy";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = false;
                IsFillLimitOnTouch = false;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
                OrderFillResolution = OrderFillResolution.Standard;
                Slippage = 0;
                StartBehavior = StartBehavior.WaitUntilFlat;
                TimeInForce = TimeInForce.Gtc;
                TraceOrders = false;
                RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
                StopTargetHandling = StopTargetHandling.PerEntryExecution;
                BarsRequiredToTrade = 50;
                IsInstantiatedOnEachOptimizationIteration = true;

                ReverseAllSignals = false;
                RequireAllEnabledToAgree = true;
                AllowLongs = true;
                AllowShorts = true;
                LongEntranceOffset = 0;
                ShortEntranceOffset = 0;

                UseSma = false;
                SmaPeriod = 20;

                UseStochastic = true;
                StochKPeriod = 14;
                StochKSmooth = 3;
                StochDPeriod = 3;
                StochOverbought = 80;
                StochOversold = 20;

                UseMacd = false;
                MacdFast = 12;
                MacdSlow = 26;
                MacdSignal = 9;
                MacdUpperThreshold = 0;
                MacdLowerThreshold = 0;

                UseRsi = false;
                RsiPeriod = 10;
                RsiSmooth = 1;
                RsiLongTrigger = 20;
                RsiShortTrigger = 80;

                UseMomentum = false;
                MomentumPeriod = 10;
                MomentumUpperThreshold = 0;
                MomentumLowerThreshold = 0;

                ProfitTargetPoints = 90;
                StopLossPoints = 90;
            }
            else if (State == State.Configure)
            {
                BarsRequiredToTrade = Math.Max(SmaPeriod,
                    Math.Max(MacdSlow + MacdSignal,
                    Math.Max(StochKPeriod + StochKSmooth + StochDPeriod,
                    Math.Max(RsiPeriod, MomentumPeriod)))) + 5;
            }
            else if (State == State.DataLoaded)
            {
                rawK = new Series<double>(this);
                smoothK = new Series<double>(this);
                dLine = new Series<double>(this);

                sma = SMA(SmaPeriod);
                macd = MACD(MacdFast, MacdSlow, MacdSignal);
                rsi = RSI(RsiPeriod, RsiSmooth);
                StochExtremesStoch stochDisplay = StochExtremesStoch(StochKPeriod, StochKSmooth, StochDPeriod);
                StochExtremesMomentum momentumDisplay = StochExtremesMomentum(MomentumPeriod);

                // Comment out any of these five to drop that indicator from the chart --
                // each line is independent and doesn't affect the trading logic above, which
                // always evaluates all five regardless of what's plotted.
                AddChartIndicator(sma);
                AddChartIndicator(macd);
                AddChartIndicator(rsi);
                AddChartIndicator(stochDisplay);
                AddChartIndicator(momentumDisplay);

                // CalculationMode.Ticks used deliberately over .Price: .Price was silently
                // treating the value as an absolute price level rather than a points-offset
                // from entry, so any StopLossPoints value (50/100/300/...) was an unreachable
                // absolute price for NQ and the stop effectively never fired -- every trade
                // exited the same way (via target) no matter what stop was configured. Ticks
                // is unambiguous: always N ticks from entry.
                SetProfitTarget(CalculationMode.Ticks, ProfitTargetPoints / TickSize);
                SetStopLoss(CalculationMode.Ticks, StopLossPoints / TickSize);
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < BarsRequiredToTrade)
                return;

            // --- Stochastic: event-based (fresh-cross pulse, 0 on every other bar) ---
            double hh = MAX(High, StochKPeriod)[0];
            double ll = MIN(Low, StochKPeriod)[0];
            rawK[0] = (hh - ll) == 0 ? 50.0 : 100.0 * (Close[0] - ll) / (hh - ll);

            int kAvail = Math.Min(StochKSmooth, CurrentBar + 1);
            double kSum = 0;
            for (int i = 0; i < kAvail; i++) kSum += rawK[i];
            smoothK[0] = kSum / kAvail;

            int dAvail = Math.Min(StochDPeriod, CurrentBar + 1);
            double dSum = 0;
            for (int i = 0; i < dAvail; i++) dSum += smoothK[i];
            dLine[0] = dSum / dAvail;

            if (CurrentBar < 1)
                return;

            double kNow = smoothK[0];
            double kPrev = smoothK[1];

            int stochSignal = 0;
            if (kPrev > StochOversold && kNow <= StochOversold)
                stochSignal = 1;
            else if (kPrev < StochOverbought && kNow >= StochOverbought)
                stochSignal = -1;

            // --- SMA: level-based (price vs SMA) ---
            int smaSignal = 0;
            if (Close[0] > sma[0])
                smaSignal = 1;
            else if (Close[0] < sma[0])
                smaSignal = -1;

            // --- MACD: level-based (MACD line vs thresholds) ---
            int macdSignal = 0;
            double macdVal = macd.Default[0];
            if (macdVal > MacdUpperThreshold)
                macdSignal = 1;
            else if (macdVal < MacdLowerThreshold)
                macdSignal = -1;

            // --- RSI: level-based ---
            int rsiSignal = 0;
            double rsiVal = rsi[0];
            if (rsiVal < RsiLongTrigger)
                rsiSignal = 1;
            else if (rsiVal > RsiShortTrigger)
                rsiSignal = -1;

            // --- Momentum: level-based ---
            int momentumSignal = 0;
            double momentumVal = CurrentBar >= MomentumPeriod ? Close[0] - Close[MomentumPeriod] : 0;
            if (momentumVal > MomentumUpperThreshold)
                momentumSignal = 1;
            else if (momentumVal < MomentumLowerThreshold)
                momentumSignal = -1;

            if (Position.MarketPosition != MarketPosition.Flat)
                return;

            List<int> enabled = new List<int>();
            if (UseSma) enabled.Add(smaSignal);
            if (UseStochastic) enabled.Add(stochSignal);
            if (UseMacd) enabled.Add(macdSignal);
            if (UseRsi) enabled.Add(rsiSignal);
            if (UseMomentum) enabled.Add(momentumSignal);

            if (enabled.Count == 0)
                return;

            int direction = 0;
            if (RequireAllEnabledToAgree)
            {
                bool allLong = enabled.TrueForAll(s => s == 1);
                bool allShort = enabled.TrueForAll(s => s == -1);
                direction = allLong ? 1 : (allShort ? -1 : 0);
            }
            else
            {
                bool anyLong = enabled.Contains(1);
                bool anyShort = enabled.Contains(-1);
                direction = (anyLong && !anyShort) ? 1 : ((anyShort && !anyLong) ? -1 : 0);
            }

            if (direction == 0)
                return;

            // AllowLongs/AllowShorts gate the RAW signal (before reverse) -- e.g. AllowLongs=On,
            // AllowShorts=Off, ReverseAllSignals=On means: only act on raw Long signals, then
            // flip them into actual Short orders, so every trade placed is a Short. Gating the
            // post-reverse direction instead (the earlier bug) would block exactly the trades
            // this combination is meant to produce.
            if (direction == 1 && !AllowLongs)
                return;
            if (direction == -1 && !AllowShorts)
                return;

            if (ReverseAllSignals)
                direction = -direction;

            if (pendingEntryActive)
            {
                if (direction == pendingDirectionSign)
                    return; // same-direction signal while already waiting -- keep the existing order

                // opposite-direction signal invalidates the resting order -- cancel and replace
                if (pendingOrder != null)
                    CancelOrder(pendingOrder);
                pendingEntryActive = false;
                pendingOrder = null;
            }

            double anchor = Close[0];
            if (direction == 1)
            {
                double limitPrice = anchor - LongEntranceOffset;
                EnterLongLimit(limitPrice, "CompositeLong");
            }
            else
            {
                double limitPrice = anchor + ShortEntranceOffset;
                EnterShortLimit(limitPrice, "CompositeShort");
            }
            pendingEntryActive = true;
            pendingDirectionSign = direction;
        }

        protected override void OnOrderUpdate(Order order, double limitPrice, double stopPrice, int quantity, int filled, double averageFillPrice, OrderState orderState, DateTime time, ErrorCode error, string comment)
        {
            if (order.Name != "CompositeLong" && order.Name != "CompositeShort")
                return;

            if (orderState == OrderState.Working || orderState == OrderState.Accepted)
            {
                pendingOrder = order;
            }
            else if (orderState == OrderState.Filled)
            {
                pendingEntryActive = false;
                pendingOrder = null;
            }
            else if (orderState == OrderState.Cancelled || orderState == OrderState.Rejected)
            {
                if (pendingOrder != null && pendingOrder == order)
                {
                    pendingEntryActive = false;
                    pendingOrder = null;
                }
            }
        }

        protected override void OnExecutionUpdate(Execution execution, string executionId, double price, int quantity, MarketPosition marketPosition, string orderId, DateTime time)
        {
            if (execution.Order == null)
                return;

            string signal = execution.Order.Name;

            if (signal == "CompositeLong" && execution.Order.OrderAction == OrderAction.Buy)
            {
                tradeEntryBar = CurrentBar;
                tradeEntryPrice = price;
                tradeDirection = "Long";
                pendingEntryActive = false;
                pendingOrder = null;
                Draw.ArrowUp(this, "entryUp" + (drawCounter++), false, 0, Low[0] - 4 * TickSize, Brushes.Green);
            }
            else if (signal == "CompositeShort" && execution.Order.OrderAction == OrderAction.SellShort)
            {
                tradeEntryBar = CurrentBar;
                tradeEntryPrice = price;
                tradeDirection = "Short";
                pendingEntryActive = false;
                pendingOrder = null;
                Draw.ArrowDown(this, "entryDown" + (drawCounter++), false, 0, High[0] + 4 * TickSize, Brushes.Red);
            }
            else if (tradeEntryBar >= 0 && (execution.Order.OrderAction == OrderAction.Sell || execution.Order.OrderAction == OrderAction.BuyToCover))
            {
                double pnl = tradeDirection == "Long" ? price - tradeEntryPrice : tradeEntryPrice - price;
                Brush lineBrush = pnl >= 0 ? Brushes.LimeGreen : Brushes.OrangeRed;
                int barsAgoEntry = CurrentBar - tradeEntryBar;
                Draw.Line(this, "tradeLine" + (drawCounter++), false, barsAgoEntry, tradeEntryPrice, 0, price, lineBrush, DashStyleHelper.Dash, 2);
                Draw.Dot(this, "exitDot" + (drawCounter++), false, 0, price, Brushes.Gray);
                tradeEntryBar = -1;
            }
        }

        #region Properties
        [NinjaScriptProperty]
        [Display(Name = "Reverse All Signals", GroupName = "0 Global", Order = 1)]
        public bool ReverseAllSignals { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Require All Enabled To Agree (off = any one can trigger)", GroupName = "0 Global", Order = 2)]
        public bool RequireAllEnabledToAgree { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Allow Longs", GroupName = "0 Global", Order = 3)]
        public bool AllowLongs { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Allow Shorts", GroupName = "0 Global", Order = 4)]
        public bool AllowShorts { get; set; }

        [NinjaScriptProperty]
        [Range(-100000, 100000)]
        [Display(Name = "Long Entrance Offset (points, limit below trigger price; negative = above)", GroupName = "0 Global", Order = 5)]
        public double LongEntranceOffset { get; set; }

        [NinjaScriptProperty]
        [Range(-100000, 100000)]
        [Display(Name = "Short Entrance Offset (points, limit above trigger price; negative = below)", GroupName = "0 Global", Order = 6)]
        public double ShortEntranceOffset { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use SMA", GroupName = "1 SMA", Order = 1)]
        public bool UseSma { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "SMA Period", GroupName = "1 SMA", Order = 2)]
        public int SmaPeriod { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use Stochastic", GroupName = "2 Stochastic", Order = 1)]
        public bool UseStochastic { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Stochastic %K Period", GroupName = "2 Stochastic", Order = 2)]
        public int StochKPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Stochastic %K Smoothing", GroupName = "2 Stochastic", Order = 3)]
        public int StochKSmooth { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Stochastic %D Period", GroupName = "2 Stochastic", Order = 4)]
        public int StochDPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(1, 99)]
        [Display(Name = "Stochastic Overbought (Short trigger)", GroupName = "2 Stochastic", Order = 5)]
        public double StochOverbought { get; set; }

        [NinjaScriptProperty]
        [Range(1, 99)]
        [Display(Name = "Stochastic Oversold (Long trigger)", GroupName = "2 Stochastic", Order = 6)]
        public double StochOversold { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use MACD", GroupName = "3 MACD", Order = 1)]
        public bool UseMacd { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "MACD Fast", GroupName = "3 MACD", Order = 2)]
        public int MacdFast { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "MACD Slow", GroupName = "3 MACD", Order = 3)]
        public int MacdSlow { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "MACD Signal", GroupName = "3 MACD", Order = 4)]
        public int MacdSignal { get; set; }

        [NinjaScriptProperty]
        [Range(-100000, 100000)]
        [Display(Name = "MACD Upper Threshold (Long trigger)", GroupName = "3 MACD", Order = 5)]
        public double MacdUpperThreshold { get; set; }

        [NinjaScriptProperty]
        [Range(-100000, 100000)]
        [Display(Name = "MACD Lower Threshold (Short trigger)", GroupName = "3 MACD", Order = 6)]
        public double MacdLowerThreshold { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use RSI", GroupName = "4 RSI", Order = 1)]
        public bool UseRsi { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "RSI Period", GroupName = "4 RSI", Order = 2)]
        public int RsiPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "RSI Smoothing (1 = pure Wilder RSI)", GroupName = "4 RSI", Order = 3)]
        public int RsiSmooth { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "RSI Long Trigger (below)", GroupName = "4 RSI", Order = 4)]
        public double RsiLongTrigger { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "RSI Short Trigger (above)", GroupName = "4 RSI", Order = 5)]
        public double RsiShortTrigger { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use Momentum", GroupName = "5 Momentum", Order = 1)]
        public bool UseMomentum { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Momentum Period", GroupName = "5 Momentum", Order = 2)]
        public int MomentumPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(-100000, 100000)]
        [Display(Name = "Momentum Upper Threshold (Long trigger)", GroupName = "5 Momentum", Order = 3)]
        public double MomentumUpperThreshold { get; set; }

        [NinjaScriptProperty]
        [Range(-100000, 100000)]
        [Display(Name = "Momentum Lower Threshold (Short trigger)", GroupName = "5 Momentum", Order = 4)]
        public double MomentumLowerThreshold { get; set; }

        [NinjaScriptProperty]
        [Range(0.25, double.MaxValue)]
        [Display(Name = "Profit Target (points)", GroupName = "6 Risk", Order = 1)]
        public double ProfitTargetPoints { get; set; }

        [NinjaScriptProperty]
        [Range(0.25, double.MaxValue)]
        [Display(Name = "Stop Loss (points)", GroupName = "6 Risk", Order = 2)]
        public double StopLossPoints { get; set; }
        #endregion
    }
}
