// AyniOrderFlowStrategy - NinjaTrader 8 strategy (NinjaScript, C#)
//
// What it does: every SampleSeconds it turns live order flow (delta, order-flow imbalance,
// book pressure, microprice, momentum) into z-scores, combines them with a logistic model into
// P(up), and trades when P(up) is far enough from 50%. Everything it "thinks" is drawn on the chart.
//
// Install: NinjaTrader 8 > New > NinjaScript Editor > Strategies > right-click > New Strategy,
// paste this file's contents over the generated file (or import the .cs), press F5 to compile.
//
// IMPORTANT
//  - Needs tick-level bid/ask data: live feed or Market Replay data. A plain historical
//    backtest has no bid/ask, so only delta (tick-rule) and momentum work there.
//  - The default weights are STARTING PLACEHOLDERS, not trained values. Fit them offline
//    (logistic regression on your NQ tick data) and paste them into the parameters.
//  - SignalsOnly = true (default): draws signals, places NO orders. Turn it off only on Sim101.
//  - Compiles against the NT8 DLLs (tools/nt_compile.ps1). Not yet run on a live/replay chart.

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class AyniOrderFlowStrategy : Strategy
    {
        // Running mean/variance (EMA) so every feature is comparable as a z-score.
        private class RunningZ
        {
            private double mean, var;
            private int n;
            private readonly double alpha;
            public RunningZ(int window) { alpha = 2.0 / (window + 1); }
            public double Update(double x)
            {
                n++;
                if (n == 1) { mean = x; var = 0; return 0; }
                double d = x - mean;
                mean += alpha * d;
                var = (1 - alpha) * (var + alpha * d * d);
                if (n < 50 || var <= 1e-12) return 0;   // warm-up
                double z = (x - mean) / Math.Sqrt(var);
                return Math.Max(-4, Math.Min(4, z));
            }
        }

        // Accumulators for the current sample window
        private double winDelta, winOfi;
        private double bidP, bidQ, askP, askQ, prevBidP, prevBidQ, prevAskP, prevAskQ;
        private bool haveBook;
        private double lastClose;
        private DateTime nextSample = DateTime.MinValue;
        private DateTime lastTickTime = DateTime.MinValue;
        private readonly Queue<double> midHistory = new Queue<double>();

        private RunningZ zDelta, zOfi, zPress, zMicro, zMom;

        // Last decision, kept for drawing
        private double lastP = 0.5, lzDelta, lzOfi, lzPress, lzMicro, lzMom, lastScore;
        private string lastState = "Warming up";
        private Brush tint = Brushes.Transparent;

        // Risk bookkeeping
        private DateTime currentDay = DateTime.MinValue;
        private int tradesToday;
        private double dayStartProfit;
        private bool halted;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Order-flow logistic model with on-chart diagnostics";
                Name = "AyniOrderFlowStrategy";
                Calculate = Calculate.OnEachTick;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                IsOverlay = true;
                BarsRequiredToTrade = 1;
                IsUnmanaged = false;

                SignalsOnly = true;
                Quantity = 1;
                SampleSeconds = 5;
                MomentumSamples = 12;
                NormWindow = 300;

                WOfi = 0.8; WDelta = 0.6; WPress = 0.4; WMicro = 0.4; WMom = 0.3; Bias = 0.0;
                EntryThreshold = 0.60;

                StopTicks = 8;
                TargetTicks = 12;
                MaxTradesPerDay = 10;
                DailyLossLimit = 500;
                StartTime = 93500;   // HHmmss, chart time zone
                EndTime = 155500;
            }
            else if (State == State.Configure)
            {
                zDelta = new RunningZ(NormWindow);
                zOfi = new RunningZ(NormWindow);
                zPress = new RunningZ(NormWindow);
                zMicro = new RunningZ(NormWindow);
                zMom = new RunningZ(NormWindow);
                if (StopTicks > 0) SetStopLoss(CalculationMode.Ticks, StopTicks);
                if (TargetTicks > 0) SetProfitTarget(CalculationMode.Ticks, TargetTicks);
            }
            else if (State == State.Realtime)
            {
                // History only had tick-rule delta + momentum (pressure/microprice were always 0,
                // so their variance is 0). Carrying those stats into live data makes the first
                // real book reading a +/-4 z-score and fires a bogus signal. Re-warm on live data.
                zDelta = new RunningZ(NormWindow);
                zOfi = new RunningZ(NormWindow);
                zPress = new RunningZ(NormWindow);
                zMicro = new RunningZ(NormWindow);
                zMom = new RunningZ(NormWindow);
                midHistory.Clear();
                winDelta = 0; winOfi = 0;
                nextSample = DateTime.MinValue;
                lastP = 0.5; lastScore = 0;
                lzDelta = lzOfi = lzPress = lzMicro = lzMom = 0;
                lastState = "Warming up (live)";
                tint = Brushes.Transparent;
                Print("AyniOrderFlow: realtime. SignalsOnly=" + SignalsOnly);
            }
        }

        // ---- Raw order-flow inputs ----------------------------------------------------------
        protected override void OnMarketData(MarketDataEventArgs e)
        {
            if (e.MarketDataType == MarketDataType.Bid || e.MarketDataType == MarketDataType.Ask)
            {
                if (e.MarketDataType == MarketDataType.Bid) { bidP = e.Price; bidQ = e.Volume; }
                else { askP = e.Price; askQ = e.Volume; }

                if (haveBook && bidP > 0 && askP > 0)
                {
                    // Cont-Kukanov-Stoikov order flow imbalance at the top of the book
                    double eBid = (bidP >= prevBidP ? bidQ : 0) - (bidP <= prevBidP ? prevBidQ : 0);
                    double eAsk = (askP <= prevAskP ? askQ : 0) - (askP >= prevAskP ? prevAskQ : 0);
                    winOfi += eBid - eAsk;
                }
                if (bidP > 0 && askP > 0)
                {
                    prevBidP = bidP; prevBidQ = bidQ; prevAskP = askP; prevAskQ = askQ;
                    haveBook = true;
                }
            }
            else if (e.MarketDataType == MarketDataType.Last)
            {
                if (haveBook)
                {
                    if (e.Price >= askP) winDelta += e.Volume;        // buyer aggressed
                    else if (e.Price <= bidP) winDelta -= e.Volume;   // seller aggressed
                }
                else
                {
                    // No bid/ask available: tick rule fallback
                    if (lastClose > 0)
                    {
                        if (e.Price > lastClose) winDelta += e.Volume;
                        else if (e.Price < lastClose) winDelta -= e.Volume;
                    }
                }
                lastClose = e.Price;
                lastTickTime = e.Time;
            }
        }

        // ---- Decision loop -----------------------------------------------------------------
        protected override void OnBarUpdate()
        {
            if (CurrentBar < BarsRequiredToTrade) return;

            // Tick-rule delta when running on historical data without OnMarketData
            if (State == State.Historical && !haveBook)
            {
                if (lastClose > 0)
                {
                    if (Close[0] > lastClose) winDelta += Volume[0];
                    else if (Close[0] < lastClose) winDelta -= Volume[0];
                }
                lastClose = Close[0];
            }

            ResetDayIfNeeded();
            BackBrush = tint;

            // Time[0] is the bar's timestamp and is constant within a bar, so live sampling
            // must use the tick clock or SampleSeconds collapses to one sample per bar.
            DateTime now = (State == State.Realtime && lastTickTime > DateTime.MinValue) ? lastTickTime : Time[0];
            if (nextSample == DateTime.MinValue) nextSample = now.AddSeconds(SampleSeconds);
            if (now >= nextSample)
            {
                nextSample = now.AddSeconds(SampleSeconds);
                RunModel();
            }
            DrawPanel();
        }

        private void RunModel()
        {
            double mid = haveBook ? (bidP + askP) / 2.0 : Close[0];

            double press = 0, micro = 0;
            if (haveBook && (bidQ + askQ) > 0)
            {
                press = (bidQ - askQ) / (bidQ + askQ);
                micro = ((askP * bidQ + bidP * askQ) / (bidQ + askQ) - mid) / TickSize;
            }

            midHistory.Enqueue(mid);
            double mom = 0;
            if (midHistory.Count > MomentumSamples)
            {
                double old = midHistory.Dequeue();
                mom = (mid - old) / TickSize;
            }

            lzDelta = zDelta.Update(winDelta);
            lzOfi = zOfi.Update(winOfi);
            lzPress = zPress.Update(press);
            lzMicro = zMicro.Update(micro);
            lzMom = zMom.Update(mom);
            winDelta = 0; winOfi = 0;

            lastScore = Bias + WOfi * lzOfi + WDelta * lzDelta + WPress * lzPress + WMicro * lzMicro + WMom * lzMom;
            lastP = 1.0 / (1.0 + Math.Exp(-lastScore));

            byte a = (byte)Math.Min(90, Math.Abs(lastP - 0.5) * 360);
            Brush b = lastP >= 0.5 ? new SolidColorBrush(Color.FromArgb(a, 0, 200, 0))
                                   : new SolidColorBrush(Color.FromArgb(a, 220, 0, 0));
            b.Freeze();
            tint = b;

            bool inSession = InSession();
            bool canTrade = inSession && !halted && tradesToday < MaxTradesPerDay;
            bool flat = Position.MarketPosition == MarketPosition.Flat;

            if (!inSession) lastState = "Outside trading hours";
            else if (halted) lastState = "HALTED: daily loss limit";
            else if (tradesToday >= MaxTradesPerDay) lastState = "Max trades reached";
            else if (lastP >= EntryThreshold) lastState = "LONG signal";
            else if (lastP <= 1 - EntryThreshold) lastState = "SHORT signal";
            else lastState = "No edge: staying out";

            if (canTrade && flat && (lastP >= EntryThreshold || lastP <= 1 - EntryThreshold))
            {
                bool isLong = lastP >= EntryThreshold;
                tradesToday++;
                MarkSignal(isLong);
                if (!SignalsOnly)
                {
                    if (isLong) EnterLong(Quantity, "AyniLong");
                    else EnterShort(Quantity, "AyniShort");
                }
            }

            // Flatten at end of session / on halt
            if (!SignalsOnly && (!inSession || halted))
            {
                if (Position.MarketPosition == MarketPosition.Long) ExitLong();
                if (Position.MarketPosition == MarketPosition.Short) ExitShort();
            }

            CheckDailyLoss();

            Print(string.Format("{0:HH:mm:ss} P={1:0.00} score={2:0.00} ofi={3:0.0} delta={4:0.0} press={5:0.0} micro={6:0.0} mom={7:0.0} -> {8}",
                Time[0], lastP, lastScore, lzOfi, lzDelta, lzPress, lzMicro, lzMom, lastState));
        }

        // ---- Chart output ------------------------------------------------------------------
        private void MarkSignal(bool isLong)
        {
            string tag = "sig" + CurrentBar + "_" + tradesToday;
            string label = string.Format("{0} P={1:0.00}\nofi {2:+0.0;-0.0} dlt {3:+0.0;-0.0}{4}",
                isLong ? "BUY" : "SELL", lastP, lzOfi, lzDelta, SignalsOnly ? "\n(signal only)" : "");
            Brush color = SignalsOnly ? Brushes.Cyan : (isLong ? Brushes.Lime : Brushes.Red);
            if (isLong)
            {
                Draw.ArrowUp(this, tag, false, 0, Low[0] - 2 * TickSize, color);
                Draw.Text(this, tag + "t", false, label, 0, Low[0] - 6 * TickSize, 0, color,
                    new NinjaTrader.Gui.Tools.SimpleFont("Arial", 10), System.Windows.TextAlignment.Center,
                    Brushes.Transparent, Brushes.Transparent, 0);
            }
            else
            {
                Draw.ArrowDown(this, tag, false, 0, High[0] + 2 * TickSize, color);
                Draw.Text(this, tag + "t", false, label, 0, High[0] + 6 * TickSize, 0, color,
                    new NinjaTrader.Gui.Tools.SimpleFont("Arial", 10), System.Windows.TextAlignment.Center,
                    Brushes.Transparent, Brushes.Transparent, 0);
            }
        }

        private void DrawPanel()
        {
            double dayPnl = SystemPerformance.AllTrades.TradesPerformance.Currency.CumProfit - dayStartProfit;
            string txt = string.Format(
                "AYNI ORDER FLOW  [{0}]\n" +
                "P(up): {1:0.00}   score: {2:+0.00;-0.00}   need >= {3:0.00} / <= {4:0.00}\n" +
                "OFI z: {5:+0.0;-0.0}   Delta z: {6:+0.0;-0.0}   Pressure z: {7:+0.0;-0.0}\n" +
                "Microprice z: {8:+0.0;-0.0}   Momentum z: {9:+0.0;-0.0}\n" +
                "Decision: {10}\n" +
                "Position: {11}   Trades today: {12}/{13}   Day P&L: {14:0.00}",
                SignalsOnly ? "SIGNALS ONLY" : "ORDERS ON",
                lastP, lastScore, EntryThreshold, 1 - EntryThreshold,
                lzOfi, lzDelta, lzPress, lzMicro, lzMom,
                lastState,
                Position.MarketPosition, tradesToday, MaxTradesPerDay, dayPnl);
            Draw.TextFixed(this, "aynipanel", txt, TextPosition.TopLeft,
                lastP >= EntryThreshold ? Brushes.Lime : (lastP <= 1 - EntryThreshold ? Brushes.Red : Brushes.White),
                new NinjaTrader.Gui.Tools.SimpleFont("Consolas", 12),
                Brushes.Transparent, Brushes.Black, 70);
        }

        // ---- Risk helpers ------------------------------------------------------------------
        private bool InSession()
        {
            int t = ToTime(Time[0]);
            return t >= StartTime && t < EndTime;
        }

        private void ResetDayIfNeeded()
        {
            if (Time[0].Date == currentDay) return;
            currentDay = Time[0].Date;
            tradesToday = 0;
            halted = false;
            dayStartProfit = SystemPerformance.AllTrades.TradesPerformance.Currency.CumProfit;
        }

        private void CheckDailyLoss()
        {
            double dayPnl = SystemPerformance.AllTrades.TradesPerformance.Currency.CumProfit - dayStartProfit;
            if (dayPnl <= -DailyLossLimit) halted = true;
        }

        // ---- Parameters --------------------------------------------------------------------
        #region Properties
        [NinjaScriptProperty][Display(Name = "Signals only (no orders)", GroupName = "1. Safety", Order = 1)]
        public bool SignalsOnly { get; set; }
        [NinjaScriptProperty][Range(1, 100)][Display(Name = "Quantity", GroupName = "1. Safety", Order = 2)]
        public int Quantity { get; set; }
        [NinjaScriptProperty][Range(1, 100000)][Display(Name = "Daily loss limit ($)", GroupName = "1. Safety", Order = 3)]
        public double DailyLossLimit { get; set; }
        [NinjaScriptProperty][Range(1, 1000)][Display(Name = "Max trades per day", GroupName = "1. Safety", Order = 4)]
        public int MaxTradesPerDay { get; set; }
        [NinjaScriptProperty][Display(Name = "Start time (HHmmss)", GroupName = "1. Safety", Order = 5)]
        public int StartTime { get; set; }
        [NinjaScriptProperty][Display(Name = "End time (HHmmss)", GroupName = "1. Safety", Order = 6)]
        public int EndTime { get; set; }

        [NinjaScriptProperty][Range(1, 600)][Display(Name = "Sample seconds", GroupName = "2. Model", Order = 1)]
        public int SampleSeconds { get; set; }
        [NinjaScriptProperty][Range(1, 1000)][Display(Name = "Momentum samples", GroupName = "2. Model", Order = 2)]
        public int MomentumSamples { get; set; }
        [NinjaScriptProperty][Range(20, 100000)][Display(Name = "Normalization window", GroupName = "2. Model", Order = 3)]
        public int NormWindow { get; set; }
        [NinjaScriptProperty][Range(0.5, 0.99)][Display(Name = "Entry threshold P", GroupName = "2. Model", Order = 4)]
        public double EntryThreshold { get; set; }

        [NinjaScriptProperty][Display(Name = "Weight: OFI", GroupName = "3. Weights", Order = 1)]
        public double WOfi { get; set; }
        [NinjaScriptProperty][Display(Name = "Weight: Delta", GroupName = "3. Weights", Order = 2)]
        public double WDelta { get; set; }
        [NinjaScriptProperty][Display(Name = "Weight: Pressure", GroupName = "3. Weights", Order = 3)]
        public double WPress { get; set; }
        [NinjaScriptProperty][Display(Name = "Weight: Microprice", GroupName = "3. Weights", Order = 4)]
        public double WMicro { get; set; }
        [NinjaScriptProperty][Display(Name = "Weight: Momentum", GroupName = "3. Weights", Order = 5)]
        public double WMom { get; set; }
        [NinjaScriptProperty][Display(Name = "Bias", GroupName = "3. Weights", Order = 6)]
        public double Bias { get; set; }

        [NinjaScriptProperty][Range(0, 1000)][Display(Name = "Stop (ticks, 0 = none)", GroupName = "4. Exits", Order = 1)]
        public int StopTicks { get; set; }
        [NinjaScriptProperty][Range(0, 1000)][Display(Name = "Target (ticks, 0 = none)", GroupName = "4. Exits", Order = 2)]
        public int TargetTicks { get; set; }
        #endregion
    }
}
