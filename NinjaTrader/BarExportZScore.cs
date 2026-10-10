#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Text;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

// BarExportZScore: dumps every closed chart bar plus the hosted ZScoreHybrid value to CSV,
// so offline Python analysis uses exactly the bars/values shown on the chart.
namespace NinjaTrader.NinjaScript.Indicators
{
    public class BarExportZScore : Indicator
    {
        private ZScoreHybrid zs;
        private StreamWriter writer;
        private string filePath;
        private int rowsWritten;
        private int warmup;
        private int lastWrittenBar = -1;
        private bool ioFailed;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description              = "Exports chart bars + ZScoreHybrid value to CSV (claude_probe).";
                Name                     = "BarExportZScore";
                Calculate                = Calculate.OnBarClose;
                IsOverlay                = true;
                DisplayInDataBox         = false;
                DrawOnPricePanel         = true;
                PaintPriceMarkers        = false;
                IsSuspendedWhileInactive = false;

                ZPeriod       = 30;
                ZSignalSmooth = 3;
                ZTrendPeriod  = 200;
                OutputFolder  = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "claude_probe");
            }
            else if (State == State.DataLoaded)
            {
                zs     = ZScoreHybrid(ZPeriod, ZSignalSmooth, ZTrendPeriod, true, 2, 1, -2, -1, 1.5, true);
                warmup = Math.Max(ZTrendPeriod, Math.Max(ZPeriod, ZSignalSmooth)) + 2;
                rowsWritten    = 0;
                lastWrittenBar = -1;
                ioFailed       = false;
                OpenFile();
            }
            else if (State == State.Realtime)
            {
                FlushSafe();
                ShowStatus();
            }
            else if (State == State.Terminated)
            {
                FlushSafe();
                try
                {
                    if (writer != null)
                        writer.Dispose();
                }
                catch (Exception ex)
                {
                    Print("BarExportZScore: error closing file: " + ex.Message);
                }
                writer = null;
            }
        }

        private void OpenFile()
        {
            try
            {
                string folder = string.IsNullOrWhiteSpace(OutputFolder)
                    ? Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "claude_probe")
                    : OutputFolder;
                Directory.CreateDirectory(folder);

                string instr = Instrument.FullName.Replace(' ', '_');
                foreach (char c in Path.GetInvalidFileNameChars())
                    instr = instr.Replace(c, '_');
                string fileName = string.Format(CultureInfo.InvariantCulture, "{0}_{1}{2}_bars.csv",
                    instr, BarsPeriod.Value, BarsPeriod.BarsPeriodType);
                filePath = Path.Combine(folder, fileName);

                var fs = new FileStream(filePath, FileMode.Create, FileAccess.Write, FileShare.ReadWrite);
                writer = new StreamWriter(fs, new UTF8Encoding(false));
                writer.AutoFlush = false;
                writer.WriteLine("bar_index,time,open,high,low,close,volume,z,is_first_bar_of_session");
                writer.Flush();
            }
            catch (Exception ex)
            {
                ioFailed = true;
                writer   = null;
                Print("BarExportZScore: could not open output file '" + filePath + "': " + ex.Message);
            }
        }

        private void FlushSafe()
        {
            try
            {
                if (writer != null)
                    writer.Flush();
            }
            catch (Exception ex)
            {
                Print("BarExportZScore: flush error: " + ex.Message);
            }
        }

        private void ShowStatus()
        {
            string msg = ioFailed
                ? "BarExport: FAILED (see Output window)"
                : string.Format(CultureInfo.InvariantCulture, "BarExport: {0} rows -> {1}", rowsWritten, filePath);
            Draw.TextFixed(this, "BarExportZScore_status", msg, TextPosition.BottomRight);
        }

        protected override void OnBarUpdate()
        {
            if (writer == null || ioFailed)
                return;
            if (CurrentBar <= lastWrittenBar)
                return; // OnBarClose: one call per bar, but be safe against duplicates

            // Ensure the hosted indicator has processed this bar before reading it.
            zs.Update();

            string zText = "";
            if (CurrentBar >= warmup)
                zText = zs.Value[0].ToString("R", CultureInfo.InvariantCulture);

            var ci = CultureInfo.InvariantCulture;
            var sb = new StringBuilder(128);
            sb.Append(CurrentBar.ToString(ci)).Append(',')
              .Append(Time[0].ToString("yyyy-MM-dd HH:mm:ss.fff", ci)).Append(',')
              .Append(Open[0].ToString("R", ci)).Append(',')
              .Append(High[0].ToString("R", ci)).Append(',')
              .Append(Low[0].ToString("R", ci)).Append(',')
              .Append(Close[0].ToString("R", ci)).Append(',')
              .Append(Volume[0].ToString("R", ci)).Append(',')
              .Append(zText).Append(',')
              .Append(Bars.IsFirstBarOfSession ? "1" : "0");

            try
            {
                writer.WriteLine(sb.ToString());
                rowsWritten++;
                lastWrittenBar = CurrentBar;
                if (rowsWritten % 500 == 0)
                    writer.Flush();
            }
            catch (Exception ex)
            {
                ioFailed = true;
                Print("BarExportZScore: write error: " + ex.Message);
            }

            if (State == State.Realtime || ioFailed)
                ShowStatus();
            else if (CurrentBar >= Count - 2)
                ShowStatus(); // last historical bar(s); playback/offline charts never reach Realtime
        }

        #region Properties
        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "ZPeriod", Order = 1, GroupName = "Parameters")]
        public int ZPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "ZSignalSmooth", Order = 2, GroupName = "Parameters")]
        public int ZSignalSmooth { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "ZTrendPeriod", Order = 3, GroupName = "Parameters")]
        public int ZTrendPeriod { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "OutputFolder", Order = 4, GroupName = "Parameters")]
        public string OutputFolder { get; set; }
        #endregion
    }
}
