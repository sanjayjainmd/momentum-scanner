"""
data_fetcher.py — Price Data + All Technical Indicators + Context Data
=======================================================================
Uses yfinance exclusively (free, no API key).

Price data:
  SMA(200), SMA(50), EMA(20), RSI(14), ATR(14)
  Volume SMA(20), 20-day prior high (breakout), 5-bar swing low
  52-week high
  EMA(20) slope

Context functions:
  fetch_market_regime()   — SPY + QQQ vs their 50 SMA
  fetch_vix()             — current VIX level
  fetch_earnings_date()   — next expected earnings date
  fetch_weekly_data()     — weekly chart with SMA50
  compute_rs_score()      — relative strength vs SPY (3m + 6m)
"""

import warnings
warnings.filterwarnings("ignore")

import threading
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

# yfinance is NOT thread-safe — concurrent yf.download() calls share an internal
# HTTP session and can return the wrong ticker's data when run in parallel.
# This lock serialises all downloads so each request completes cleanly.
_yf_lock = threading.Lock()


# ── Technical Helpers ─────────────────────────────────────────────────────────

def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period).mean()


def _download(symbol: str, start, end) -> pd.DataFrame | None:
    import time
    for attempt in range(3):
        try:
            with _yf_lock:
                df = yf.download(
                    symbol, start=start, end=end,
                    progress=False, auto_adjust=True,
                    multi_level_index=False,
                )
            if df is None or df.empty:
                return None
            # Flatten MultiIndex columns (yfinance sometimes returns them anyway)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            # Deduplicate columns — keep only first occurrence of each name
            df = df.loc[:, ~df.columns.duplicated()]
            # Ensure each OHLCV column is a flat 1-D Series
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                if col in df.columns and isinstance(df[col], pd.DataFrame):
                    df[col] = df[col].iloc[:, 0]
            return df.dropna(subset=["Close"])
        except Exception:
            if attempt < 2:
                time.sleep(2 + attempt * 2)   # 2s, then 4s before retry
            continue
    return None


# ── Daily Price Data ──────────────────────────────────────────────────────────

def fetch_price_data(symbol: str, lookback_days: int = 400) -> pd.DataFrame | None:
    """
    Download OHLCV and attach all indicators.
    Needs 300+ bars for SMA200 + 52-week high calculation.
    Returns None if insufficient data.
    """
    end   = datetime.now()
    start = end - timedelta(days=lookback_days)
    df    = _download(symbol, start, end)

    if df is None or len(df) < 210:
        return None

    close  = pd.Series(df["Close"].values.flatten(),  index=df.index)
    high   = pd.Series(df["High"].values.flatten(),   index=df.index)
    low    = pd.Series(df["Low"].values.flatten(),    index=df.index)
    volume = pd.Series(df["Volume"].values.flatten(), index=df.index)

    df["sma_200"]         = close.rolling(200).mean()
    df["sma_50"]          = close.rolling(50).mean()
    df["ema_20"]          = close.ewm(span=20, adjust=False).mean()
    df["rsi_14"]          = _rsi(close, 14)
    df["atr_14"]          = _atr(high, low, close, 14)
    df["vol_sma_20"]      = volume.rolling(20).mean()

    # Prior 20-bar high (excludes current bar — for breakout check)
    df["highest_high_20"] = high.shift(1).rolling(20).max()

    # 5-bar swing low (recent support)
    df["swing_low_5"]     = low.rolling(5).min()

    # 52-week high (252 trading days, excludes current bar)
    df["high_52wk"]       = high.shift(1).rolling(252).max()

    # Volume direction: is recent volume declining? (for pullback quality)
    df["vol_5_avg"]       = volume.rolling(5).mean()

    return df


# ── Weekly Data ───────────────────────────────────────────────────────────────

def fetch_weekly_data(symbol: str, weeks: int = 80) -> pd.DataFrame | None:
    """
    Weekly OHLCV resampled from daily data.
    Returns weekly df with weekly SMA50 attached.
    """
    end   = datetime.now()
    start = end - timedelta(days=weeks * 7 + 30)
    df    = _download(symbol, start, end)

    if df is None or df.empty:
        return None

    # Resample to weekly (Friday close) — use explicit methods (pandas 2.x compat)
    rs = df.resample("W-FRI")
    weekly = pd.concat([
        rs["Open"].first().rename("Open"),
        rs["High"].max().rename("High"),
        rs["Low"].min().rename("Low"),
        rs["Close"].last().rename("Close"),
        rs["Volume"].sum().rename("Volume"),
    ], axis=1).dropna(subset=["Close"])

    if len(weekly) < 52:
        return None

    weekly["sma_50w"] = weekly["Close"].rolling(50).mean()
    return weekly


# ── Market Regime ─────────────────────────────────────────────────────────────

def fetch_market_regime(sma_period: int = 50) -> dict:
    """
    Fetch SPY and QQQ, check if their close is above their sma_period SMA.
    Returns a dict with ok/price/sma for each.
    """
    result = {
        "spy_ok": False, "spy_price": None, "spy_sma50": None,
        "qqq_ok": False, "qqq_price": None, "qqq_sma50": None,
    }

    for ticker, prefix in [("SPY", "spy"), ("QQQ", "qqq")]:
        end   = datetime.now()
        start = end - timedelta(days=(sma_period + 60) * 1.5)
        df    = _download(ticker, start, end)

        if df is None or len(df) < sma_period + 5:
            continue

        close = pd.Series(df["Close"].values.flatten(), index=df.index)
        sma   = close.rolling(sma_period).mean()

        price   = float(close.iloc[-1])
        sma_val = float(sma.iloc[-1]) if not pd.isna(sma.iloc[-1]) else None

        result[f"{prefix}_price"] = round(price, 2)
        result[f"{prefix}_sma50"] = round(sma_val, 2) if sma_val else None
        result[f"{prefix}_ok"]    = (sma_val is not None and price > sma_val)

    return result


