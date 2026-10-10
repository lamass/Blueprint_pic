#region Using declarations
using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Media;
using SharpDX.Direct2D1;
using SharpDX.DirectWrite;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using Brush = System.Windows.Media.Brush;
using Brushes = System.Windows.Media.Brushes;
using Font = NinjaTrader.Gui.Tools.SimpleFont;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    /// <summary>
    /// Live footprint: bid vol / ask vol per price level per bar, plus a heat
    /// overlay for tick intensity (prints/second) at that level.
    /// Purely a visualization -- does not touch the DB (kept low-latency).
    /// </summary>
    public class OrderFlowFootprint : Indicator
    {
        // price -> (bidVol, askVol, tickCount, firstTickTime, lastTickTime)
        private Dictionary<int, LevelStats> currentBar;
        private readonly Dictionary<int, Dictionary<int, LevelStats>> barHistory
            = new Dictionary<int, Dictionary<int, LevelStats>>();

        private double lastBid, lastAsk;
        private int currentBarIdx = -1;

        // NOTE: [NinjaScriptProperty]/[Display] removed -- they require adding
        // System.ComponentModel.DataAnnotations.dll under Tools > References in
        // NinjaScript Editor. If you want these editable in the Properties dialog,
        // add that reference and restore the attributes. For now they're just
        // hardcoded defaults below.
        public int MaxBarsToDraw { get; set; } = 150;
        public int RowHeightTicks { get; set; } = 1;

        private class LevelStats
        {
            public long BidVol;
            public long AskVol;
            public int TickCount;
            public DateTime FirstTick;
            public DateTime LastTick;
        }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Bid/ask volume footprint with tick-intensity heat, per price level per bar.";
                Name = "OrderFlowFootprint";
                Calculate = Calculate.OnEachTick;
                IsOverlay = true;
                DisplayInDataBox = false;
                PaintPriceMarkers = false;
                MaxBarsToDraw = 150;
                RowHeightTicks = 1;
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar != currentBarIdx)
            {
                // roll to new bar
                if (currentBar != null)
                    barHistory[currentBarIdx] = currentBar;

                currentBarIdx = CurrentBar;
                currentBar = new Dictionary<int, LevelStats>();

                // trim history so we don't leak memory on long sessions
                if (barHistory.Count > MaxBarsToDraw * 2)
                {
                    int cutoff = currentBarIdx - MaxBarsToDraw * 2;
                    foreach (var key in barHistory.Keys.Where(k => k < cutoff).ToList())
                        barHistory.Remove(key);
                }
            }
        }

        protected override void OnMarketData(MarketDataEventArgs e)
        {
            if (e.MarketDataType == MarketDataType.Bid) { lastBid = e.Price; return; }
            if (e.MarketDataType == MarketDataType.Ask) { lastAsk = e.Price; return; }
            if (e.MarketDataType != MarketDataType.Last) return;
            if (currentBar == null) return;

            int levelKey = ToTicks(e.Price);
            if (!currentBar.TryGetValue(levelKey, out var stats))
            {
                stats = new LevelStats { FirstTick = e.Time };
                currentBar[levelKey] = stats;
            }

            bool isBuy = lastAsk > 0 && e.Price >= lastAsk;
            bool isSell = lastBid > 0 && e.Price <= lastBid;
            if (isBuy) stats.AskVol += e.Volume;
            else if (isSell) stats.BidVol += e.Volume;
            else if (e.Price >= (lastBid + lastAsk) / 2.0) stats.AskVol += e.Volume;
            else stats.BidVol += e.Volume;

            stats.TickCount++;
            stats.LastTick = e.Time;
        }

        private int ToTicks(double price) => (int)Math.Round(price / TickSize);

        protected override void OnRender(ChartControl chartControl, ChartScale chartScale)
        {
            base.OnRender(chartControl, chartScale);
            if (ChartBars == null) return;

            int firstBar = Math.Max(0, ChartBars.FromIndex - 1);
            int lastBar = Math.Min(CurrentBar, ChartBars.ToIndex + 1);

            for (int barIdx = firstBar; barIdx <= lastBar; barIdx++)
            {
                var levels = barIdx == currentBarIdx ? currentBar
                    : (barHistory.TryGetValue(barIdx, out var h) ? h : null);
                if (levels == null || levels.Count == 0) continue;

                float x = chartControl.GetXByBarIndex(ChartBars, barIdx);
                float barWidth = Math.Max(6f, (float)chartControl.GetBarPaintWidth(ChartBars));

                long maxVolAtLevel = levels.Values.Select(v => Math.Max(v.BidVol, v.AskVol)).DefaultIfEmpty(1).Max();
                double maxIntensity = levels.Values
                    .Select(v => (v.LastTick - v.FirstTick).TotalSeconds > 0
                        ? v.TickCount / Math.Max(0.1, (v.LastTick - v.FirstTick).TotalSeconds)
                        : v.TickCount)
                    .DefaultIfEmpty(1).Max();

                foreach (var kv in levels)
                {
                    double price = kv.Key * TickSize;
                    float y = chartScale.GetYByValue(price);
                    float rowHeight = Math.Max(3f, (float)(chartScale.GetYByValue(price) - chartScale.GetYByValue(price + TickSize * RowHeightTicks)));

                    var stats = kv.Value;
                    float bidW = (float)(stats.BidVol / (double)maxVolAtLevel) * (barWidth / 2f);
                    float askW = (float)(stats.AskVol / (double)maxVolAtLevel) * (barWidth / 2f);

                    double secs = (stats.LastTick - stats.FirstTick).TotalSeconds;
                    double intensity = secs > 0 ? stats.TickCount / Math.Max(0.1, secs) : stats.TickCount;
                    double heat = Math.Min(1.0, intensity / Math.Max(1.0, maxIntensity));

                    // heat backdrop -- yellow->red as intensity rises
                    var heatColor = System.Windows.Media.Color.FromArgb(
                        (byte)(heat * 90), 255, (byte)(200 * (1 - heat)), 40);
                    var heatBrush = new System.Windows.Media.SolidColorBrush(heatColor);
                    RenderTarget.FillRectangle(
                        new SharpDX.RectangleF(x - barWidth / 2f, y - rowHeight / 2f, barWidth, rowHeight),
                        heatBrush.ToDxBrush(RenderTarget));

                    // bid volume (left, red) / ask volume (right, green)
                    RenderTarget.FillRectangle(
                        new SharpDX.RectangleF(x - bidW, y - rowHeight / 2f, bidW, rowHeight),
                        Brushes.OrangeRed.ToDxBrush(RenderTarget));
                    RenderTarget.FillRectangle(
                        new SharpDX.RectangleF(x, y - rowHeight / 2f, askW, rowHeight),
                        Brushes.LimeGreen.ToDxBrush(RenderTarget));
                }
            }
        }
    }
}
