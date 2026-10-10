#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
#endregion

// Companion to MiddleLineTracker.cs: same bunch/MiddleLine math, duplicated here purely to plot
// the VerticalSpan as its own properly-scaled series (own panel, IsOverlay = false).
// MiddleLineTracker.cs itself must be IsOverlay = true so its horizontal lines and text draw
// correctly on the price panel, which would otherwise also pull this points-scale plot onto
// that same ~29,000-scale price panel, where it'd be invisible.
//
// The signed run count is plotted separately, in MiddleLineRunCount.cs -- Span (tens to
// hundreds of points) and Count (small integers) were originally two plots crammed into this
// one panel sharing one Y-axis; auto-scaled to fit both, the small one goes flat-invisible near
// zero and, depending on the numbers, both can end up looking like nothing's there. Splitting
// them into separate indicators, each with its own panel, fixes that for good rather than
// relying on manually reassigning panels in NT8's Indicators dialog.
//
// Add this alongside MiddleLineTracker.cs for the trend view; the two adjustable bucket-mode
// parameters must be set the same on both (and on MiddleLineRunCount.cs) to track the same
// bunches. Keep this formula in sync with MiddleLineTracker.cs if that one's math ever changes.

namespace NinjaTrader.NinjaScript.Indicators
{
    public class MiddleLineSpanCount : Indicator
    {
        private bool bunchStarted = false;
        private double bunchHigh;
        private double bunchLow;
        private int bunchBarCount;
        private double bunchVolumeSoFar;

        private bool hasPriorMid = false;
        private double lastMidValue;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Plotted VerticalSpan and signed run count for MiddleLineTracker.cs -- see that file for the on-chart lines/labels.";
                Name = "MiddleLineSpanCount";
                IsOverlay = false;
                Calculate = Calculate.OnBarClose;

                UseMinutesMode = true;
                UseVolumeMode = false;
                MinutesPerBunch = 5;
                VolumePerBunch = 3000;

                AddPlot(Brushes.DodgerBlue, "Vertical Span (points)");
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
                Values[0][0] = Math.Abs(midValue - lastMidValue);

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
