"""
ibkr_client.py — IBKR Paper Trading Client for Momentum Investing
==================================================================
Wraps the IBKRTrader from the parent folder.
Adds:
  - ATR-based position sizing
  - Bracket orders (entry limit + stop loss together)
  - Trade log specific to this strategy
  - Daily loss limit enforcement
"""

import sys
import json
import math
from pathlib import Path
from datetime import datetime

# ── Import IBKRTrader from parent folder ──────────────────────────────────────
_PARENT = Path(__file__).parent.parent
if str(_PARENT) not in sys.path:
    sys.path.insert(0, str(_PARENT))

try:
    from ibkr_trader import IBKRTrader
    _IBKR_AVAILABLE = True
except ImportError:
    _IBKR_AVAILABLE = False
    print("  [IBKR] ib_insync not installed — orders will be simulated (paper log only)")


TRADE_LOG_PATH = Path(__file__).parent / "momentum_trade_log.json"


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
    entry: float,
    stop: float,
    account_size: float,
    risk_pct: float,
    max_position_pct: float,
) -> int:
    """
    ATR / stop-distance based position sizing.

    shares = (account × risk_pct) / (entry - stop)

    Capped at:
      account × max_position_pct / entry
    to prevent oversized positions when stops are very tight.
    """
    if entry <= 0 or stop <= 0 or entry <= stop:
        return 0

    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0

    risk_dollars    = account_size * risk_pct
    shares_by_risk  = risk_dollars / risk_per_share
    max_shares      = (account_size * max_position_pct) / entry

    shares = min(shares_by_risk, max_shares)
    return max(1, math.floor(shares))


# ── MomentumIBKRClient ────────────────────────────────────────────────────────

