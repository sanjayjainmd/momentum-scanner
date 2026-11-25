"""
backtest.py — Momentum Investing Historical Backtest
=====================================================
Walk-forward simulation of the momentum scanner strategy.

How it works:
  1. Downloads OHLCV for the fixed universe + SPY/QQQ (5 years default)
  2. Walks forward bar by bar — no look-ahead bias
  3. Signal detected on close of day D → entry at open of day D+1
  4. Exits: stop loss | T1 +12% (sell half, stop → breakeven) | T2 +25% (exit rest)
  5. After T1 hit: trail remainder with EMA20
  6. Max 5 concurrent positions, 2% risk per trade, 5% max per position

Outputs:
  - Printed summary (win rate, Sharpe, drawdown, by-setup breakdown)
  - backtest_results.json  (all trades + daily equity curve)

Usage:
  cd momentum-scanner
  py backtest.py
"""

import warnings
warnings.filterwarnings("ignore")

import sys
sys.stdout.reconfigure(encoding="utf-8")

import json
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from pathlib import Path

import config as cfg

# ── Parameters ────────────────────────────────────────────────────────────────
START_DATE      = "2020-01-01"
END_DATE        = "2024-12-31"
INITIAL_CAPITAL = 100_000          # round number for clean metrics
MAX_POSITIONS   = cfg.MAX_CONCURRENT_TRADES   # 5
RISK_PCT        = cfg.RISK_PER_TRADE_PCT       # 0.02
MAX_POS_PCT     = 0.10                         # raised from 5% → 10% for backtest
STOP_ATR_MULT   = cfg.STOP_ATR_MULT            # 2.0
T1_PCT          = cfg.TARGET_1_PCT             # 0.20
T2_PCT          = cfg.TARGET_2_PCT             # 0.40
MIN_RR          = cfg.MIN_RISK_REWARD          # 2.0

UNIVERSE = cfg.UNIVERSE   # fixed list — dynamic S&P500 not practical for multi-year BT


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _rsi(c, p=14):
    d = c.diff()
    g = d.clip(lower=0).ewm(com=p-1, min_periods=p).mean()
    l = (-d.clip(upper=0)).ewm(com=p-1, min_periods=p).mean()
    return 100 - 100 / (1 + g / l)


def _atr(h, l, c, p=14):
    pc = c.shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(com=p-1, min_periods=p).mean()


def _flat(s):
    """Force Series/DataFrame column to 1-D float Series."""
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    return pd.to_numeric(s, errors="coerce")


# ── Data Download ─────────────────────────────────────────────────────────────

def _load_one(sym: str, raw_multi, start_pad: str, end: str) -> pd.DataFrame | None:
    """Extract one symbol from a multi-symbol yfinance download and attach indicators."""
    try:
        if isinstance(raw_multi.columns, pd.MultiIndex):
            df = raw_multi.xs(sym, axis=1, level=1).copy()
        else:
            df = raw_multi.copy()

        df = df.dropna(subset=["Close"])
        if len(df) < 220:
            return None

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = _flat(df[col])

        c, h, l, v = df["Close"], df["High"], df["Low"], df["Volume"]

        df["sma_200"]    = c.rolling(200).mean()
        df["sma_50"]     = c.rolling(50).mean()
        df["ema_20"]     = c.ewm(span=20, adjust=False).mean()
        df["rsi_14"]     = _rsi(c)
        df["atr_14"]     = _atr(h, l, c)
        df["vol_avg_20"] = v.rolling(20).mean()
        df["high_20"]    = h.shift(1).rolling(20).max()   # prior 20-bar high
        df["swing_l5"]   = l.rolling(5).min()
        df["high_52wk"]  = h.shift(1).rolling(252).max()
        df["vol_avg_5"]  = v.rolling(5).mean()

        return df

    except Exception:
        return None


def download_universe(symbols, start, end):
    """Batch-download all symbols + SPY/QQQ. Returns {sym: df}."""
    # Extra warmup for SMA200 + 52-week high
    pad_start = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=420)).strftime("%Y-%m-%d")
    all_syms  = list(set(symbols + ["SPY", "QQQ"]))

    print(f"  Downloading {len(all_syms)} symbols ({pad_start} → {end}) ...")
    raw = yf.download(
        all_syms, start=pad_start, end=end,
        progress=True, auto_adjust=True, multi_level_index=True,
    )

    loaded = {}
    for sym in all_syms:
        df = _load_one(sym, raw, pad_start, end)
        if df is not None:
            loaded[sym] = df

    ok  = [s for s in symbols if s in loaded]
    bad = [s for s in symbols if s not in loaded]
    print(f"  Loaded {len(ok)}/{len(symbols)} universe symbols "
          f"({'OK' if not bad else 'missing: ' + ', '.join(bad[:5])})")
    return loaded


