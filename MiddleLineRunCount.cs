#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

// Companion to MiddleLineTracker.cs: same bunch/MiddleLine/signed-run-count math, duplicated
// here purely to plot the signed run count as its own series (own panel, IsOverlay = false).
// Split out from MiddleLineSpanCount.cs, which now plots only VerticalSpan -- see that file's
// header for why sharing one panel between Span (tens to hundreds of points) and this (small
// integers) made both hard to read.
//
// Add this alongside MiddleLineTracker.cs and MiddleLineSpanCount.cs for the full trend view;
// the two adjustable bucket-mode parameters must match across all three to track the same
// bunches. Keep this formula in sync with MiddleLineTracker.cs if that one's math ever changes.

namespace NinjaTrader.NinjaScript.Indicators
{
    public class MiddleLineRunCount : Indicator
    {
        private bool bunchStarted = false;
        private double bunchHigh;
        private double bunchLow;
        private int bunchBarCount;
        private double bunchVolumeSoFar;

        private bool hasPriorMid = false;
        private double lastMidValue;
        private int runDirection = 1;
        private int runCount = 0;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Plotted signed run count for MiddleLineTracker.cs -- see that file for the on-chart lines/labels, and MiddleLineSpanCount.cs for the VerticalSpan plot.";
                Name = "MiddleLineRunCount";
                IsOverlay = false;
                Calculate = Calculate.OnBarClose;

                UseMinutesMode = true;
                UseVolumeMode = false;
                MinutesPerBunch = 5;
                VolumePerBunch = 3000;

                AddPlot(Brushes.Silver, "Signed Run Count");
            }
        }

        protected override void OnBarUpdate()
        {
            // NT8 does not carry a plot's value forward between bars on its own -- without this,
            // Values[0][0] silently defaults to 0 on every bar except the rare one where a bunch
            // actually completes, so the line spikes to the real value then snaps back to 0,
            // which at a glance looks like an empty panel even though the data is correct (this
            // was the actual bug, confirmed by the axis auto-scaling to a sensible real range
            // while the line itself was invisible).
            if (CurrentBar >= 1)
                Values[0][0] = Values[0][1];

            if (!bunchStarted)
            {
                bunchHigh = High[0];
                bunchLow = Low[0];
                bunchBarCount = 0;
                bunchVolumeSoFar = 0;
                bunchStarted = true;
            }
            else
            {
                if (High[0] > bunchHigh) bunchHigh = High[0];
                if (Low[0] < bunchLow) bunchLow = Low[0];
            }

            bunchBarCount++;
            bunchVolumeSoFar += Volume[0];

            bool bunchComplete = UseVolumeMode
                ? bunchVolumeSoFar >= VolumePerBunch
                : (UseMinutesMode && bunchBarCount >= MinutesPerBunch);

            if (!bunchComplete)
                return;

            double midValue = (bunchHigh + bunchLow) / 2.0;

            if (hasPriorMid)
            {
                if (midValue > lastMidValue)
                {
                    if (runDirection == 1)
                        runCount++;
                    else
                    {
                        runDirection = 1;
                        runCount = 1;
                    }
                }
                else if (midValue < lastMidValue)
                {
                    if (runDirection == -1)
                        runCount++;
                    else
                    {
                        runDirection = -1;
                        runCount = 1;
                    }
                }
                // exact tie: runDirection/runCount unchanged, same signed number repeats
            }
            else
            {
                runDirection = 1;
                runCount = 1;
            }

            Values[0][0] = runDirection == 1 ? runCount : -runCount;

            lastMidValue = midValue;
            hasPriorMid = true;
            bunchStarted = false;
        }

        #region Properties
        [NinjaScriptProperty]
        [Display(Name = "Use Minutes Mode", GroupName = "0 Bucket Mode", Order = 1)]
        public bool UseMinutesMode { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Use Volume Mode (wins if both on)", GroupName = "0 Bucket Mode", Order = 2)]
        public bool UseVolumeMode { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Minutes Per Bunch (bars of this chart)", GroupName = "0 Bucket Mode", Order = 3)]
        public int MinutesPerBunch { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Volume Per Bunch (contracts)", GroupName = "0 Bucket Mode", Order = 4)]
        public int VolumePerBunch { get; set; }
        #endregion
    }
}