class MomentumIBKRClient:
    """
    Thin wrapper around IBKRTrader with momentum-strategy-specific logic.
    Handles position sizing, bracket placement, and trade logging.
    """

    def __init__(self, config):
        self.cfg    = config
        self._trades = _load_log()
        self._trader = None

    def connect(self) -> bool:
        if not _IBKR_AVAILABLE:
            print("  [IBKR] ib_insync not available — running in simulation mode")
            return False
        try:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    raise RuntimeError
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            self._trader = IBKRTrader(
                host      = self.cfg.IBKR_HOST,
                port      = self.cfg.IBKR_PORT,
                client_id = self.cfg.IBKR_CLIENT_ID,
            )
            ok = self._trader.connect()
            if ok:
                val  = self._trader.get_account_value()
                cash = self._trader.get_cash()
                print(f"  [IBKR] Connected | Portfolio: ${val:,.0f} | Cash: ${cash:,.0f}")
            return ok
        except Exception as e:
            print(f"  [IBKR] Connection failed: {e}")
            return False

    def disconnect(self):
        if self._trader:
            try:
                self._trader.disconnect()
            except Exception:
                pass
            self._trader = None

    def get_account_value(self) -> float:
        if self._trader:
            try:
                return self._trader.get_account_value() or self.cfg.ACCOUNT_SIZE
            except Exception:
                pass
        return self.cfg.ACCOUNT_SIZE

    def get_positions(self) -> list:
        if self._trader:
            try:
                return self._trader.get_positions()
            except Exception:
                pass
        return []

    def check_daily_loss(self) -> dict:
        today     = datetime.now().strftime("%Y-%m-%d")
        today_pnl = sum(
            t.get("pnl", 0)
            for t in self._trades
            if t.get("date", "").startswith(today) and t.get("status") == "CLOSED"
        )
        max_loss   = self.cfg.ACCOUNT_SIZE * self.cfg.DAILY_MAX_LOSS_PCT
        hit_limit  = today_pnl <= -max_loss

        return {
            "daily_pnl":  round(today_pnl, 2),
            "max_loss":   round(-max_loss, 2),
            "hit_limit":  hit_limit,
            "pct_used":   round(abs(today_pnl) / max_loss * 100, 1) if max_loss > 0 else 0,
        }

    def count_open_trades(self) -> int:
        return sum(1 for t in self._trades if t.get("status") in ["PENDING", "FILLED"])

    def place_order(self, setup: dict) -> dict:
        """
        Place a limit buy order + stop loss order for a passing setup.

        setup dict must contain:
          symbol, entry_price, stop_price, target_1, target_2,
          shares, setup_type, rr_ratio, ev_dollars, rs_rank
        """
        symbol      = setup["symbol"]
        entry       = setup["entry_price"]
        stop        = setup["stop_price"]
        target_1    = setup["target_1"]
        shares      = setup["shares"]

        if shares <= 0:
            return {"error": "Invalid share count (0)"}

        # Daily loss check
        loss_check = self.check_daily_loss()
        if loss_check["hit_limit"]:
            return {"error": f"Daily loss limit hit (${loss_check['daily_pnl']:,.0f})"}

        # Concurrent trades check
        if self.count_open_trades() >= self.cfg.MAX_CONCURRENT_TRADES:
            return {"error": f"Max concurrent trades ({self.cfg.MAX_CONCURRENT_TRADES}) reached"}

        order_id = None

        if _IBKR_AVAILABLE:
            # ── Run IBKR order in its own thread with its own event loop ─────
            # Flask uses a thread pool (process_request_thread) that has no
            # asyncio event loop. The only safe fix is to spin up a brand-new
            # thread that owns its event loop from creation to teardown.
            result_box = {}

            def _place_in_thread():
                import asyncio
                from ib_insync import IB, Stock, LimitOrder, StopOrder

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    ib = IB()
                    ib.connect(
                        self.cfg.IBKR_HOST,
                        self.cfg.IBKR_PORT,
                        clientId=self.cfg.IBKR_ORDER_CLIENT_ID,
                    )
                    contract = Stock(symbol, "SMART", "USD")
                    ib.qualifyContracts(contract)

                    lmt = ib.placeOrder(contract, LimitOrder("BUY", shares, round(entry, 2)))
                    ib.sleep(1)
                    result_box["order_id"] = lmt.order.orderId
                    print(f"  [IBKR] BUY LIMIT {shares}x {symbol} @ ${entry:.2f}  (id {lmt.order.orderId})")

                    stp = ib.placeOrder(contract, StopOrder("SELL", shares, round(stop, 2)))
                    ib.sleep(1)
                    print(f"  [IBKR] STOP LOSS {shares}x {symbol} @ ${stop:.2f}  (id {stp.order.orderId})")

                    ib.disconnect()
                except Exception as e:
                    result_box["error"] = str(e)
                finally:
                    loop.close()

            import threading
            t = threading.Thread(target=_place_in_thread, daemon=True)
            t.start()
            t.join(timeout=20)

            if "error" in result_box:
                return {"error": f"IBKR order failed: {result_box['error']}"}
            if "order_id" not in result_box:
                return {"error": "IBKR timed out — is TWS open on port 7497?"}

            order_id = result_box["order_id"]
        else:
            # Simulation mode — log only
            order_id = f"SIM-{datetime.now().strftime('%H%M%S')}"
            print(f"  [SIM] BUY LIMIT {shares}x {symbol} @ ${entry:.2f} (simulated)")
            print(f"  [SIM] STOP LOSS @ ${stop:.2f} (simulated)")

        # Build trade record
        record = {
            "id":          order_id,
            "symbol":      symbol,
            "date":        datetime.now().strftime("%Y-%m-%d"),
            "time":        datetime.now().strftime("%H:%M:%S"),
            "shares":      shares,
            "entry_price": entry,
            "stop_price":  stop,
            "target_1":    target_1,
            "target_2":    setup.get("target_2"),
            "setup_type":  setup.get("setup_type"),
            "rr_ratio":    setup.get("rr_ratio"),
            "ev_dollars":  setup.get("ev_dollars"),
            "rs_rank":     setup.get("rs_rank"),
            "risk_dollars": shares * (entry - stop),
            "status":      "PENDING",
            "exit_price":  None,
            "pnl":         0,
            "simulated":   not bool(order_id and not str(order_id).startswith("SIM")),
        }

        self._trades.append(record)
        _save_log(self._trades)

        return record
