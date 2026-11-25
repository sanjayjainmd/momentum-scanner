"""
scanner.py — 10-Layer Momentum Scanner
=======================================

Layer 0  — Market Regime (SPY + QQQ vs 50 SMA) — abort entire scan if failing
Layer 0b — VIX Gate — no breakouts > 25, no longs > 35
Layer 0c — Earnings Blackout — skip if earnings within N days
Layer 1  — Trend Filter (Close > SMA50 > SMA200, EMA20 slope positive)
Layer 1b — Weekly Trend (weekly Close > weekly SMA50)
Layer 1c — Relative Strength (stock must be top 30% of universe vs SPY)
Layer 2  — Setup Detection
           A. Pullback: RSI 40-55, near SMA50, quiet declining volume, no breakdown
           B. Breakout: above 20-day high, volume spike, near 52-week high preferred, no wick
Layer 3  — Volume + ATR Confirmation
Layer 4  — Options Flow (30-90 DTE call/put ratio)
Layer 5  — Risk / Reward >= 2:1 + Position Sizing + Expected Value
"""

import math
from datetime import datetime
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Layer 0 — Market Regime
# ─────────────────────────────────────────────────────────────────────────────

def check_market_regime(regime: dict, require_qqq: bool = True) -> tuple[bool, str]:
    """
    Returns (ok, message).
    If ok is False the entire scan should be aborted (no individual stock checks).
    """
    spy_ok = regime.get("spy_ok", False)
    qqq_ok = regime.get("qqq_ok", False)

    if not spy_ok:
        spy_p = regime.get("spy_price", 0)
        spy_s = regime.get("spy_sma50", 0)
        return False, f"SPY {spy_p:.2f} is BELOW its 50 SMA {spy_s:.2f} — bear market regime"

    if require_qqq and not qqq_ok:
        qqq_p = regime.get("qqq_price", 0)
        qqq_s = regime.get("qqq_sma50", 0)
        return False, f"QQQ {qqq_p:.2f} is BELOW its 50 SMA {qqq_s:.2f} — tech/growth under pressure"

    spy_p = regime.get("spy_price", 0)
    spy_s = regime.get("spy_sma50", 0)
    dist  = (spy_p - spy_s) / spy_s * 100 if spy_s else 0
    return True, f"Regime OK — SPY {dist:+.1f}% above 50 SMA"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 0b — VIX Gate
# ─────────────────────────────────────────────────────────────────────────────

def check_vix(vix: float | None, config) -> tuple[bool, str, set]:
    """
    Returns (continue, message, allowed_setups).
    allowed_setups: set of allowed setup strings ("Pullback", "Breakout").
    If continue=False, do not scan any stocks.
    """
    if vix is None:
        return True, "VIX unknown — proceeding with caution", {"Pullback", "Breakout"}

    no_trade_lvl    = config.VIX_NO_TRADE
    skip_bo_lvl     = config.VIX_SKIP_BREAKOUTS

    if vix >= no_trade_lvl:
        return False, f"VIX {vix:.1f} >= {no_trade_lvl} — extreme fear, no new longs", set()

    if vix >= skip_bo_lvl:
        return True, f"VIX {vix:.1f} >= {skip_bo_lvl} — elevated fear, pullbacks only", {"Pullback"}

    return True, f"VIX {vix:.1f} — normal conditions, both setups allowed", {"Pullback", "Breakout"}


# ─────────────────────────────────────────────────────────────────────────────
# Layer 0c — Earnings Blackout
# ─────────────────────────────────────────────────────────────────────────────

def check_earnings(earnings_date, blackout_days: int) -> tuple[bool, str]:
    """
    Returns (ok, msg).
    ok=False means skip this stock — earnings too close.
    """
    if earnings_date is None:
        return True, "Earnings date unknown (proceeding)"

    today = datetime.now().date()
    try:
        edate = earnings_date.date() if hasattr(earnings_date, "date") else earnings_date
        days  = (edate - today).days
    except Exception:
        return True, "Earnings date parse error"

    if 0 <= days <= blackout_days:
        return False, f"Earnings in {days} day{'s' if days != 1 else ''} ({edate}) — gap risk"

    return True, f"Earnings: {edate} ({days}d away)"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — Trend Filter
# ─────────────────────────────────────────────────────────────────────────────

