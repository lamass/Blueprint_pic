#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.SuperDom;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using NinjaTrader.Core.FloatingPoint;
#endregion

// Groups bars into "bunches" -- either N bars (Minutes mode) or a running volume total (Volume
// mode, whichever button is on; Volume wins if both are) -- and for each bunch draws a
// horizontal MiddleLine at (bunchHigh + bunchLow) / 2, spanning exactly from the bunch's first
// bar to its last.
//
// "Minutes" mode groups N BARS of whatever chart this is applied to, not N real elapsed
// minutes independent of chart period -- apply this to a 1-minute chart for MinutesPerBunch to
// mean literal minutes; on a 5-min chart, MinutesPerBunch=3 groups 15 real minutes. Doing true
// elapsed-time grouping independent of the chart's own bar period would need a secondary time
// series, deliberately skipped for the same robustness reason a secondary tick series was
// dropped from this project's VPIN indicator. "Volume" mode accumulates Volume[0] bar-by-bar
// (not tick-by-tick) until the running total reaches VolumePerBunch -- apply to a finer chart,
// down to 1-tick, for finer bucket granularity.
//
// Each new MiddleLine is counted against the previous one: strictly higher continues an
// up-run (1, 2, 3, ...), strictly lower continues a down-run displayed as negative (-1, -2,
// -3, ...), and crossing from one to the other resets the count to +-1. An exact tie (rare)
// just repeats the last count without incrementing. The count is drawn as text at the new
// line's right end, offset above the line for up-runs / below for down-runs.
//
// The VerticalSpan -- the absolute point distance between this MiddleLine and the previous one
// -- is drawn as text vertically centered between the two lines (deliberately placed in the
// gap between them, not on top of either).
//
// This indicator draws directly on the price panel (IsOverlay = true), which is a hard NT8
// constraint that also pins any of ITS OWN plots to that same price-scale panel -- so a
// points-scale span or a small-integer count plotted here would be invisible next to NQ's
// ~29,000 price level. Two companion files duplicate the same bunch/count/span math purely to
// plot those two series properly scaled, each in its OWN panel: MiddleLineSpanCount.cs (span,
// points) and MiddleLineRunCount.cs (signed count, small integers) -- kept as two separate
// indicators rather than one two-plot indicator because Span and Count are wildly different
// scales and sharing one panel/Y-axis made both hard to read. A shrinking-span run or a
// stalling count (1,2,1,1,2,1,...) reads as a visible trend in these, not just individually-
// read numbers scattered on the price chart here. Add all three to see the full picture; add
// just this one for the on-chart lines/labels alone.
//
// This is a display/analysis Indicator only -- no orders, no strategy logic. A Strategy with
// stop/target built on this would come later, once a rule is actually decided on.

namespace NinjaTrader.NinjaScript.Indicators
{
    public class MiddleLineTracker : Indicator
    {
        private bool bunchStarted = false;
        private double bunchHigh;
        private double bunchLow;
        private int bunchStartBar;
        private int bunchBarCount;
        private double bunchVolumeSoFar;

        private bool hasPriorMid = false;
        private double lastMidValue;
        private int runDirection = 1;   // +1 = up-run, -1 = down-run
        private int runCount = 0;

        private int drawCounter = 0;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Middle-of-range lines over N-bar or volume bunches, with signed run counting and vertical-span tracking between them (accumulation/distribution contraction watch).";
                Name = "MiddleLineTracker";
                IsOverlay = true;
                Calculate = Calculate.OnBarClose;

                UseMinutesMode = true;
                UseVolumeMode = false;
                MinutesPerBunch = 5;
                VolumePerBunch = 3000;
            }
        }

        protected override void OnBarUpdate()
        {
            if (!bunchStarted)
            {
                bunchHigh = High[0];
                bunchLow = Low[0];
                bunchStartBar = CurrentBar;
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

            // UseVolumeMode wins if both toggles are on; UseMinutesMode is otherwise the only
            // other real state (an explicit check, not just "else", so a bug where both toggles
            // end up off doesn't silently fall through to Minutes mode unexamined).
            bool bunchComplete = UseVolumeMode
                ? bunchVolumeSoFar >= VolumePerBunch
                : (UseMinutesMode && bunchBarCount >= MinutesPerBunch);

            if (!bunchComplete)
                return;

            double midValue = (bunchHigh + bunchLow) / 2.0;
            int barsAgoStart = CurrentBar - bunchStartBar;
            int barsAgoEnd = 0;

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
                // exact tie (midValue == lastMidValue): runDirection/runCount stay exactly as
                // they were, so the same signed number repeats rather than resetting or
                // incrementing on a flat bunch.
            }
            else
            {
                runDirection = 1;
                runCount = 1;
            }

            int displayCount = runDirection == 1 ? runCount : -runCount;
            Brush lineBrush = runDirection == 1 ? Brushes.LimeGreen : Brushes.OrangeRed;

            Draw.Line(this, "midline" + (drawCounter++), false, barsAgoStart, midValue, barsAgoEnd, midValue, lineBrush, DashStyleHelper.Solid, 2);

            double labelOffset = 10 * TickSize;
            double labelPrice = midValue + (runDirection == 1 ? labelOffset : -labelOffset);
            Draw.Text(this, "midlinecount" + (drawCounter++), displayCount.ToString(), barsAgoEnd, labelPrice, lineBrush);

            if (hasPriorMid)
            {
                double span = Math.Abs(midValue - lastMidValue);
                double spanMidPrice = (midValue + lastMidValue) / 2.0;
                Draw.Text(this, "span" + (drawCounter++), span.ToString("F2"), barsAgoEnd, spanMidPrice, Brushes.DodgerBlue);
            }

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
