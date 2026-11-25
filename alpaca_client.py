"""
alpaca_client.py — Alpaca Paper Trading Client for Momentum Investing
=====================================================================
Replaces ibkr_client.py. No desktop app required — pure REST API.

Order flow per approved setup:
  1. DAY limit buy  @ prior close price  (entry)
  2. GTC stop sell  @ stop price         (loss protection)

T1 / T2 exits are shown in the email. Manage them on Alpaca's web UI
or app after the entry fills.

Requires env vars (set in GitHub Secrets + Render dashboard):
  ALPACA_API_KEY
  ALPACA_API_SECRET
  ALPACA_BASE_URL   (default: https://paper-api.alpaca.markets)
"""

import os
import math
import json
from pathlib import Path
from datetime import datetime

# ── SDK ───────────────────────────────────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import LimitOrderRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False
    print("  [Alpaca] alpaca-py not installed — pip install alpaca-py")

TRADE_LOG_PATH = Path(__file__).parent / "momentum_trade_log.json"


# ── Credentials (from environment) ────────────────────────────────────────────

def _get_client() -> "TradingClient":
    key    = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    paper  = "paper" in os.environ.get("ALPACA_BASE_URL",
                                        "https://paper-api.alpaca.markets")
    if not key or not secret:
        raise ValueError("ALPACA_API_KEY / ALPACA_API_SECRET env vars not set")
    return TradingClient(key, secret, paper=paper)


# ── Trade Log ─────────────────────────────────────────────────────────────────

def _load_log() -> list:
    if TRADE_LOG_PATH.exists():
        try:
            return json.loads(TRADE_LOG_PATH.read_text())
        except Exception:
            pass
    return []


def _save_log(trades: list):
    try:
        TRADE_LOG_PATH.write_text(json.dumps(trades, indent=2, default=str))
    except Exception as e:
        print(f"  [TradeLog] Save error: {e}")


# ── Position Sizing ────────────────────────────────────────────────────────────

def compute_position_size(
    entry:            float,
    stop:             float,
    account_size:     float,
    risk_pct:         float,
    max_position_pct: float,
) -> int:
    """
    shares = (account × risk_pct) / (entry − stop)
    Capped at account × max_position_pct / entry.
    """
    if entry <= 0 or stop <= 0 or entry <= stop:
        return 0
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0
    shares = min(
        (account_size * risk_pct) / risk_per_share,
        (account_size * max_position_pct) / entry,
    )
    return max(1, math.floor(shares))


# ── Account Info ──────────────────────────────────────────────────────────────

def get_account_value() -> float:
    """Return portfolio value from Alpaca. Falls back to 0 on error."""
    if not _ALPACA_AVAILABLE:
        return 0.0
    try:
        acct = _get_client().get_account()
        return float(acct.portfolio_value)
    except Exception as e:
        print(f"  [Alpaca] Account fetch error: {e}")
        return 0.0


# ── Daily Loss Check ──────────────────────────────────────────────────────────

def check_daily_loss(account_size: float, daily_max_loss_pct: float) -> dict:
    """Check today's realised P&L from trade log against the daily loss limit."""
    trades   = _load_log()
    today    = datetime.now().strftime("%Y-%m-%d")
    today_pnl = sum(
        t.get("pnl", 0)
        for t in trades
        if t.get("date", "").startswith(today) and t.get("status") == "CLOSED"
    )
    max_loss  = account_size * daily_max_loss_pct
    hit_limit = today_pnl <= -max_loss
    return {
        "daily_pnl": round(today_pnl, 2),
        "max_loss":  round(-max_loss, 2),
        "hit_limit": hit_limit,
        "pct_used":  round(abs(today_pnl) / max_loss * 100, 1) if max_loss > 0 else 0,
    }


def count_open_trades() -> int:
    return sum(1 for t in _load_log() if t.get("status") in ["PENDING", "FILLED"])


# ── Place Order ───────────────────────────────────────────────────────────────

def place_order(setup: dict, config) -> dict:
    """
    Place a DAY limit buy + GTC stop loss sell on Alpaca.

    setup dict keys:
      symbol, entry_price, stop_price, target_1, target_2,
      shares, setup_type, rr_ratio, ev_dollars, rs_rank

    Returns the trade record dict, or a dict with an "error" key on failure.
    """
    symbol   = setup["symbol"]
    entry    = round(float(setup["entry_price"]), 2)
    stop     = round(float(setup["stop_price"]),  2)
    target_1 = setup.get("target_1")
    target_2 = setup.get("target_2")
    shares   = int(setup["shares"])

    if shares <= 0:
        return {"error": "Invalid share count (0)"}

    # Guards
    loss_check = check_daily_loss(config.ACCOUNT_SIZE, config.DAILY_MAX_LOSS_PCT)
    if loss_check["hit_limit"]:
        return {"error": f"Daily loss limit hit (${loss_check['daily_pnl']:,.0f})"}

    if count_open_trades() >= config.MAX_CONCURRENT_TRADES:
        return {"error": f"Max concurrent trades ({config.MAX_CONCURRENT_TRADES}) reached"}

    order_id  = None
    simulated = False

    if _ALPACA_AVAILABLE:
        try:
            client = _get_client()

            # ── OTO: limit buy + stop loss linked together ────────────────────
            # Stop only activates after entry fills — no "wash trade" error
            buy_req = LimitOrderRequest(
                symbol        = symbol,
                qty           = shares,
                side          = OrderSide.BUY,
                time_in_force = TimeInForce.DAY,
                limit_price   = entry,
                order_class   = OrderClass.OTO,
                stop_loss     = StopLossRequest(stop_price=stop),
            )
            buy_order = client.submit_order(buy_req)
        except Exception as e:
            return {"error": f"Alpaca API error: {e}"}

        # Order submitted — extract ID separately so a parsing quirk can't
        # mask a successful placement and trigger the "Order Failed" page.
        try:
            order_id = str(buy_order.id)
        except Exception:
            order_id = f"OTO-{datetime.now().strftime('%H%M%S')}"
        print(f"  [Alpaca] OTO BUY+STOP  {shares}x {symbol} "
              f"@ ${entry:.2f} entry / ${stop:.2f} stop  id={order_id[:8]}")
    else:
        # Simulation mode (alpaca-py not installed)
        order_id  = f"SIM-{datetime.now().strftime('%H%M%S')}"
        simulated = True
        print(f"  [SIM] BUY LIMIT  {shares}x {symbol} @ ${entry:.2f}")
        print(f"  [SIM] STOP LOSS  {shares}x {symbol} @ ${stop:.2f}")

    record = {
        "id":           order_id,
        "symbol":       symbol,
        "date":         datetime.now().strftime("%Y-%m-%d"),
        "time":         datetime.now().strftime("%H:%M:%S"),
        "shares":       shares,
        "entry_price":  entry,
        "stop_price":   stop,
        "target_1":     target_1,
        "target_2":     target_2,
        "setup_type":   setup.get("setup_type"),
        "rr_ratio":     setup.get("rr_ratio"),
        "ev_dollars":   setup.get("ev_dollars"),
        "rs_rank":      setup.get("rs_rank"),
        "risk_dollars": shares * (entry - stop),
        "status":       "PENDING",
        "exit_price":   None,
        "pnl":          0,
        "simulated":    simulated,
    }

    trades = _load_log()
    trades.append(record)
    _save_log(trades)

    return record