def check_trend(df: pd.DataFrame) -> tuple[bool, str, list]:
    """
    All four must hold. Returns (ok, summary, rationale_bullets).
    rationale_bullets are positive "why this stock" bullets for the email.
    """
    row      = df.iloc[-1]
    close    = float(row["Close"])
    sma_50   = row["sma_50"]
    sma_200  = row["sma_200"]
    ema_now  = float(row["ema_20"]) if not pd.isna(row["ema_20"]) else None
    ema_5ago = float(df.iloc[-6]["ema_20"]) if len(df) >= 6 and not pd.isna(df.iloc[-6]["ema_20"]) else None

    if pd.isna(sma_200) or pd.isna(sma_50):
        return False, "Insufficient SMA data", []

    sma_50  = float(sma_50)
    sma_200 = float(sma_200)

    if close <= sma_200:
        return False, f"Close {close:.2f} <= SMA200 {sma_200:.2f}", []
    if sma_50 <= sma_200:
        return False, f"SMA50 {sma_50:.2f} <= SMA200 {sma_200:.2f} (downtrend)", []
    if close <= sma_50:
        return False, f"Close {close:.2f} <= SMA50 {sma_50:.2f}", []

    rationale = []
    dist200   = (close - sma_200) / sma_200 * 100
    dist50    = (close - sma_50) / sma_50 * 100
    rationale.append(f"Long-term uptrend: Close {dist200:+.1f}% above SMA200")
    rationale.append(f"SMA50 > SMA200 — golden cross alignment")

    if ema_now and ema_5ago:
        slope = ema_now - ema_5ago
        if slope <= 0:
            return False, f"EMA20 slope flat/negative ({slope:+.3f})", []
        rationale.append(f"EMA20 slope rising (+{slope:.2f}) — momentum intact")

    return True, f"Trend OK | +{dist200:.1f}% > SMA200 | +{dist50:.1f}% > SMA50", rationale


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1b — Weekly Trend
# ─────────────────────────────────────────────────────────────────────────────

def check_weekly_trend(weekly_df: pd.DataFrame | None) -> tuple[bool, str]:
    if weekly_df is None:
        return True, "Weekly data unavailable (skipping check)"

    row = weekly_df.iloc[-1]
    close  = float(row["Close"]) if not pd.isna(row["Close"]) else None
    sma50w = float(row["sma_50w"]) if not pd.isna(row["sma_50w"]) else None

    if close is None or sma50w is None:
        return True, "Weekly SMA50 not yet calculated"

    if close < sma50w:
        return False, f"Weekly close {close:.2f} < weekly SMA50 {sma50w:.2f} (bearish weekly)"

    dist = (close - sma50w) / sma50w * 100
    return True, f"Weekly trend OK — {dist:+.1f}% above weekly SMA50"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1c — Relative Strength Rank
# ─────────────────────────────────────────────────────────────────────────────

def check_rs_rank(rs_percentile: float | None, min_percentile: float) -> tuple[bool, str]:
    """
    rs_percentile: 0.0–1.0 (where this stock ranks in the universe).
    Returns ok=False if below the minimum percentile threshold.
    """
    if rs_percentile is None:
        return True, "RS rank unavailable (proceeding)"

    if rs_percentile < min_percentile:
        return False, f"RS rank {rs_percentile:.0%} < {min_percentile:.0%} threshold — weak relative strength"

    return True, f"RS rank {rs_percentile:.0%} — top {100 - rs_percentile*100:.0f}% of universe"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2A — Pullback Setup
# ─────────────────────────────────────────────────────────────────────────────

