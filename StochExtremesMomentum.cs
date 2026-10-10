#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
#endregion

// Chart-only visual copy of StochExtremesStrategy.cs's internal Momentum math (simple point
// difference, Close[0] - Close[MomentumPeriod] -- not NT8's built-in Momentum formula, which
// is percentage-based and would show different values than what the strategy actually trades
// on). Exists purely so Momentum shows on the chart via AddChartIndicator. Keep in sync with
// StochExtremesStrategy.cs's OnBarUpdate if that formula ever changes.

namespace NinjaTrader.NinjaScript.Indicators
{
    public class StochExtremesMomentum : Indicator
    {
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Chart display for StochExtremesStrategy's internal Momentum (point difference, not standalone).";
                Name = "StochExtremesMomentum";
                IsOverlay = false;
                MomentumPeriod = 10;

                AddPlot(Brushes.MediumPurple, "Momentum");
            }
        }

        protected override void OnBarUpdate()
        {
            Values[0][0] = CurrentBar >= MomentumPeriod ? Close[0] - Close[MomentumPeriod] : 0;
        }

        #region Properties
        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Momentum Period", GroupName = "Parameters", Order = 1)]
        public int MomentumPeriod { get; set; }
        #endregion
    }
}