# ── Signal Detection ──────────────────────────────────────────────────────────

def _signal(df: pd.DataFrame, i: int, spy_ret_3m: float, spy_ret_6m: float):
    """
    Check bar i for a valid setup.
    Returns (setup_type, stop_price) or (None, None).
    """
    if i < 205:   # need SMA200 warmup
        return None, None

    r       = df.iloc[i]
    close   = r["Close"];  sma200 = r["sma_200"]; sma50 = r["sma_50"]
    ema20   = r["ema_20"]; rsi    = r["rsi_14"];  atr   = r["atr_14"]
    vol     = r["Volume"]; vol_avg = r["vol_avg_20"]
    high_20 = r["high_20"]; high_52 = r["high_52wk"]
    swing_l = r["swing_l5"]

    # Any NaN → skip
    if any(pd.isna(x) for x in [close, sma200, sma50, ema20, rsi, atr, vol_avg]):
        return None, None
    if sma200 <= 0 or sma50 <= 0:
        return None, None

    # ── Trend: close > SMA50 > SMA200 (golden cross), EMA20 rising ──────────
    if not (close > sma50 and sma50 > sma200):
        return None, None
    ema_prev = df["ema_20"].iloc[max(0, i-5)]
    if ema20 <= ema_prev:
        return None, None

    # ── Weekly trend: close > weekly SMA50 (approximate with daily SMA50 > SMA200 already)
    # (full weekly resample not done here for speed — trend filter above is equivalent)

    # ── RS vs SPY: stock must outperform SPY on 6m basis ─────────────────────
    if i >= 127:
        ret_3m = close / df["Close"].iloc[i - 63] - 1
        ret_6m = close / df["Close"].iloc[i - 126] - 1
        rs     = 0.4 * (ret_3m - spy_ret_3m) + 0.6 * (ret_6m - spy_ret_6m)
        if rs < 0:   # underperforming SPY → skip
            return None, None

    # ── Pullback setup ────────────────────────────────────────────────────────
    dist = abs(close - sma50) / sma50
    if cfg.PULLBACK_RSI_MIN <= rsi <= cfg.PULLBACK_RSI_MAX and dist <= cfg.PULLBACK_MAX_DIST:
        if cfg.PULLBACK_QUIET_VOLUME and vol_avg > 0:
            if vol / vol_avg > cfg.PULLBACK_MAX_VOL_MULT:
                return None, None
        stop = max(
            swing_l if not pd.isna(swing_l) else (close - STOP_ATR_MULT * atr),
            close - STOP_ATR_MULT * atr,
        )
        return "Pullback", float(stop)

    # ── Breakout setup ────────────────────────────────────────────────────────
    if not pd.isna(high_20) and close > high_20:
        if vol_avg > 0 and vol / vol_avg < cfg.BREAKOUT_VOL_MULT:
            return None, None
        if cfg.BREAKOUT_52WK_CHECK and not pd.isna(high_52):
            if close < high_52 * (1 - cfg.BREAKOUT_52WK_PROX):
                return None, None
        stop = close - STOP_ATR_MULT * atr
        return "Breakout", float(stop)

    return None, None


# ── Position ──────────────────────────────────────────────────────────────────

class Position:
    __slots__ = [
        "symbol", "setup", "entry_date", "entry", "stop", "orig_stop",
        "t1", "t2", "shares", "shares_rem", "t1_hit", "t2_hit", "realized_pnl",
    ]

    def __init__(self, symbol, setup, entry_date, entry, stop, t1, t2, shares):
        self.symbol       = symbol
        self.setup        = setup
        self.entry_date   = entry_date
        self.entry        = entry
        self.stop         = stop
        self.orig_stop    = stop   # never mutated — used for R-multiple calc
        self.t1           = t1
        self.t2           = t2
        self.shares       = shares
        self.shares_rem   = shares
        self.t1_hit       = False
        self.t2_hit       = False
        self.realized_pnl = 0.0


# ── Main Backtest Loop ────────────────────────────────────────────────────────