def check_pullback(df: pd.DataFrame, config) -> tuple[bool, str, list, list]:
    """
    Returns (ok, summary, rationale_bullets, warning_bullets).
    """
    row       = df.iloc[-1]
    close     = float(row["Close"])
    sma_50    = float(row["sma_50"])
    sma_200   = float(row["sma_200"])
    rsi       = row["rsi_14"]
    swing_lo  = row["swing_low_5"]
    vol       = float(row["Volume"])
    vol_sma   = float(row["vol_sma_20"]) if not pd.isna(row["vol_sma_20"]) else 0
    vol_5avg  = float(row["vol_5_avg"])  if not pd.isna(row.get("vol_5_avg", None) if "vol_5_avg" in row.index else None) else 0

    # Distance from SMA50
    dist = (close - sma_50) / sma_50
    if not (-config.PULLBACK_MAX_DIST <= dist <= config.PULLBACK_MAX_DIST):
        return False, f"Not near SMA50 (dist={dist:+.1%}, limit=±{config.PULLBACK_MAX_DIST:.0%})", [], []

    if close <= sma_200:
        return False, "Below SMA200 — breakdown not pullback", [], []

    if pd.isna(rsi):
        return False, "RSI unavailable", [], []

    rsi = float(rsi)
    if not (config.PULLBACK_RSI_MIN <= rsi <= config.PULLBACK_RSI_MAX):
        return False, f"RSI {rsi:.1f} not in [{config.PULLBACK_RSI_MIN}, {config.PULLBACK_RSI_MAX}]", [], []

    if not pd.isna(swing_lo) and close < float(swing_lo):
        return False, f"Breakdown: Close {close:.2f} < SwingLow {float(swing_lo):.2f}", [], []

    rationale = [
        f"Pullback to SMA50 ({dist:+.1%}) — low-risk entry in an uptrend",
        f"RSI {rsi:.0f} — mildly oversold, room to bounce",
        "Still above SMA200 — no structural damage to trend",
    ]
    warnings = []

    # Improvement 9: Quiet volume check
    if config.PULLBACK_QUIET_VOLUME:
        vol_ratio = vol / vol_sma if vol_sma > 0 else 0
        if vol_ratio > config.PULLBACK_MAX_VOL_MULT:
            # Not a hard fail — add warning
            warnings.append(f"Pullback volume elevated ({vol_ratio:.1f}x avg) — watch for distribution")
        else:
            rationale.append(f"Quiet pullback volume ({vol_ratio:.1f}x avg) — healthy consolidation")

    # Distribution check (hard fail): 3+ red candles with high volume
    recent = df.iloc[-5:]
    red    = recent[recent["Close"] < recent["Open"]]
    if len(red) >= 3 and vol_sma > 0:
        if red["Volume"].mean() > vol_sma * 1.5:
            return False, "Distribution: high-vol red candles — sellers in control", [], []

    return True, f"Pullback near SMA50 ({dist:+.1%}) | RSI={rsi:.0f}", rationale, warnings


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2B — Breakout Setup
# ─────────────────────────────────────────────────────────────────────────────

def check_breakout(df: pd.DataFrame, config) -> tuple[bool, str, list, list, bool]:
    """
    Returns (ok, summary, rationale_bullets, warning_bullets, near_52wk_high).
    near_52wk_high is used to adjust EV win-rate in email.
    """
    row         = df.iloc[-1]
    close       = float(row["Close"])
    high_bar    = float(row["High"])
    low_bar     = float(row["Low"])
    volume      = float(row["Volume"])
    vol_sma_20  = row["vol_sma_20"]
    highest_20  = row["highest_high_20"]
    high_52wk   = row.get("high_52wk") if "high_52wk" in row.index else None

    if pd.isna(highest_20):
        return False, "Insufficient lookback for breakout", [], [], False

    highest_20 = float(highest_20)

    if close <= highest_20:
        return False, f"No breakout: {close:.2f} <= 20d-high {highest_20:.2f}", [], [], False

    # Volume spike
    vol_ratio = volume / float(vol_sma_20) if not pd.isna(vol_sma_20) and float(vol_sma_20) > 0 else 0
    if vol_ratio < config.BREAKOUT_VOL_MULT:
        return False, f"Weak volume: {vol_ratio:.1f}x < {config.BREAKOUT_VOL_MULT}x required", [], [], False

    # Wick rejection: close must be in upper 50% of bar range
    bar_range = high_bar - low_bar
    if bar_range > 0:
        close_pos = (close - low_bar) / bar_range
        if close_pos < 0.5:
            return False, f"Wick rejection: close at {close_pos:.0%} of bar range", [], [], False

    # Improvement 8: 52-week high proximity
    near_52wk = False
    rationale = [
        f"Breakout above {highest_20:.2f} (20-day high) — momentum confirmed",
        f"Volume {vol_ratio:.1f}x average — institutional participation",
    ]
    warnings = []

    if config.BREAKOUT_52WK_CHECK and high_52wk is not None and not pd.isna(high_52wk):
        high_52wk   = float(high_52wk)
        prox        = (high_52wk - close) / high_52wk
        near_52wk   = prox <= config.BREAKOUT_52WK_PROX
        if near_52wk:
            rationale.append(f"Within {prox:.1%} of 52-week high — breakout from near-ATH base (strongest setups)")
        else:
            warnings.append(f"Not near 52-week high ({prox:.1%} away) — breakout in mid-range (lower win rate)")

    return True, f"Breakout above {highest_20:.2f} | Vol={vol_ratio:.1f}x", rationale, warnings, near_52wk


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — Volume + ATR Confirmation
# ─────────────────────────────────────────────────────────────────────────────

