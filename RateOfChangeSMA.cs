#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.SuperDom;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

// RateOfChangeSMA
//
// Rate of change = (Close[0] - Close[1]) / elapsed seconds between the two
// bars' timestamps -- NOT a fixed divisor. That's the whole point: it uses
// Time[0]-Time[1], which NinjaTrader gives you correctly for every bar type
// (tick, second, minute, range, Renko...), so the same indicator, with the
// same SMA lengths, is directly comparable across a tick chart and a time
// chart instead of silently meaning something different on each.
//
// Three SMAs of the RoC series are plotted on top of it, each independently
// toggleable and independently sized in Properties. A "Show RoC bars" switch
// hides the raw histogram (output NaN => NinjaTrader just doesn't draw it and
// the panel autoscales to whatever's left visible), which is the equivalent
// of the "Spikes" toggle from the companion web chart: useful because a single
// very-short tick bar can produce an outsized pts/sec spike that dominates the
// panel's scale and makes the SMAs hard to read.
//
// ShowCrossSignals plots an arrow where the fast SMA crosses the slow SMA.
// This is a plain visual crossover marker, nothing more -- it has not been
// backtested for win rate, false-positive rate, or profitability, and a
// crossover on a smoothed derivative of price is a lagging, noisy signal by
// construction. Treat it as "worth a look," not "a signal to trade on."

namespace NinjaTrader.NinjaScript.Indicators
{
	public class RateOfChangeSMA : Indicator
	{
		private Series<double> roc;

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Description					= @"Rate of change (Δclose ÷ elapsed seconds since the prior bar's close) with up to three independently toggleable/length-adjustable SMAs. Uses each bar's actual timestamp, so it works unmodified on tick, second, minute, or any other bar type.";
				Name						= "RateOfChangeSMA";
				Calculate					= Calculate.OnBarClose;
				IsOverlay					= false;
				DisplayInDataBox			= true;
				DrawOnPricePanel			= false;
				PaintPriceMarkers			= true;
				ScaleJustification			= NinjaTrader.Gui.Chart.ScaleJustification.Right;
				IsSuspendedWhileInactive	= true;

				AddPlot(new Stroke(Brushes.DimGray,     2), PlotStyle.Bar,  "RoC");
				AddPlot(new Stroke(Brushes.DodgerBlue,  2), PlotStyle.Line, "Sma1");
				AddPlot(new Stroke(Brushes.DarkOrange,  2), PlotStyle.Line, "Sma2");
				AddPlot(new Stroke(Brushes.MediumSeaGreen, 2), PlotStyle.Line, "Sma3");