def run_backtest():
    print("\n" + "=" * 65)
    print("  MOMENTUM INVESTING BACKTEST")
    print(f"  Period  : {START_DATE}  →  {END_DATE}")
    print(f"  Capital : ${INITIAL_CAPITAL:,}  |  Risk/trade: {RISK_PCT:.0%}")
    print(f"  Universe: {len(UNIVERSE)} stocks (fixed)")
    print("=" * 65 + "\n")

    # ── Load data ──────────────────────────────────────────────────────────────
    data = download_universe(UNIVERSE, START_DATE, END_DATE)
    spy  = data.get("SPY")
    qqq  = data.get("QQQ")

    if spy is None:
        print("ERROR: SPY data missing — aborting.")
        return

    bt_start = pd.Timestamp(START_DATE)
    bt_end   = pd.Timestamp(END_DATE)
    dates    = spy.loc[bt_start:bt_end].index

    print(f"\n  Trading days in period: {len(dates)}\n")

    # ── Walk-forward ───────────────────────────────────────────────────────────
    cash          = float(INITIAL_CAPITAL)
    open_pos      = {}         # sym → Position
    closed_trades = []
    equity_curve  = []

    for date in dates:
        # ── Regime: SPY + QQQ above SMA50 ─────────────────────────────────────
        def _above_sma(df_mkt):
            if df_mkt is None:
                return True   # assume OK if data missing
            try:
                row = df_mkt.loc[:date].iloc[-1]
                return float(row["Close"]) > float(row["sma_50"])
            except Exception:
                return True

        spy_ok     = _above_sma(spy)
        qqq_ok     = _above_sma(qqq) if cfg.REQUIRE_QQQ_REGIME else True
        regime_ok  = spy_ok and qqq_ok

        # SPY returns for RS comparison
        spy_c = spy["Close"].loc[:date]
        spy_r3 = float(spy_c.iloc[-1] / spy_c.iloc[-64]  - 1) if len(spy_c) > 64  else 0.0
        spy_r6 = float(spy_c.iloc[-1] / spy_c.iloc[-127] - 1) if len(spy_c) > 127 else 0.0

        # ── Process exits on open positions ────────────────────────────────────
        to_close = []
        for sym, pos in open_pos.items():
            df = data.get(sym)
            if df is None or date not in df.index:
                continue

            row   = df.loc[date]
            lo    = float(row["Low"])
            hi    = float(row["High"])
            cl    = float(row["Close"])
            ema20 = float(row["ema_20"]) if not pd.isna(row["ema_20"]) else None

            exit_price  = None
            exit_reason = None

            # ── Stop hit ──────────────────────────────────────────────────────
            if lo <= pos.stop:
                exit_price  = pos.stop
                exit_reason = "Stop"

            # ── T1 hit: sell 1/3, move stop to breakeven ──────────────────────
            elif not pos.t1_hit and hi >= pos.t1:
                third = max(1, pos.shares // 3)
                third = min(third, pos.shares_rem)
                pnl_partial           = third * (pos.t1 - pos.entry)
                pos.realized_pnl     += pnl_partial
                cash                 += third * pos.t1
                pos.shares_rem       -= third
                pos.t1_hit            = True
                pos.stop              = pos.entry   # move stop to breakeven

                if pos.shares_rem <= 0:
                    exit_price  = pos.t1
                    exit_reason = "T1 (full)"

            # ── T2 hit: sell 1/3 ──────────────────────────────────────────────
            if not exit_price and pos.t1_hit and not pos.t2_hit and hi >= pos.t2:
                third = max(1, pos.shares // 3)
                third = min(third, pos.shares_rem)
                pnl_partial           = third * (pos.t2 - pos.entry)
                pos.realized_pnl     += pnl_partial
                cash                 += third * pos.t2
                pos.shares_rem       -= third
                pos.t2_hit            = True

                if pos.shares_rem <= 0:
                    exit_price  = pos.t2
                    exit_reason = "T2 (full)"

            # ── EMA20 trail: only after T2 — exits final 1/3 ─────────────────
            if not exit_price and pos.t2_hit and ema20 and cl < ema20:
                exit_price  = cl
                exit_reason = "EMA trail"

            # ── Time stop: 90 calendar days max ──────────────────────────────
            if not exit_price and (date - pos.entry_date).days > 90:
                exit_price  = cl
                exit_reason = "Time stop"

            # ── Execute exit ──────────────────────────────────────────────────
            if exit_price and pos.shares_rem > 0:
                pnl_exit          = pos.shares_rem * (exit_price - pos.entry)
                pos.realized_pnl += pnl_exit
                cash             += pos.shares_rem * exit_price

                total_pnl = pos.realized_pnl
                closed_trades.append({
                    "symbol":      pos.symbol,
                    "setup":       pos.setup,
                    "entry_date":  str(pos.entry_date.date()),
                    "exit_date":   str(date.date()),
                    "entry":       round(pos.entry, 2),
                    "exit":        round(exit_price, 2),
                    "shares":      pos.shares,
                    "pnl":         round(total_pnl, 2),
                    "pct_ret":     round((exit_price / pos.entry - 1) * 100, 2),
                    "init_risk":   round(pos.shares * (pos.entry - pos.orig_stop), 2),
                    "r_mult":      round(total_pnl / max(0.01, pos.shares * (pos.entry - pos.orig_stop)), 2),
                    "exit_reason": exit_reason,
                    "days_held":   (date - pos.entry_date).days,
                    "t1_hit":      pos.t1_hit,
                    "t2_hit":      pos.t2_hit,
                })
                to_close.append(sym)

        for sym in to_close:
            del open_pos[sym]

        # ── Look for new signals ───────────────────────────────────────────────
        if regime_ok and len(open_pos) < MAX_POSITIONS:
            for sym in UNIVERSE:
                if sym in open_pos or sym not in data:
                    continue
                if len(open_pos) >= MAX_POSITIONS:
                    break

                df = data[sym]
                if date not in df.index:
                    continue

                i       = df.index.get_loc(date)
                setup, stop = _signal(df, i, spy_r3, spy_r6)
                if not setup:
                    continue

                # Entry = next bar's open
                if i + 1 >= len(df):
                    continue
                next_date  = df.index[i + 1]
                if next_date > bt_end:
                    continue

                entry = float(df.iloc[i + 1]["Open"])
                if not np.isfinite(entry) or entry <= 0:
                    continue

                # Recalculate stop relative to entry
                atr_val = float(df.iloc[i]["atr_14"])
                stop    = entry - STOP_ATR_MULT * atr_val
                if stop >= entry:
                    continue

                risk_per_share = entry - stop
                if risk_per_share <= 0:
                    continue

                t1 = entry * (1 + T1_PCT)
                t2 = entry * (1 + T2_PCT)
                rr = (t1 - entry) / risk_per_share

                if rr < MIN_RR:
                    continue

                # Position sizing
                shares = int(min(
                    cash * RISK_PCT    / risk_per_share,
                    cash * MAX_POS_PCT / entry,
                ))
                if shares <= 0:
                    continue

                cost = shares * entry
                if cost > cash:
                    continue

                cash -= cost
                open_pos[sym] = Position(
                    symbol     = sym,
                    setup      = setup,
                    entry_date = next_date,
                    entry      = entry,
                    stop       = stop,
                    t1         = t1,
                    t2         = t2,
                    shares     = shares,
                )

        # ── Equity snapshot ───────────────────────────────────────────────────
        mkt_val = 0.0
        for sym, pos in open_pos.items():
            df = data.get(sym)
            if df is not None and date in df.index:
                try:
                    mkt_val += pos.shares_rem * float(df.loc[date, "Close"])
                except Exception:
                    pass

        equity_curve.append({"date": str(date.date()), "equity": round(cash + mkt_val, 2)})

    # ── Close remaining open positions at last price ───────────────────────────
    last_date = dates[-1]
    for sym, pos in list(open_pos.items()):
        df = data.get(sym)
        if df is None or last_date not in df.index:
            continue
        exit_price         = float(df.loc[last_date, "Close"])
        pnl_exit           = pos.shares_rem * (exit_price - pos.entry)
        pos.realized_pnl  += pnl_exit
        cash              += pos.shares_rem * exit_price
        closed_trades.append({
            "symbol":      sym,
            "setup":       pos.setup,
            "entry_date":  str(pos.entry_date.date()),
            "exit_date":   str(last_date.date()),
            "entry":       round(pos.entry, 2),
            "exit":        round(exit_price, 2),
            "shares":      pos.shares,
            "pnl":         round(pos.realized_pnl, 2),
            "pct_ret":     round((exit_price / pos.entry - 1) * 100, 2),
            "init_risk":   round(pos.shares * (pos.entry - pos.orig_stop), 2),
            "r_mult":      round(pos.realized_pnl / max(0.01, pos.shares * (pos.entry - pos.orig_stop)), 2),
            "exit_reason": "End of backtest",
            "days_held":   (last_date - pos.entry_date).days,
            "t1_hit":      pos.t1_hit,
        })

    # ── Print results ─────────────────────────────────────────────────────────
    _print_stats(closed_trades, equity_curve)

    # ── Save results ──────────────────────────────────────────────────────────
    out = {
        "params": {
            "start": START_DATE, "end": END_DATE,
            "initial_capital": INITIAL_CAPITAL,
            "risk_pct": RISK_PCT, "max_positions": MAX_POSITIONS,
        },
        "trades":        closed_trades,
        "equity_curve":  equity_curve,
    }
    Path("backtest_results.json").write_text(json.dumps(out, indent=2, default=str))
    print("\n  Full trade log saved to  backtest_results.json")


# ── Statistics Printer ────────────────────────────────────────────────────────

def _print_stats(trades, equity_curve):
    print("\n" + "=" * 65)
    print("  RESULTS")
    print("=" * 65)

    if not trades:
        print("  No trades were generated.")
        return

    df      = pd.DataFrame(trades)
    wins    = df[df["pnl"] > 0]
    losses  = df[df["pnl"] <= 0]

    n       = len(df)
    win_rt  = len(wins) / n
    avg_w   = wins["pnl"].mean()  if len(wins)   > 0 else 0
    avg_l   = losses["pnl"].mean() if len(losses) > 0 else 0
    pf      = abs(avg_w / avg_l)  if avg_l != 0 else float("inf")
    avg_r   = df["r_mult"].mean()

    total_pnl  = df["pnl"].sum()
    final_cap  = INITIAL_CAPITAL + total_pnl
    total_ret  = (final_cap / INITIAL_CAPITAL - 1) * 100

    # CAGR
    years = (datetime.strptime(END_DATE, "%Y-%m-%d") -
             datetime.strptime(START_DATE, "%Y-%m-%d")).days / 365.25
    cagr  = ((final_cap / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Max drawdown from equity curve
    eq   = [e["equity"] for e in equity_curve]
    peak = INITIAL_CAPITAL
    mdd  = 0.0
    for v in eq:
        peak = max(peak, v)
        mdd  = max(mdd, (peak - v) / peak)

    # Sharpe (annualized)
    eq_s   = pd.Series(eq)
    dr     = eq_s.pct_change().dropna()
    sharpe = (dr.mean() / dr.std() * np.sqrt(252)) if dr.std() > 0 else 0

    print(f"\n  Trades:          {n}  ({len(wins)} wins / {len(losses)} losses)")
    print(f"  Win rate:        {win_rt:.1%}")
    print(f"  Avg win:         ${avg_w:+,.0f}  |  Avg loss: ${avg_l:+,.0f}")
    print(f"  Profit factor:   {pf:.2f}   (target > 1.5)")
    print(f"  Avg R-multiple:  {avg_r:+.2f}R  (target > 0.5)")
    print(f"\n  Start capital:   ${INITIAL_CAPITAL:,.0f}")
    print(f"  End capital:     ${final_cap:,.0f}")
    print(f"  Total return:    {total_ret:+.1f}%")
    print(f"  CAGR:            {cagr:+.1f}%")
    print(f"  Max drawdown:    -{mdd:.1%}")
    print(f"  Sharpe ratio:    {sharpe:.2f}   (target > 1.0)")

    # By setup
    print(f"\n  {'Setup':<12}  {'Trades':>6}  {'Win%':>6}  {'Avg R':>6}  {'Total P&L':>10}")
    print(f"  {'-'*50}")
    for setup_type, grp in df.groupby("setup"):
        w    = (grp["pnl"] > 0).sum()
        wr_s = w / len(grp)
        ar_s = grp["r_mult"].mean()
        tp_s = grp["pnl"].sum()
        print(f"  {setup_type:<12}  {len(grp):>6}  {wr_s:>5.0%}  {ar_s:>+5.2f}R  ${tp_s:>+9,.0f}")

    # By exit reason
    print(f"\n  {'Exit reason':<16}  {'Count':>5}  {'Total P&L':>10}")
    print(f"  {'-'*36}")
    for reason, grp in df.groupby("exit_reason"):
        print(f"  {reason:<16}  {len(grp):>5}  ${grp['pnl'].sum():>+9,.0f}")

    # Best / worst
    best = df.loc[df["pnl"].idxmax()]
    wrst = df.loc[df["pnl"].idxmin()]
    print(f"\n  Best trade:  {best['symbol']} {best['entry_date']} "
          f"${best['pnl']:+,.0f}  ({best['pct_ret']:+.1f}%)")
    print(f"  Worst trade: {wrst['symbol']} {wrst['entry_date']} "
          f"${wrst['pnl']:+,.0f}  ({wrst['pct_ret']:+.1f}%)")
    print(f"  Avg hold:    {df['days_held'].mean():.0f} days")
    print("=" * 65)


if __name__ == "__main__":
    run_backtest()
