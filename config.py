"""
config.py — Momentum Investing Scanner Configuration
import os  # credentials come from environment variables, never hardcoded
=====================================================
THIS IS THE ONLY FILE YOU NEED TO EDIT.

Strategy layers:
  1.  Trend Filter      — Close > SMA50 > SMA200, EMA20 slope positive
  2.  Weekly Trend      — weekly Close > weekly SMA50
  3.  Relative Strength — stock must be top 30% of universe vs SPY
  4.  Earnings Blackout — skip if earnings within N days
  5.  VIX Gate          — no new breakouts if VIX > 25; no longs if VIX > 35
  6.  Market Regime     — SPY + QQQ must both be above their 50 SMA
  7.  Setup             — Pullback (RSI 40-55, near SMA50, quiet volume)
                       OR Breakout (20-day high, volume spike)
  8.  Volume / ATR      — expanding confirmation
  9.  Options Flow      — C/P ratio >= 2x for 30-90 DTE
  10. Risk / Reward     — >= 2:1, position sized to 1% account risk
"""

import os as _os

# ── Alpaca ────────────────────────────────────────────────────────────────────
# Set these in GitHub Secrets (scanner) and Render env vars (approval server).
# Never hardcode credentials here.
ALPACA_API_KEY    = _os.environ.get("ALPACA_API_KEY",    "")
ALPACA_API_SECRET = _os.environ.get("ALPACA_API_SECRET", "")
ALPACA_BASE_URL   = _os.environ.get("ALPACA_BASE_URL",
                                    "https://paper-api.alpaca.markets")

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_SENDER       = _os.environ.get("GMAIL_SENDER",       "")
GMAIL_APP_PASSWORD = _os.environ.get("GMAIL_APP_PASSWORD",  "")
GMAIL_RECIPIENT    = _os.environ.get("GMAIL_RECIPIENT",    "")

# ── Account / Position Sizing ─────────────────────────────────────────────────
ACCOUNT_SIZE         = 100_000         # fallback if broker balance is unavailable; set to your account size
RISK_PER_TRADE_PCT   = 0.02           # 2% of account per trade (max loss if stopped)
MAX_POSITION_PCT     = 0.10           # never deploy > 10% of account in one stock
DAILY_MAX_LOSS_PCT   = 0.02           # halt trading if day PnL < -2%
MAX_CONCURRENT_TRADES = 5             # max open positions at once

# ── Approval Server ───────────────────────────────────────────────────────────
FLASK_PORT    = 5002                  # LanceBTrading uses 5001, stock screener uses 5000

# ── Results Persistence ────────────────────────────────────────────────────────
RESULTS_FILE  = "momentum_results.json"
TRADE_LOG_FILE = "momentum_trade_log.json"

# ── Market Regime Filter ──────────────────────────────────────────────────────
# Abort entire scan if SPY and/or QQQ are below their 50 SMA.
# If True, BOTH must pass. If False, only SPY is required.
REQUIRE_QQQ_REGIME    = True
REGIME_SMA_PERIOD     = 50

# ── VIX Thresholds ────────────────────────────────────────────────────────────
VIX_SKIP_BREAKOUTS    = 25.0          # if VIX > 25, skip breakout setups entirely
VIX_NO_TRADE          = 35.0          # if VIX > 35, send "no trade" email and stop

# ── Earnings Blackout ─────────────────────────────────────────────────────────
EARNINGS_BLACKOUT_DAYS = 5            # skip stock if earnings are within this many days

# ── Relative Strength vs SPY ──────────────────────────────────────────────────
# Stocks are ranked by 3-month + 6-month return vs SPY.
# Only the top percentile of the universe passes.
RS_LOOKBACK_3M        = 63            # trading days (~3 months)
RS_LOOKBACK_6M        = 126           # trading days (~6 months)
RS_3M_WEIGHT          = 0.4           # 3-month RS weight in composite score
RS_6M_WEIGHT          = 0.6           # 6-month RS weight (longer horizon gets more weight)
RS_MIN_PERCENTILE     = 0.60          # must rank >= 60th percentile (top 40%)

# ── Weekly Timeframe ──────────────────────────────────────────────────────────
# Additional trend confirmation on the weekly chart.
WEEKLY_TREND_CHECK    = False         # disabled — too restrictive in volatile markets
WEEKLY_SMA_PERIOD     = 50

# ── Pullback Setup ────────────────────────────────────────────────────────────
PULLBACK_RSI_MIN      = 40
PULLBACK_RSI_MAX      = 55
PULLBACK_MAX_DIST     = 0.08          # within ±8% of SMA50
# Improvement 9: quiet volume on pullback — declining volume = healthy rest
PULLBACK_QUIET_VOLUME = False         # pullback bars should be BELOW 20-day vol avg
PULLBACK_MAX_VOL_MULT = 1.0           # pullback day volume <= 1.0x avg (quiet)