# ── VIX ───────────────────────────────────────────────────────────────────────

def fetch_vix() -> float | None:
    """Fetch current VIX level from yfinance (^VIX)."""
    try:
        end   = datetime.now()
        start = end - timedelta(days=10)
        df    = _download("^VIX", start, end)

        if df is None or df.empty:
            return None

        return round(float(df["Close"].values.flatten()[-1]), 2)
    except Exception:
        return None


# ── Earnings Date ─────────────────────────────────────────────────────────────

def fetch_earnings_date(symbol: str) -> datetime | None:
    """
    Attempt to get the next earnings date from yfinance.
    Returns the next date, or None if unknown.
    """
    try:
        tk  = yf.Ticker(symbol)
        cal = tk.calendar

        if cal is None:
            return None

        # calendar can be a DataFrame or dict depending on yfinance version
        if isinstance(cal, pd.DataFrame):
            if "Earnings Date" in cal.index:
                val = cal.loc["Earnings Date"]
                dates = pd.to_datetime(val, errors="coerce")
                dates = [d for d in dates if pd.notna(d)]
                future = [d for d in dates if d.date() >= datetime.now().date()]
                return min(future).to_pydatetime() if future else None

        elif isinstance(cal, dict):
            raw = cal.get("Earnings Date", [])
            if raw:
                dates = pd.to_datetime(raw, errors="coerce")
                future = [d for d in dates if pd.notna(d) and d.date() >= datetime.now().date()]
                return min(future).to_pydatetime() if future else None

        return None

    except Exception:
        return None


# ── Relative Strength vs SPY ──────────────────────────────────────────────────

def compute_rs_score(
    sym_df:      pd.DataFrame,
    spy_df:      pd.DataFrame,
    lookback_3m: int = 63,
    lookback_6m: int = 126,
    w3m:         float = 0.4,
    w6m:         float = 0.6,
) -> float | None:
    """
    Composite relative strength score vs SPY.

    RS = w3m × (stock_3m_return - spy_3m_return)
       + w6m × (stock_6m_return - spy_6m_return)

    Higher = stronger than market.
    Raw score (not percentile). Percentile is computed in runner across the universe.
    """
    try:
        sym_close = pd.Series(sym_df["Close"].values.flatten(), index=sym_df.index)
        spy_close = pd.Series(spy_df["Close"].values.flatten(), index=spy_df.index)

        def _ret(series, lb):
            if len(series) < lb + 1:
                return None
            return float(series.iloc[-1]) / float(series.iloc[-(lb + 1)]) - 1

        sym_3m = _ret(sym_close, lookback_3m)
        sym_6m = _ret(sym_close, lookback_6m)
        spy_3m = _ret(spy_close, lookback_3m)
        spy_6m = _ret(spy_close, lookback_6m)

        if None in (sym_3m, sym_6m, spy_3m, spy_6m):
            return None

        rs = w3m * (sym_3m - spy_3m) + w6m * (sym_6m - spy_6m)
        return round(rs, 4)

    except Exception:
        return None


# ── Options Flow ──────────────────────────────────────────────────────────────

def fetch_options_data(symbol: str) -> dict | None:
    """
    Fetch 30-90 DTE options chain via yfinance.
    Returns call/put metrics, or None on failure.

    NOTE: EOD volume only — not real-time sweeps.
    """
    try:
        tk          = yf.Ticker(symbol)
        expirations = tk.options
        if not expirations:
            return None

        today = datetime.now().date()
        targets = [
            exp for exp in expirations
            if 30 <= (datetime.strptime(exp, "%Y-%m-%d").date() - today).days <= 90
        ]

        if not targets:
            return None

        info          = tk.fast_info
        current_price = getattr(info, "last_price", None) or getattr(info, "regular_market_price", None)

        total_call_vol = total_put_vol = otm_call_vol = 0
        total_call_oi  = total_put_oi  = 0

        for exp in targets:
            try:
                chain = tk.option_chain(exp)
            except Exception:
                continue

            calls = chain.calls
            puts  = chain.puts

            total_call_vol += calls["volume"].fillna(0).sum()
            total_put_vol  += puts["volume"].fillna(0).sum()
            total_call_oi  += calls["openInterest"].fillna(0).sum()
            total_put_oi   += puts["openInterest"].fillna(0).sum()

            if current_price:
                otm = calls[calls["strike"] > current_price]
                otm_call_vol += otm["volume"].fillna(0).sum()

        if total_put_vol == 0:
            cpr = float("inf") if total_call_vol > 0 else 0.0
        else:
            cpr = total_call_vol / total_put_vol

        oi_ratio = (total_call_oi / total_put_oi) if total_put_oi > 0 else 0.0
        otm_pct  = (otm_call_vol / total_call_vol) if total_call_vol > 0 else 0.0

        return {
            "call_vol":       int(total_call_vol),
            "put_vol":        int(total_put_vol),
            "call_put_ratio": round(cpr, 2),
            "call_oi_ratio":  round(oi_ratio, 2),
            "otm_call_pct":   round(otm_pct, 2),
            "expiries_used":  len(targets),
        }

    except Exception:
        return None
