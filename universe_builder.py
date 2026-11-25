"""
universe_builder.py — Dynamic Stock Universe Generator
=======================================================
Builds a fresh candidate list every morning from the S&P 500.

Steps:
  1. Download S&P 500 tickers from Wikipedia (free, no API)
  2. Quick pre-filter via yfinance:
       - Price >= MIN_PRICE
       - 20-day avg volume >= MIN_AVG_VOLUME
       - Has options chain (optionable)
       - Close > SMA 200 (already in uptrend — pointless to scan downtrending stocks)
  3. Return the surviving list for the main scanner

Runs in parallel threads for speed.
Typical result: 150-250 stocks from the S&P 500 pass pre-filter on any given day.
Takes ~2-3 minutes to pre-screen all 500.
"""

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


# ── Pre-filter Parameters (edit here or override from config) ──────────────────
DEFAULT_MIN_PRICE           = 15.0   # skip penny-ish stocks
DEFAULT_MIN_AVG_VOLUME      = 500_000  # skip illiquid names (500k shares/day)
DEFAULT_REQUIRE_OPTIONS     = True   # skip stocks with no options chain
DEFAULT_REQUIRE_UPTREND     = True   # skip stocks below their 200 SMA
DEFAULT_MAX_STOCKS          = 200    # cap universe size (ranked by RS score)
DEFAULT_THREADS             = 12     # parallel fetch threads
DEFAULT_REQUIRE_EPS_GROWTH  = True   # skip stocks with weak earnings growth
DEFAULT_MIN_EPS_GROWTH      = 0.20   # minimum YoY quarterly EPS growth (20%)
DEFAULT_REQUIRE_52WK_RANGE  = True   # skip stocks > 25% below their 52-week high
DEFAULT_MAX_52WK_DIST       = 0.25   # max distance below 52-week high allowed


def _get_sp500_tickers() -> list[str]:
    """
    Pull S&P 500 tickers from Wikipedia.
    Returns a clean list of ticker strings.
    Falls back to a hardcoded top-100 list if Wikipedia is unreachable.
    """
    try:
        import io
        import urllib.request
        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read()
        tables  = pd.read_html(io.BytesIO(html))
        df      = tables[0]
        tickers = df["Symbol"].tolist()
        tickers = [t.replace(".", "-") for t in tickers]
        print(f"  [Universe] S&P 500: {len(tickers)} tickers from Wikipedia")
        return tickers
    except Exception as e:
        short = str(e)[:80]
        print(f"  [Universe] Wikipedia fetch failed ({short}) — using fallback list")
        return _fallback_tickers()


def _fallback_tickers() -> list[str]:
    """Hardcoded S&P 100 as fallback if Wikipedia is unreachable."""
    return [
        "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","BRK-B","JPM",
        "LLY","V","UNH","XOM","MA","COST","HD","PG","JNJ","ABBV",
        "MRK","BAC","CRM","CVX","WMT","KO","NFLX","AMD","PEP","TMO",
        "ACN","ABT","LIN","MCD","GE","DHR","ISRG","CAT","GS","AXP",
        "NOW","SPGI","BLK","RTX","QCOM","TXN","MS","AMGN","HON","SYK",
        "VRTX","REGN","LOW","PANW","CRWD","INTU","PLD","ADI","AMAT","LRCX",
        "DE","UNP","GD","NOC","LMT","ADP","CME","ICE","PYPL","BKNG",
        "SBUX","NKE","TJX","LULU","DECK","FICO","MELI","APP","AXON","PWR",
        "DDOG","SNOW","NET","ZS","FTNT","OXY","COP","SLB","FCX","NEM",
        "CELH","PODD","DXCM","MRNA","BIIB","GILD","CI","HUM","ELV","CVS",
    ]