def check_volume_volatility(df: pd.DataFrame, setup_type: str, config) -> tuple[bool, str]:
    row      = df.iloc[-1]
    volume   = float(row["Volume"])
    vol_sma  = row["vol_sma_20"]
    atr_now  = row["atr_14"]
    atr_5ago = df.iloc[-6]["atr_14"] if len(df) >= 6 else None

    vol_ratio  = volume / float(vol_sma) if not pd.isna(vol_sma) and float(vol_sma) > 0 else 0
    atr_rising = (
        not pd.isna(atr_5ago)
        and not pd.isna(atr_now)
        and float(atr_now) > float(atr_5ago)
    )

    if setup_type == "Breakout":
        if vol_ratio < config.BREAKOUT_VOL_MULT:
            return False, f"Breakout vol {vol_ratio:.1f}x < {config.BREAKOUT_VOL_MULT}x"
        if not atr_rising:
            return False, "ATR not expanding on breakout"

    elif setup_type == "Pullback":
        bar_is_red = float(row["Close"]) < float(row["Open"])
        if bar_is_red and vol_ratio > 2.0:
            return False, f"High-vol red bar ({vol_ratio:.1f}x) on pullback — distribution risk"

    atr_tag = "ATR rising" if atr_rising else "ATR flat"
    return True, f"Vol={vol_ratio:.1f}x | {atr_tag}"


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — Options Flow
# ─────────────────────────────────────────────────────────────────────────────

def check_options_flow(options: dict | None, config) -> tuple[bool | None, str]:
    if options is None:
        return None, "No options data available"

    cpr     = options.get("call_put_ratio", 0)
    otm_pct = options.get("otm_call_pct", 0)
    min_cpr = config.MIN_CALL_PUT_RATIO

    if cpr == float("inf"):
        return True, f"All calls, zero puts — maximum bullish options signal | OTM={otm_pct:.0%}"

    if cpr < min_cpr:
        return False, f"C/P ratio {cpr:.2f} < {min_cpr} (bearish lean — smart money not buying calls)"

    return True, f"Bullish options flow — C/P={cpr:.2f} | OTM calls={otm_pct:.0%}"


# ─────────────────────────────────────────────────────────────────────────────
# Risk / Reward + Position Size
# ─────────────────────────────────────────────────────────────────────────────

