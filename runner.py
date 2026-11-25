"""
runner.py — Momentum Investing Scanner Entry Point
===================================================
Full 10-layer pipeline:

  Pass 1  — Market regime check (SPY + QQQ vs 50 SMA)
  Pass 2  — VIX check
  Pass 3  — Parallel data fetch for all stocks
            (price, weekly, options, earnings)
  Pass 4  — Compute RS scores, rank vs universe
  Pass 5  — Run scanner (10 layers) per stock
  Pass 6  — Send HTML email with results + EV + approve/reject links
  Pass 7  — Start IBKR approval server (stays alive for link clicks)

Usage:
  cd momentum-scanner
  py runner.py

Requirements:
  pip install yfinance ib_insync flask pyngrok
"""

import os
import sys
import time
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import config as cfg
from universe_builder import build_universe
from data_fetcher import (
    fetch_price_data,
    fetch_weekly_data,
    fetch_options_data,
    fetch_earnings_date,
    fetch_market_regime,
    fetch_vix,
    compute_rs_score,
)
from scanner import (
    check_market_regime,
    check_vix,
    scan_symbol,
)
from approval_server import start_server
from email_reporter  import build_and_send

import yfinance as yf
from datetime import timedelta


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_spy_df():
    """Download SPY for RS computation (400 days)."""
    import warnings
    warnings.filterwarnings("ignore")
    end   = datetime.now()
    start = end - timedelta(days=400)
    try:
        df = yf.download("SPY", start=start, end=end,
                         progress=False, auto_adjust=True, multi_level_column=False)
        if isinstance(df.columns, yf.core.utils.pd.core.indexes.multi.MultiIndex if hasattr(yf, "core") else type(df.columns)):
            df.columns = df.columns.get_level_values(0)
        return df.dropna(subset=["Close"])
    except Exception:
        # Simpler fallback
        import pandas as pd
        try:
            df = yf.download("SPY", start=start, end=end, progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df.dropna(subset=["Close"])
        except Exception:
            return None


def _fetch_all_stock_data(symbol: str):
    """
    Fetch all data for a single symbol.
    Returns (symbol, price_df, weekly_df, options, earnings_date).
    """
    price   = fetch_price_data(symbol, lookback_days=cfg.PRICE_LOOKBACK_DAYS)
    weekly  = fetch_weekly_data(symbol)
    options = fetch_options_data(symbol)
    earn    = fetch_earnings_date(symbol)
    return symbol, price, weekly, options, earn


def _percentile_rank(scores: dict) -> dict:
    """
    Given {symbol: raw_rs_score}, return {symbol: percentile_0_to_1}.
    None scores map to 0.0.
    """
    symbols = list(scores.keys())
    vals    = [scores[s] if scores[s] is not None else -999 for s in symbols]
    sorted_vals = sorted(set(vals))
    n       = len(sorted_vals)

    rank_map = {}
    for s, v in zip(symbols, vals):
        if v == -999:
            rank_map[s] = None
        else:
            idx         = sorted_vals.index(v)
            rank_map[s] = round(idx / (n - 1), 3) if n > 1 else 0.5

    return rank_map


def _save_results(results: list):
    path = Path(__file__).parent / cfg.RESULTS_FILE
    try:
        path.write_text(json.dumps(results, indent=2, default=str))
        print(f"  [Results] Saved to {path.name}")
    except Exception as e:
        print(f"  [Results] Save failed: {e}")


# ── Main Scan Pass ─────────────────────────────────────────────────────────────

def run_scan(public_url: str = "http://localhost:5002") -> list[dict]:

    # ── Universe selection ─────────────────────────────────────────────────────
    if cfg.USE_DYNAMIC_UNIVERSE:
        universe = build_universe(
            min_price           = cfg.DYNAMIC_MIN_PRICE,
            min_avg_volume      = cfg.DYNAMIC_MIN_AVG_VOLUME,
            require_options     = cfg.DYNAMIC_REQUIRE_OPTIONS,
            require_uptrend     = cfg.DYNAMIC_REQUIRE_UPTREND,
            max_stocks          = cfg.DYNAMIC_MAX_STOCKS,
            threads             = cfg.DYNAMIC_THREADS,
            verbose             = False,
            require_eps_growth  = cfg.DYNAMIC_REQUIRE_EPS_GROWTH,
            min_eps_growth      = cfg.DYNAMIC_MIN_EPS_GROWTH,
            require_52wk_range  = cfg.DYNAMIC_REQUIRE_52WK_RANGE,
            max_52wk_dist       = cfg.DYNAMIC_MAX_52WK_DIST,
        )
    else:
        universe = cfg.UNIVERSE

    print("\n" + "=" * 70)
    print(f"  MOMENTUM INVESTING SCANNER  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    mode = "Dynamic (S&P 500)" if cfg.USE_DYNAMIC_UNIVERSE else "Fixed watchlist"
    print(f"  Universe: {len(universe)} stocks  [{mode}]")
    print("=" * 70)

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 1 — Market Regime
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[1/5] Checking market regime ...")
    regime = fetch_market_regime(sma_period=cfg.REGIME_SMA_PERIOD)
    spy_s   = regime.get("spy_price") or 0
    qqq_s   = regime.get("qqq_price") or 0
    spy_sma = regime.get("spy_sma50") or "n/a"
    qqq_sma = regime.get("qqq_sma50") or "n/a"
    print(f"  SPY: ${spy_s:.2f} {'✓ ABOVE' if regime['spy_ok'] else '✗ BELOW'} SMA50 ({spy_sma})")
    print(f"  QQQ: ${qqq_s:.2f} {'✓ ABOVE' if regime['qqq_ok'] else '✗ BELOW'} SMA50 ({qqq_sma})")

    regime_ok, regime_msg = check_market_regime(regime, cfg.REQUIRE_QQQ_REGIME)
    if not regime_ok:
        print(f"\n  REGIME FAIL: {regime_msg}")
        print("  Sending NO TRADE email ...")
        vix = fetch_vix()
        build_and_send(
            results=[], regime=regime, vix=vix,
            no_trade=True, no_reason=f"Market regime failed: {regime_msg}",
            public_url=public_url, config=cfg,
        )
        return []

    print(f"  {regime_msg}")

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 2 — VIX
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[2/5] Checking VIX ...")
    vix = fetch_vix()
    print(f"  VIX: {vix:.1f}" if vix else "  VIX: unavailable")

    vix_ok, vix_msg, allowed_setups = check_vix(vix, cfg)
    print(f"  {vix_msg}")

    if not vix_ok:
        print("\n  VIX too high — sending NO TRADE email ...")
        build_and_send(
            results=[], regime=regime, vix=vix,
            no_trade=True, no_reason=f"VIX {vix:.1f}: {vix_msg}",
            public_url=public_url, config=cfg,
        )
        return []

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 3 — Parallel Data Fetch
    # ──────────────────────────────────────────────────────────────────────────
    print(f"\n[3/5] Fetching data for {len(universe)} stocks "
          f"({cfg.FETCH_THREADS} threads) ...")

    spy_df   = _fetch_spy_df()
    if spy_df is None:
        print("  WARNING: SPY data unavailable — RS scores will be skipped")

    stock_data = {}   # sym → (price_df, weekly_df, options, earnings)
    rs_raw     = {}   # sym → raw RS score (not yet ranked)

    with ThreadPoolExecutor(max_workers=cfg.FETCH_THREADS) as pool:
        futures = {pool.submit(_fetch_all_stock_data, sym): sym for sym in universe}

        done = 0
        for future in as_completed(futures):
            sym, price, weekly, options, earn = future.result()
            done += 1
            stock_data[sym] = (price, weekly, options, earn)

            if price is not None and spy_df is not None:
                rs_raw[sym] = compute_rs_score(
                    price, spy_df,
                    lookback_3m = cfg.RS_LOOKBACK_3M,
                    lookback_6m = cfg.RS_LOOKBACK_6M,
                    w3m         = cfg.RS_3M_WEIGHT,
                    w6m         = cfg.RS_6M_WEIGHT,
                )
            else:
                rs_raw[sym] = None

            status = (
                "price-ok" if price is not None else "NO-DATA"
            )
            print(f"  [{done:>3}/{len(universe)}] {sym:<7}  {status}")

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 4 — RS Percentile Ranking
    # ──────────────────────────────────────────────────────────────────────────
    print("\n[4/5] Computing RS ranks ...")
    rs_pct = _percentile_rank(rs_raw)

    rs_top = sorted(
        [(s, p) for s, p in rs_pct.items() if p is not None],
        key=lambda x: -x[1],
    )
    print(f"  Top RS stocks: " + "  ".join(f"{s}({p:.0%})" for s, p in rs_top[:8]))

    # ──────────────────────────────────────────────────────────────────────────
    # Pass 5 — Scanner
    # ──────────────────────────────────────────────────────────────────────────
    print(f"\n[5/5] Running scanner ...")
    print(f"  Active setups: {', '.join(sorted(allowed_setups))}")
    print(f"  Threshold: RS >= {cfg.RS_MIN_PERCENTILE:.0%} | R:R >= {cfg.MIN_RISK_REWARD} | "
          f"Earnings blackout: {cfg.EARNINGS_BLACKOUT_DAYS}d")
    print()

    results  = []
    filtered = []

    for sym in universe:
        price, weekly, options, earn = stock_data.get(sym, (None, None, None, None))
        rs_rank = rs_pct.get(sym)

        print(f"  {sym:<7}", end="  ", flush=True)

        if price is None:
            print("SKIP  (no price data)")
            filtered.append((sym, "No price data"))
            continue

        result, reason = scan_symbol(
            symbol         = sym,
            df             = price,
            weekly_df      = weekly,
            options        = options,
            earnings_date  = earn,
            rs_percentile  = rs_rank,
            allowed_setups = allowed_setups,
            config         = cfg,
        )

        if result:
            opt_tag = (
                "opt-OK"  if result["options_ok"] is True else
                "opt-?"   if result["options_ok"] is None else
                "opt-NO"
            )
            print(f"PASS  [{result['setup']:<8}] R:R={result['rr_ratio']:.1f} "
                  f"RS={rs_rank:.0%} {opt_tag}")
            results.append(result)
        else:
            short = (reason or "")[:62]
            print(f"skip  {short}")
            filtered.append((sym, reason))

    # Sort: breakouts first, then by R:R
    results.sort(key=lambda x: (
        x["setup"] != "Breakout",
        -x["rr_ratio"],
    ))

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  SCAN COMPLETE | {len(results)} passed | {len(filtered)} filtered")
    print("=" * 70)

    if results:
        print(f"\n  TOP SETUPS:")
        hdr = f"  {'#':<3} {'Sym':<7} {'Setup':<10} {'Price':>7} {'Stop':>7} {'T1':>7} {'T2':>7} {'R:R':>5} {'Shares':>6} {'Risk$':>7} {'RS':>5}"
        print(hdr)
        print(f"  {'-' * (len(hdr)-2)}")
        for i, r in enumerate(results, 1):
            rs_s = f"{r['rs_rank']:.0%}" if r.get("rs_rank") else "—"
            print(
                f"  {i:<3} {r['symbol']:<7} {r['setup']:<10} "
                f"{r['price']:>7.2f} {r['stop']:>7.2f} "
                f"{r['target_1']:>7.2f} {r['target_2'] or 0:>7.2f} "
                f"{r['rr_ratio']:>5.1f} {r['shares']:>6,} "
                f"${r['risk_dollars']:>6,.0f} {rs_s:>5}"
            )
    else:
        print("""
  No stocks passed all 10 layers.

  Suggestions:
    — Lower RS_MIN_PERCENTILE to 0.50 (accept wider range)
    — Widen PULLBACK_MAX_DIST to 0.08
    — Set PULLBACK_QUIET_VOLUME = False
    — Set REQUIRE_OPTIONS = False (already default)
    — Set WEEKLY_TREND_CHECK = False
    — Add more tickers to UNIVERSE
""")

    _save_results(results)

    # ── Send Email ────────────────────────────────────────────────────────────
    print(f"\n  Sending email to {cfg.GMAIL_RECIPIENT} ...")
    try:
        sent = build_and_send(
            results    = results,
            regime     = regime,
            vix        = vix,
            no_trade   = False,
            no_reason  = "",
            public_url = public_url,
            config     = cfg,
        )
        if sent:
            print(f"  Email sent! Check inbox (and spam folder).")
        else:
            print("  Email failed — check GMAIL_SENDER / GMAIL_APP_PASSWORD in config.py")
    except Exception as e:
        print(f"  Email error: {e}")

    return results


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    """
    Two execution modes:

    GitHub Actions (CI=true or RENDER_URL set):
      — Fetches public_url from RENDER_URL env var
      — Runs scan once, sends email, exits
      — Approval server runs separately on Render

    Local:
      — Starts a local Flask approval server on port 5002
      — Runs scan once, keeps server alive for approval clicks
      — Ctrl+C to stop
    """
    in_ci      = os.environ.get("CI", "false").lower() == "true"
    render_url = os.environ.get("RENDER_URL", "")

    if in_ci or render_url:
        # ── GitHub Actions mode ────────────────────────────────────────────────
        public_url = render_url or "http://localhost:5002"
        print(f"\n  Mode: GitHub Actions  |  Approval URL: {public_url}")
        run_scan(public_url=public_url)
        print("\n  Scan complete. Email sent. Exiting.")
    else:
        # ── Local mode ────────────────────────────────────────────────────────
        print("\n  Mode: Local  |  Starting approval server on port 5002 ...")
        public_url = start_server()

        run_scan(public_url=public_url)

        print(f"\n  Approval dashboard: {public_url}")
        print(f"  Server staying alive for approval clicks. Ctrl+C to stop.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n  Stopped.")


if __name__ == "__main__":
    main()