def _prefilter_stock(
    symbol:             str,
    min_price:          float,
    min_avg_volume:     float,
    require_options:    bool,
    require_uptrend:    bool,
    require_eps_growth: bool  = DEFAULT_REQUIRE_EPS_GROWTH,
    min_eps_growth:     float = DEFAULT_MIN_EPS_GROWTH,
    require_52wk_range: bool  = DEFAULT_REQUIRE_52WK_RANGE,
    max_52wk_dist:      float = DEFAULT_MAX_52WK_DIST,
) -> tuple[str, bool, str]:
    """
    Quick pre-filter for a single stock.
    Returns (symbol, passes, reason).
    """
    try:
        end   = datetime.now()
        start = end - timedelta(days=230)   # enough for SMA200

        df = yf.download(
            symbol, start=start, end=end,
            progress=False, auto_adjust=True,
            multi_level_index=False,
        )

        if df is None or df.empty or len(df) < 30:
            return symbol, False, "Insufficient data"

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close  = df["Close"].squeeze()
        volume = df["Volume"].squeeze()

        last_price  = float(close.iloc[-1])
        avg_vol_20  = float(volume.rolling(20).mean().iloc[-1])

        # Price filter
        if last_price < min_price:
            return symbol, False, f"Price ${last_price:.2f} < ${min_price}"

        # Volume filter
        if avg_vol_20 < min_avg_volume:
            return symbol, False, f"Avg vol {avg_vol_20/1e6:.1f}M < {min_avg_volume/1e6:.1f}M"

        # Uptrend filter (above SMA200)
        if require_uptrend and len(close) >= 200:
            sma200 = float(close.rolling(200).mean().iloc[-1])
            if last_price < sma200:
                return symbol, False, f"Below SMA200 (${last_price:.0f} < ${sma200:.0f})"

        # 52-week high proximity filter (within 25% of 52-week high)
        if require_52wk_range and len(df) >= 60:
            high_col = df["High"].squeeze() if "High" in df.columns else close
            high_52wk = float(high_col.rolling(min(252, len(high_col))).max().iloc[-1])
            if high_52wk > 0:
                dist_from_high = (high_52wk - last_price) / high_52wk
                if dist_from_high > max_52wk_dist:
                    return symbol, False, f"{dist_from_high:.0%} below 52wk high (limit {max_52wk_dist:.0%})"

        # Create Ticker once — reused for options + EPS checks
        tk = yf.Ticker(symbol)

        # Options check
        if require_options:
            try:
                opts = tk.options
                if not opts:
                    return symbol, False, "No options chain"
            except Exception:
                return symbol, False, "Options check failed"

        # EPS growth check — YoY quarterly earnings growth via yfinance info
        if require_eps_growth:
            try:
                info = tk.info
                eg   = info.get("earningsGrowth")   # trailing YoY EPS growth (decimal)
                if eg is not None:
                    if eg < min_eps_growth:
                        return symbol, False, f"EPS growth {eg:.0%} < {min_eps_growth:.0%}"
                # eg is None for financials / REITs / recent IPOs — give benefit of doubt
            except Exception:
                pass   # data unavailable — don't penalise

        return symbol, True, f"OK | ${last_price:.2f} | vol {avg_vol_20/1e6:.1f}M/day"

    except Exception as e:
        return symbol, False, f"Error: {str(e)[:40]}"


def build_universe(
    min_price:          float = DEFAULT_MIN_PRICE,
    min_avg_volume:     float = DEFAULT_MIN_AVG_VOLUME,
    require_options:    bool  = DEFAULT_REQUIRE_OPTIONS,
    require_uptrend:    bool  = DEFAULT_REQUIRE_UPTREND,
    max_stocks:         int   = DEFAULT_MAX_STOCKS,
    threads:            int   = DEFAULT_THREADS,
    verbose:            bool  = True,
    require_eps_growth: bool  = DEFAULT_REQUIRE_EPS_GROWTH,
    min_eps_growth:     float = DEFAULT_MIN_EPS_GROWTH,
    require_52wk_range: bool  = DEFAULT_REQUIRE_52WK_RANGE,
    max_52wk_dist:      float = DEFAULT_MAX_52WK_DIST,
) -> list[str]:
    """
    Build a fresh universe from S&P 500 each morning.

    Returns a list of tickers that pass all pre-filters,
    capped at max_stocks (sorted alphabetically — RS ranking
    happens later in the main scanner).
    """
    print(f"\n  [Universe] Building dynamic universe from S&P 500 ...")
    print(f"  [Universe] Filters: price >= ${min_price} | vol >= {min_avg_volume/1e6:.1f}M/day"
          f" | uptrend: {require_uptrend} | options: {require_options}"
          f" | EPS growth >= {min_eps_growth:.0%}: {require_eps_growth}"
          f" | within {max_52wk_dist:.0%} of 52wk high: {require_52wk_range}")

    tickers = _get_sp500_tickers()
    passed  = []
    failed  = []
    total   = len(tickers)

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {
            pool.submit(
                _prefilter_stock, sym,
                min_price, min_avg_volume, require_options, require_uptrend,
                require_eps_growth, min_eps_growth,
                require_52wk_range, max_52wk_dist,
            ): sym
            for sym in tickers
        }

        done = 0
        for future in as_completed(futures):
            sym, ok, reason = future.result()
            done += 1

            if ok:
                passed.append(sym)
                if verbose:
                    print(f"  [{done:>3}/{total}] {sym:<7}  ✓  {reason}")
            else:
                failed.append((sym, reason))
                if verbose:
                    print(f"  [{done:>3}/{total}] {sym:<7}  ✗  {reason}")

    passed.sort()

    # Cap at max_stocks
    if len(passed) > max_stocks:
        print(f"\n  [Universe] {len(passed)} passed pre-filter — capping at {max_stocks}")
        passed = passed[:max_stocks]

    print(f"\n  [Universe] Final universe: {len(passed)} stocks "
          f"({len(failed)} filtered from {total} S&P 500)")

    return passed


if __name__ == "__main__":
    # Test run
    universe = build_universe(verbose=True)
    print(f"\nFinal list ({len(universe)}):")
    print(", ".join(universe))