def compute_risk_reward(df: pd.DataFrame, setup_type: str, config) -> tuple:
    """
    Returns (stop, target_1, target_2, rr_ratio, risk_per_share).
    """
    row        = df.iloc[-1]
    close      = float(row["Close"])
    sma_50     = float(row["sma_50"])
    atr        = float(row["atr_14"]) if not pd.isna(row["atr_14"]) else close * 0.02
    highest_20 = float(row["highest_high_20"]) if not pd.isna(row["highest_high_20"]) else close * 0.95

    atr_stop = close - config.STOP_ATR_MULT * atr

    if setup_type == "Pullback":
        structure_stop = sma_50 * 0.99          # 1% below SMA50
    else:
        structure_stop = highest_20 * 0.99      # 1% below breakout level

    stop          = max(structure_stop, atr_stop)
    risk_per_sh   = close - stop

    if risk_per_sh <= 0:
        return stop, None, None, 0.0, 0.0

    t1 = close * (1 + config.TARGET_1_PCT)
    t2 = close * (1 + config.TARGET_2_PCT)
    rr = (t1 - close) / risk_per_sh

    return round(stop, 2), round(t1, 2), round(t2, 2), round(rr, 2), round(risk_per_sh, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point — scan one symbol
# ─────────────────────────────────────────────────────────────────────────────

def scan_symbol(
    symbol:        str,
    df:            pd.DataFrame,
    weekly_df:     pd.DataFrame | None,
    options:       dict | None,
    earnings_date,
    rs_percentile: float | None,
    allowed_setups: set,
    config,
) -> tuple[dict | None, str | None]:
    """
    Run all 10 layers for a single symbol.

    Returns:
      (result_dict, None)  — passes all layers
      (None, reason_str)   — filtered out
    """

    # ── Layer 0c: Earnings ─────────────────────────────────────────────────────
    earn_ok, earn_msg = check_earnings(earnings_date, config.EARNINGS_BLACKOUT_DAYS)
    if not earn_ok:
        return None, f"[Earnings] {earn_msg}"

    # ── Layer 1: Trend ─────────────────────────────────────────────────────────
    trend_ok, trend_msg, trend_rationale = check_trend(df)
    if not trend_ok:
        return None, f"[Trend] {trend_msg}"

    # ── Layer 1b: Weekly Trend ─────────────────────────────────────────────────
    if config.WEEKLY_TREND_CHECK:
        wk_ok, wk_msg = check_weekly_trend(weekly_df)
        if not wk_ok:
            return None, f"[Weekly] {wk_msg}"
    else:
        wk_msg = "Weekly check disabled"

    # ── Layer 1c: Relative Strength ────────────────────────────────────────────
    rs_ok, rs_msg = check_rs_rank(rs_percentile, config.RS_MIN_PERCENTILE)
    if not rs_ok:
        return None, f"[RS] {rs_msg}"

    # ── Layer 2: Setup ─────────────────────────────────────────────────────────
    pb_ok,  pb_msg,  pb_rat,  pb_warn           = check_pullback(df, config)
    bo_ok,  bo_msg,  bo_rat,  bo_warn, near_52wk = check_breakout(df, config)

    # Apply VIX allowed_setups filter
    setup_type  = None
    setup_msg   = ""
    rationale   = []
    warnings    = []

    if pb_ok and "Pullback" in allowed_setups:
        setup_type = "Pullback"
        setup_msg  = pb_msg
        rationale  = pb_rat
        warnings   = pb_warn
    elif bo_ok and "Breakout" in allowed_setups:
        setup_type = "Breakout"
        setup_msg  = bo_msg
        rationale  = bo_rat
        warnings   = bo_warn
    else:
        return None, f"[Setup] PB: {pb_msg} | BO: {bo_msg}"

    # ── Layer 3: Volume / ATR ──────────────────────────────────────────────────
    vol_ok, vol_msg = check_volume_volatility(df, setup_type, config)
    if not vol_ok:
        return None, f"[Vol/ATR] {vol_msg}"

    # ── Layer 4: Options ───────────────────────────────────────────────────────
    opt_ok, opt_msg = check_options_flow(options, config)
    if opt_ok is False and config.REQUIRE_OPTIONS:
        return None, f"[Options] {opt_msg}"

    # ── Risk / Reward ──────────────────────────────────────────────────────────
    stop, t1, t2, rr, risk_per_sh = compute_risk_reward(df, setup_type, config)
    if rr < config.MIN_RISK_REWARD:
        return None, f"[R:R] {rr:.1f} < {config.MIN_RISK_REWARD} | Stop={stop:.2f} T1={t1:.2f}"

    # ── Position Sizing ────────────────────────────────────────────────────────
    from alpaca_client import compute_position_size
    row     = df.iloc[-1]
    price   = float(row["Close"])
    shares  = compute_position_size(
        entry            = price,
        stop             = stop,
        account_size     = config.ACCOUNT_SIZE,
        risk_pct         = config.RISK_PER_TRADE_PCT,
        max_position_pct = config.MAX_POSITION_PCT,
    )

    # Combine rationale — trend bullets + setup bullets
    all_rationale = trend_rationale + rationale
    if rs_percentile is not None:
        all_rationale.append(f"Top {100 - rs_percentile*100:.0f}% relative strength vs SPY — leading stock")
    if config.WEEKLY_TREND_CHECK:
        all_rationale.append(wk_msg)
    all_rationale.append(f"{vol_msg}")

    vol_sma = float(row["vol_sma_20"]) if not pd.isna(row["vol_sma_20"]) else 0
    vol_ratio = float(row["Volume"]) / vol_sma if vol_sma > 0 else 0

    return {
        # Identity
        "symbol":       symbol,
        "setup":        setup_type,
        "setup_msg":    setup_msg,
        "near_52wk_high": near_52wk if setup_type == "Breakout" else False,
        # Price levels
        "price":        round(price, 2),
        "sma_50":       round(float(row["sma_50"]), 2),
        "sma_200":      round(float(row["sma_200"]), 2),
        "ema_20":       round(float(row["ema_20"]), 2),
        # Indicators
        "rsi":          round(float(row["rsi_14"]), 1) if not pd.isna(row["rsi_14"]) else None,
        "atr":          round(float(row["atr_14"]), 2)  if not pd.isna(row["atr_14"]) else None,
        "vol_ratio":    round(vol_ratio, 1),
        "vol_msg":      vol_msg,
        # Risk levels
        "stop":         stop,
        "target_1":     t1,
        "target_2":     t2,
        "rr_ratio":     rr,
        "risk_per_share": risk_per_sh,
        # Position
        "shares":       shares,
        "risk_dollars": shares * risk_per_sh,
        # Options
        "options_ok":   opt_ok,
        "options_msg":  opt_msg,
        "call_put":     options.get("call_put_ratio") if options else None,
        "otm_pct":      options.get("otm_call_pct")   if options else None,
        # RS
        "rs_percentile": rs_percentile,
        "rs_rank":      rs_percentile,
        # Email content
        "rationale":    all_rationale,
        "warnings":     warnings,
        # Earnings note
        "earnings_note": earn_msg,
    }, None