# ── Breakout Setup ────────────────────────────────────────────────────────────
BREAKOUT_LOOKBACK     = 20            # bars for "highest high" check
BREAKOUT_VOL_MULT     = 1.5           # min volume spike on breakout bar
# 52-week proximity check removed from breakout setup — now enforced in universe builder (25%)
BREAKOUT_52WK_CHECK   = False
BREAKOUT_52WK_PROX    = 0.25          # reference only — filter lives in universe builder

# ── Volume / ATR Confirmation ─────────────────────────────────────────────────
VOLUME_SPIKE_MULT     = 1.5           # general volume spike threshold
ATR_PERIOD            = 14

# ── Options Flow ──────────────────────────────────────────────────────────────
MIN_CALL_PUT_RATIO    = 2.0           # calls must outnumber puts 2:1 (30-90 DTE)
REQUIRE_OPTIONS       = False         # True = hard filter | False = advisory only

# ── Risk / Reward ─────────────────────────────────────────────────────────────
MIN_RISK_REWARD       = 2.0
TARGET_1_PCT          = 0.20          # +20% → sell 50%
TARGET_2_PCT          = 0.40          # +40% → trail remainder on EMA20
STOP_ATR_MULT         = 2.0           # stop at max 2×ATR below entry

# ── Expected Value Assumptions ────────────────────────────────────────────────
# Win rates from Minervini / IBD research — recalibrate after 50+ live trades.
# Higher targets (T1=+20%, T2=+40%) mean lower win rate but larger avg win.
EV_WIN_RATE_PULLBACK  = 0.35          # 35% — lower hit rate vs old +12% T1 (wider target)
EV_WIN_RATE_BREAKOUT  = 0.32          # 32% — fewer wins, bigger when right
EV_WIN_RATE_BREAKOUT_52WK = 0.38      # 38% — near-ATH breakouts still premium quality

# ── Data Fetch ────────────────────────────────────────────────────────────────
PRICE_LOOKBACK_DAYS   = 400           # need 300+ bars for SMA200 + 52wk high
FETCH_THREADS         = 8             # parallel threads for data fetch

# ── Scan Schedule ─────────────────────────────────────────────────────────────
RUN_INTERVAL_HOURS    = 0             # 0 = run once | 24 = daily rescan

# ── Universe Mode ─────────────────────────────────────────────────────────────
# True  = build fresh from S&P 500 every run (slower ~3 min, broader coverage)
# False = use the fixed UNIVERSE list below (faster, you control exactly what's scanned)
USE_DYNAMIC_UNIVERSE = True

# Dynamic universe pre-filter settings (only used when USE_DYNAMIC_UNIVERSE = True)
DYNAMIC_MIN_PRICE      = 15.0        # skip stocks below this price
DYNAMIC_MIN_AVG_VOLUME = 500_000     # skip stocks with < 500k avg daily volume
DYNAMIC_REQUIRE_OPTIONS= True        # skip stocks with no options chain
DYNAMIC_REQUIRE_UPTREND= True        # skip stocks below their 200 SMA (pre-filter)
DYNAMIC_MAX_STOCKS     = 200         # cap at this many stocks after pre-filter
DYNAMIC_THREADS        = 12          # parallel threads for pre-filter fetch
DYNAMIC_REQUIRE_EPS_GROWTH = False   # disabled — re-enable when market stabilises
DYNAMIC_MIN_EPS_GROWTH     = 0.10   # minimum YoY quarterly earnings growth (decimal)
DYNAMIC_REQUIRE_52WK_RANGE = False   # disabled — market correction pushed most stocks down
DYNAMIC_MAX_52WK_DIST      = 0.35   # max distance below 52-week high (widened for correction)

# ── Fixed Universe (used when USE_DYNAMIC_UNIVERSE = False) ───────────────────
UNIVERSE = [
    # Mega-cap Tech / Semiconductors
    "AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMZN", "TSLA", "AVGO", "AMD", "QCOM",
    # Cloud / SaaS
    "CRM", "NOW", "SNOW", "DDOG", "PANW", "CRWD", "FTNT", "ZS", "NET",
    # Financials
    "JPM", "GS", "V", "MA", "BLK", "SPGI", "ICE", "CME", "AXP",
    # Healthcare / Biopharma
    "UNH", "LLY", "ABBV", "TMO", "ISRG", "DXCM", "VRTX", "REGN",
    # Consumer / Retail
    "COST", "NKE", "MCD", "SBUX", "LOW", "HD", "TJX", "BKNG", "LULU",
    # Industrials / Defense
    "CAT", "DE", "HON", "RTX", "LMT", "NOC", "PWR", "AXON",
    # Energy
    "XOM", "CVX", "COP", "OXY", "SLB",
    # Materials
    "FCX", "NEM", "LIN", "APD",
    # High-momentum names
    "DECK", "FICO", "PODD", "CELH", "MELI", "APP",
]