				ShowRoc				= true;
				ShowSma1			= true;
				ShowSma2			= true;
				ShowSma3			= true;
				Sma1Period			= 5;
				Sma2Period			= 20;
				Sma3Period			= 50;
				ShowCrossSignals	= false;
				UpBrush				= Brushes.Lime;
				DownBrush			= Brushes.Red;
			}
			else if (State == State.DataLoaded)
			{
				roc = new Series<double>(this);
			}
		}

		protected override void OnBarUpdate()
		{
			if (CurrentBar < 1)
			{
				roc[0] = 0.0;
				Values[0][0] = double.NaN;
				Values[1][0] = double.NaN;
				Values[2][0] = double.NaN;
				Values[3][0] = double.NaN;
				return;
			}

			double dtSeconds = (Time[0] - Time[1]).TotalSeconds;
			// Two bars sharing a timestamp is a rare edge case (fast tick/Renko
			// bursts) -- treat it as zero rate of change rather than NaN, so the
			// SMA sums below stay simple.
			roc[0] = dtSeconds > 0 ? (Close[0] - Close[1]) / dtSeconds : 0.0;

			if (ShowRoc)
			{
				Values[0][0] = roc[0];
				PlotBrushes[0][0] = roc[0] >= 0 ? UpBrush : DownBrush;
			}
			else
			{
				Values[0][0] = double.NaN;
			}

			double sma1 = Sma(Sma1Period, 0);
			double sma2 = Sma(Sma2Period, 0);
			double sma3 = Sma(Sma3Period, 0);

			Values[1][0] = ShowSma1 && CurrentBar >= Sma1Period ? sma1 : double.NaN;
			Values[2][0] = ShowSma2 && CurrentBar >= Sma2Period ? sma2 : double.NaN;
			Values[3][0] = ShowSma3 && CurrentBar >= Sma3Period ? sma3 : double.NaN;

			// Visual-only fast/slow SMA crossover marker -- see the caveat in
			// the file header before treating this as a signal.
			if (ShowCrossSignals && ShowSma1 && ShowSma3 && CurrentBar >= Sma3Period + 1)
			{
				double sma1Prev = Sma(Sma1Period, 1);
				double sma3Prev = Sma(Sma3Period, 1);
				bool crossedUp   = sma1Prev <= sma3Prev && sma1 > sma3;
				bool crossedDown = sma1Prev >= sma3Prev && sma1 < sma3;

				double offset = MarkerOffset();
				if (crossedUp)
					Draw.ArrowUp(this, "rocUp" + CurrentBar, false, 0, sma3 - offset, UpBrush);
				if (crossedDown)
					Draw.ArrowDown(this, "rocDown" + CurrentBar, false, 0, sma3 + offset, DownBrush);
			}
		}

		// Simple moving average of the RoC series, "period" bars wide, ending
		// "barsAgo" bars back. Written by hand (rather than calling the built-in
		// SMA()) so length and enable/disable stay per-line, independent inputs.
		private double Sma(int period, int barsAgo)
		{
			if (CurrentBar - barsAgo < period)
				return 0.0;

			double sum = 0.0;
			for (int i = barsAgo; i < barsAgo + period; i++)
				sum += roc[i];
			return sum / period;
		}

		// Small vertical offset for the cross-signal arrows, scaled to recent
		// RoC volatility so it reads sensibly at any timeframe/bar type.
		private double MarkerOffset()
		{
			double maxAbs = 0.0;
			int look = Math.Min(CurrentBar, 50);
			for (int i = 0; i < look; i++)
				maxAbs = Math.Max(maxAbs, Math.Abs(roc[i]));
			return maxAbs * 0.15 + 1e-9;
		}

		#region Properties
		[NinjaScriptProperty]
		[Display(Name = "Show RoC bars", GroupName = "1. Visibility", Order = 0)]
		public bool ShowRoc { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Show SMA 1", GroupName = "1. Visibility", Order = 1)]
		public bool ShowSma1 { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Show SMA 2", GroupName = "1. Visibility", Order = 2)]
		public bool ShowSma2 { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Show SMA 3", GroupName = "1. Visibility", Order = 3)]
		public bool ShowSma3 { get; set; }

		[NinjaScriptProperty]
		[Range(1, 1000)]
		[Display(Name = "SMA 1 length", GroupName = "2. SMA lengths", Order = 0)]
		public int Sma1Period { get; set; }

		[NinjaScriptProperty]
		[Range(1, 1000)]
		[Display(Name = "SMA 2 length", GroupName = "2. SMA lengths", Order = 1)]
		public int Sma2Period { get; set; }

		[NinjaScriptProperty]
		[Range(1, 1000)]
		[Display(Name = "SMA 3 length", GroupName = "2. SMA lengths", Order = 2)]
		public int Sma3Period { get; set; }

		[NinjaScriptProperty]
		[Display(Name = "Show fast/slow SMA cross markers (visual aid only, not a validated signal)", GroupName = "3. Signals", Order = 0)]
		public bool ShowCrossSignals { get; set; }

		[XmlIgnore]
		[Display(Name = "Up color", GroupName = "4. Colors", Order = 0)]
		public Brush UpBrush { get; set; }

		[Browsable(false)]
		public string UpBrushSerializable
		{
			get { return Serialize.BrushToString(UpBrush); }
			set { UpBrush = Serialize.StringToBrush(value); }
		}

		[XmlIgnore]
		[Display(Name = "Down color", GroupName = "4. Colors", Order = 1)]
		public Brush DownBrush { get; set; }

		[Browsable(false)]
		public string DownBrushSerializable
		{
			get { return Serialize.BrushToString(DownBrush); }
			set { DownBrush = Serialize.StringToBrush(value); }
		}

		[Browsable(false)]
		[XmlIgnore]
		public Series<double> Sma1Plot { get { return Values[1]; } }

		[Browsable(false)]
		[XmlIgnore]
		public Series<double> Sma2Plot { get { return Values[2]; } }

		[Browsable(false)]
		[XmlIgnore]
		public Series<double> Sma3Plot { get { return Values[3]; } }
		#endregion
	}
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private RateOfChangeSMA[] cacheRateOfChangeSMA;
		public RateOfChangeSMA RateOfChangeSMA(bool showRoc, bool showSma1, bool showSma2, bool showSma3, int sma1Period, int sma2Period, int sma3Period, bool showCrossSignals)
		{
			return RateOfChangeSMA(Input, showRoc, showSma1, showSma2, showSma3, sma1Period, sma2Period, sma3Period, showCrossSignals);
		}

		public RateOfChangeSMA RateOfChangeSMA(ISeries<double> input, bool showRoc, bool showSma1, bool showSma2, bool showSma3, int sma1Period, int sma2Period, int sma3Period, bool showCrossSignals)
		{
			if (cacheRateOfChangeSMA != null)
				for (int idx = 0; idx < cacheRateOfChangeSMA.Length; idx++)
					if (cacheRateOfChangeSMA[idx] != null && cacheRateOfChangeSMA[idx].ShowRoc == showRoc && cacheRateOfChangeSMA[idx].ShowSma1 == showSma1 && cacheRateOfChangeSMA[idx].ShowSma2 == showSma2 && cacheRateOfChangeSMA[idx].ShowSma3 == showSma3 && cacheRateOfChangeSMA[idx].Sma1Period == sma1Period && cacheRateOfChangeSMA[idx].Sma2Period == sma2Period && cacheRateOfChangeSMA[idx].Sma3Period == sma3Period && cacheRateOfChangeSMA[idx].ShowCrossSignals == showCrossSignals && cacheRateOfChangeSMA[idx].EqualsInput(input))
						return cacheRateOfChangeSMA[idx];

			RateOfChangeSMA indicator = new RateOfChangeSMA
			{
				ShowRoc = showRoc,
				ShowSma1 = showSma1,
				ShowSma2 = showSma2,
				ShowSma3 = showSma3,
				Sma1Period = sma1Period,
				Sma2Period = sma2Period,
				Sma3Period = sma3Period,
				ShowCrossSignals = showCrossSignals
			};
			indicator.SetState(State.Configure);
			indicator.SetState(State.DataLoaded);
			Indicators.Add(indicator);
			indicator.SetUp();

			RateOfChangeSMA[] tmp = new RateOfChangeSMA[cacheRateOfChangeSMA == null ? 1 : cacheRateOfChangeSMA.Length + 1];
			if (cacheRateOfChangeSMA != null)
				cacheRateOfChangeSMA.CopyTo(tmp, 0);
			tmp[tmp.Length - 1] = indicator;
			cacheRateOfChangeSMA = tmp;
			Indicators.Add(indicator);
			return indicator;
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.RateOfChangeSMA RateOfChangeSMA(bool showRoc, bool showSma1, bool showSma2, bool showSma3, int sma1Period, int sma2Period, int sma3Period, bool showCrossSignals)
		{
			return indicator.RateOfChangeSMA(Input, showRoc, showSma1, showSma2, showSma3, sma1Period, sma2Period, sma3Period, showCrossSignals);
		}

		public Indicators.RateOfChangeSMA RateOfChangeSMA(ISeries<double> input, bool showRoc, bool showSma1, bool showSma2, bool showSma3, int sma1Period, int sma2Period, int sma3Period, bool showCrossSignals)
		{
			return indicator.RateOfChangeSMA(input, showRoc, showSma1, showSma2, showSma3, sma1Period, sma2Period, sma3Period, showCrossSignals);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.RateOfChangeSMA RateOfChangeSMA(bool showRoc, bool showSma1, bool showSma2, bool showSma3, int sma1Period, int sma2Period, int sma3Period, bool showCrossSignals)
		{
			return indicator.RateOfChangeSMA(Input, showRoc, showSma1, showSma2, showSma3, sma1Period, sma2Period, sma3Period, showCrossSignals);
		}

		public Indicators.RateOfChangeSMA RateOfChangeSMA(ISeries<double> input, bool showRoc, bool showSma1, bool showSma2, bool showSma3, int sma1Period, int sma2Period, int sma3Period, bool showCrossSignals)
		{
			return indicator.RateOfChangeSMA(input, showRoc, showSma1, showSma2, showSma3, sma1Period, sma2Period, sma3Period, showCrossSignals);
		}
	}
}

#endregion
