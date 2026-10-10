#region Using declarations
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.Text;
using System.Threading;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using Npgsql; // requires Npgsql.dll (+ deps) dropped into bin\Custom, see notes
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    /// <summary>
    /// Headless tick recorder. No plots, no overlay drawing.
    /// Add to a 1-tick chart of the instrument you want captured and leave it open.
    /// Writes every trade print to Postgres in batches on a background thread.
    /// </summary>
    public class TickRecorder : Indicator
    {
        private NpgsqlConnection conn;
        private readonly ConcurrentQueue<TickRow> queue = new ConcurrentQueue<TickRow>();
        private Timer flushTimer;
        private double lastBid, lastAsk;
        private string tableName;

        // Which physical table this chart's ticks land in -- "ticks_nq" on an
        // NQ chart, "ticks_es" on an ES chart, etc -- so the same compiled
        // indicator works unmodified on either instrument instead of needing
        // a near-duplicate copy per instrument. Only alphanumerics survive
        // (defensive; MasterInstrument.Name is trusted NinjaTrader metadata,
        // not external input, but the table name still gets interpolated
        // into SQL below so it's sanitized anyway).
        private static string TableNameFor(Instrument instrument)
        {
            string root = instrument.MasterInstrument.Name.ToLowerInvariant();
            var sb = new StringBuilder();
            foreach (char c in root)
                if (char.IsLetterOrDigit(c)) sb.Append(c);
            return "ticks_" + sb.ToString();
        }

        // Password comes from the POSTGRES_PASSWORD Windows environment variable
        // (System Properties > Environment Variables, User variables), not hardcoded
        // here -- set it once and restart NinjaTrader so the new process picks it up.
        private static string BuildConnString()
        {
            string password = Environment.GetEnvironmentVariable("POSTGRES_PASSWORD");
            if (string.IsNullOrEmpty(password))
                throw new InvalidOperationException(
                    "POSTGRES_PASSWORD environment variable is not set. " +
                    "Set it under Windows System Properties > Environment Variables, " +
                    "then restart NinjaTrader.");
            return $"Host=localhost;Port=5432;Database=market_data;Username=postgres;Password={password}";
        }

        private struct TickRow
        {
            public DateTime TsUtc;
            public double Price;
            public long Volume;
            public double Bid;
            public double Ask;
            public string Side;
        }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Headless recorder: writes every tick to Postgres. No visual output.";
                Name = "TickRecorder";
                Calculate = Calculate.OnEachTick;
                IsOverlay = true;
                DisplayInDataBox = false;
                DrawOnPricePanel = false;
                PaintPriceMarkers = false;
                ScaleJustification = NinjaTrader.Gui.Chart.ScaleJustification.Right;
                IsSuspendedWhileInactive = false; // keep recording even if chart isn't focused
            }
            else if (State == State.Configure)
            {
                // no plots added on purpose
            }
            else if (State == State.DataLoaded)
            {
                tableName = TableNameFor(Instrument);
                conn = new NpgsqlConnection(BuildConnString());
                conn.Open();
                EnsureSchema();

                // flush every 500ms on a background thread so we never block the market data thread
                flushTimer = new Timer(FlushQueue, null, 500, 500);
            }
            else if (State == State.Terminated)
            {
                flushTimer?.Dispose();
                FlushQueue(null); // final flush
                conn?.Close();
                conn?.Dispose();
            }
        }

        private void EnsureSchema()
        {
            string sql = $@"
                CREATE TABLE IF NOT EXISTS {tableName} (
                    id BIGSERIAL PRIMARY KEY,
                    instrument TEXT NOT NULL,
                    ts_utc TIMESTAMPTZ NOT NULL,
                    price DOUBLE PRECISION,
                    volume BIGINT,
                    bid DOUBLE PRECISION,
                    ask DOUBLE PRECISION,
                    side TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_{tableName}_instrument_time
                    ON {tableName} (instrument, ts_utc);";
            using (var cmd = new NpgsqlCommand(sql, conn))
                cmd.ExecuteNonQuery();
        }

        protected override void OnMarketData(MarketDataEventArgs e)
        {
            // track quotes so we can tick-rule classify trade prints
            if (e.MarketDataType == MarketDataType.Bid)
            {
                lastBid = e.Price;
                return;
            }
            if (e.MarketDataType == MarketDataType.Ask)
            {
                lastAsk = e.Price;
                return;
            }
            if (e.MarketDataType != MarketDataType.Last)
                return;

            string side;
            double mid = (lastBid + lastAsk) / 2.0;
            if (lastAsk > 0 && e.Price >= lastAsk) side = "buy";
            else if (lastBid > 0 && e.Price <= lastBid) side = "sell";
            else if (lastBid > 0 && lastAsk > 0) side = e.Price >= mid ? "buy" : "sell";
            else side = "unknown"; // no quote yet (e.g. pre-market)

            queue.Enqueue(new TickRow
            {
                TsUtc = e.Time.ToUniversalTime(),
                Price = e.Price,
                Volume = e.Volume,
                Bid = lastBid,
                Ask = lastAsk,
                Side = side
            });
        }

        private void FlushQueue(object stateInfo)
        {
            if (queue.IsEmpty || conn == null || conn.State != System.Data.ConnectionState.Open)
                return;

            var batch = new List<TickRow>();
            while (queue.TryDequeue(out var row) && batch.Count < 5000)
                batch.Add(row);

            if (batch.Count == 0) return;

            try
            {
                using (var tx = conn.BeginTransaction())
                {
                    foreach (var row in batch)
                    {
                        using (var cmd = conn.CreateCommand())
                        {
                            cmd.Transaction = tx;
                            cmd.CommandText =
                                $"INSERT INTO {tableName} (instrument, ts_utc, price, volume, bid, ask, side) " +
                                "VALUES (@instrument, @ts, @price, @vol, @bid, @ask, @side)";
                            cmd.Parameters.AddWithValue("instrument", Instrument.FullName);
                            cmd.Parameters.AddWithValue("ts", row.TsUtc); // Kind=Utc -> maps to timestamptz
                            cmd.Parameters.AddWithValue("price", row.Price);
                            cmd.Parameters.AddWithValue("vol", row.Volume);
                            cmd.Parameters.AddWithValue("bid", row.Bid);
                            cmd.Parameters.AddWithValue("ask", row.Ask);
                            cmd.Parameters.AddWithValue("side", row.Side);
                            cmd.ExecuteNonQuery();
                        }
                    }
                    tx.Commit();
                }
            }
            catch (Exception ex)
            {
                Print("TickRecorder flush error: " + ex.Message);
                // NOTE: batch is dropped on error rather than retried; for production
                // consider writing failed batches to a local fallback file.
            }
        }
    }
}
