#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
#endregion

// Chart-only visual copy of StochExtremesStrategy.cs's internal Stochastic %K/%D math (raw %K
// over KPeriod, smoothed over KSmooth, %D as a further shrinking-window average over DPeriod)
// -- exists purely so the strategy's Stochastic shows on the chart automatically via
// AddChartIndicator instead of needing to be added by hand every time. Keep this formula in
// sync with StochExtremesStrategy.cs's OnBarUpdate if that one ever changes -- this is a
// deliberate duplicate, not a shared reference, since NinjaScript strategies and indicators
// don't share code across files.

namespace NinjaTrader.NinjaScript.Indicators
{
    public class StochExtremesStoch : Indicator
    {
        private Series<double> rawK;
        private Series<double> smoothKSeries;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Chart display for StochExtremesStrategy's internal %K/%D (not a standalone trading indicator).";
                Name = "StochExtremesStoch";
                IsOverlay = false;
                KPeriod = 30;
                KSmooth = 3;
                DPeriod = 7;

                AddPlot(Brushes.DodgerBlue, "%K");
                AddPlot(Brushes.Orange, "%D");
            }
            else if (State == State.DataLoaded)
            {
                rawK = new Series<double>(this);
                smoothKSeries = new Series<double>(this);
            }
        }

        protected override void OnBarUpdate()
        {
            double hh = MAX(High, KPeriod)[0];
            double ll = MIN(Low, KPeriod)[0];
            rawK[0] = (hh - ll) == 0 ? 50.0 : 100.0 * (Close[0] - ll) / (hh - ll);

            int kAvail = Math.Min(KSmooth, CurrentBar + 1);
            double kSum = 0;
            for (int i = 0; i < kAvail; i++) kSum += rawK[i];
            smoothKSeries[0] = kSum / kAvail;

            int dAvail = Math.Min(DPeriod, CurrentBar + 1);
            double dSum = 0;
            for (int i = 0; i < dAvail; i++) dSum += smoothKSeries[i];

            Values[0][0] = smoothKSeries[0];
            Values[1][0] = dSum / dAvail;
        }

        #region Properties
        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "K Period", GroupName = "Parameters", Order = 1)]
        public int KPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "K Smoothing", GroupName = "Parameters", Order = 2)]
        public int KSmooth { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "D Period", GroupName = "Parameters", Order = 3)]
        public int DPeriod { get; set; }
        #endregion
    }
}
