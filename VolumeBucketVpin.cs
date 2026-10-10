#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
#endregion

// BVC (Bulk Volume Classification) VPIN, meant to be applied to a chart whose Data Series is
// already set to NT8's native "Volume" bar type -- same methodology validated in Python in
// this project's vpin_event_study.py, simplified to run per-bar instead of per-tick.
//
// Rebuilt from an earlier tick-level version once it became clear the bucketing itself doesn't
// need custom code: NT8's own Volume bars ALREADY give real OHLCV bars sized by volume (Data
// Series -> Volume, set your bucket size there) -- and any indicator or strategy just works on
// whatever bars it's applied to, no special handling needed. So the only piece actually worth
// building here is the VPIN/BVC computation Volume bars don't give you on their own. Each bar
// IS one bucket now, so this is just: priceDiff = Close[0]-Close[1], buyFrac =
// NormalCdf(priceDiff / sigma) where sigma is the std dev of the last VpinWindowBuckets price
// diffs, buyVolume/sellVolume split from that bar's own Volume[0], VPIN = sum(|imbalance|) /
// sum(volume) over the rolling window. No secondary tick series, no "which bucket is this"
// ambiguity, no coarser-axis visibility caveat -- all of that only existed because the earlier
// version assumed a time-based chart.
//
// This is still a display Indicator, not a Strategy -- the natural next step, when/if a rule
// gets built on this, is a Strategy applied to the same Volume-bar chart that recomputes this
// same math internally (same pattern StochExtremesStrategy.cs already uses for its own
// indicators) so it can act on it, not just show it.
//
// A third plot, "VPIN (hard-sign)", is a live NT8 side-by-side of a Python ablation test run
// on the same validated NQ data (vpin_hardsign_test.py): same VPIN formula, but the
// classification step is a trivial hard rule (100% buy if price rose this bucket, 100% sell if
// it fell) instead of BVC's magnitude-weighted NormalCdf. In Python this came out clearly
// weaker and inconsistent across periods (significant on 1 of 3, not the other 2) -- and for a
// concrete, structural reason worth knowing before reading this plot: hard-sign classification
// ignores the SIZE of a bucket's price move, only its direction, so almost every bucket
// (barring an exact tie) saturates to maximum imbalance regardless of whether the move was
// tiny or huge. Expect this line to look far choppier and pinned near its extremes compared to
// the smoother BVC line, not a data bug -- it's the mechanism itself being cruder.

namespace NinjaTrader.NinjaScript.Indicators
{
    public class VolumeBucketVpin : Indicator
    {
        private List<double> recentPriceDiffs;
        private List<double> recentImbalances;
        private List<double> recentImbalancesHardSign;
        private List<double> recentVolumes;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "BVC VPIN for a Volume-bar chart (each bar = one bucket) -- set your bucket size via the chart's own Data Series, not here.";
                Name = "VolumeBucketVpin";
                IsOverlay = false;

                VpinWindowBuckets = 50;

                AddPlot(Brushes.Goldenrod, "Bar Duration (sec)");
                AddPlot(Brushes.MediumOrchid, "VPIN (BVC)");
                AddPlot(Brushes.Gray, "VPIN (hard-sign)");
            }
            else if (State == State.DataLoaded)
            {
                recentPriceDiffs = new List<double>();
                recentImbalances = new List<double>();
                recentImbalancesHardSign = new List<double>();
                recentVolumes = new List<double>();
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < 1)
                return;

            double durationSeconds = (Time[0] - Time[1]).TotalSeconds;
            Values[0][0] = durationSeconds;

            double priceDiff = Close[0] - Close[1];
            double barVolume = Volume[0];

            recentPriceDiffs.Add(priceDiff);
            if (recentPriceDiffs.Count > VpinWindowBuckets)
                recentPriceDiffs.RemoveAt(0);

            double buyFrac = 0.5;
            if (recentPriceDiffs.Count >= 2)
            {
                double sigma = StdDev(recentPriceDiffs);
                buyFrac = sigma > 0 ? NormalCdf(priceDiff / sigma) : 0.5;
            }

            double buyVolume = barVolume * buyFrac;
            double sellVolume = barVolume * (1.0 - buyFrac);
            double imbalance = Math.Abs(buyVolume - sellVolume);

            // hard-sign ablation: same imbalance formula, but buyFrac only ever takes the
            // magnitude-blind values 0 / 0.5 / 1 instead of BVC's continuous NormalCdf output
            double buyFracHardSign = priceDiff > 0 ? 1.0 : (priceDiff < 0 ? 0.0 : 0.5);
            double imbalanceHardSign = Math.Abs(barVolume * (2 * buyFracHardSign - 1));

            recentImbalances.Add(imbalance);
            recentImbalancesHardSign.Add(imbalanceHardSign);
            recentVolumes.Add(barVolume);
            if (recentImbalances.Count > VpinWindowBuckets)
            {
                recentImbalances.RemoveAt(0);
                recentImbalancesHardSign.RemoveAt(0);
                recentVolumes.RemoveAt(0);
            }

            double imbalanceSum = 0;
            foreach (double v in recentImbalances) imbalanceSum += v;
            double imbalanceSumHardSign = 0;
            foreach (double v in recentImbalancesHardSign) imbalanceSumHardSign += v;
            double volumeSum = 0;
            foreach (double v in recentVolumes) volumeSum += v;

            Values[1][0] = volumeSum > 0 ? imbalanceSum / volumeSum : 0;
            Values[2][0] = volumeSum > 0 ? imbalanceSumHardSign / volumeSum : 0;
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

        // Zelen & Severo (1964) rational approximation of the standard normal CDF -- accurate
        // to ~7.5e-8, no external math library dependency needed.
        private static double NormalCdf(double x)
        {
            double t = 1.0 / (1.0 + 0.2316419 * Math.Abs(x));
            double d = 0.3989422804014327 * Math.Exp(-x * x / 2.0);
            double prob = d * t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
            return x >= 0 ? 1.0 - prob : prob;
        }

        #region Properties
        [NinjaScriptProperty]
        [Range(2, int.MaxValue)]
        [Display(Name = "VPIN Window (buckets)", GroupName = "Parameters", Order = 1)]
        public int VpinWindowBuckets { get; set; }
        #endregion
    }
}
