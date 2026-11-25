"""
approval_server.py — Momentum Investing Approval Server (Render / Alpaca)
=========================================================================
Runs permanently on Render (free tier).
Email approve/reject links point to this server's public URL.

/approve  — places DAY limit buy + GTC stop via Alpaca
/reject   — logs the skip, no order placed
/status   — JSON summary of today's activity
/          — approval dashboard (last 30 actions)

No IBKR. No ngrok. No desktop app required.
"""

import os
import json
import threading
from pathlib import Path
from datetime import datetime
from flask import Flask, request, jsonify

import alpaca_client as ac

app      = Flask(__name__)
LOG_FILE = Path(__file__).parent / "approvals.json"

# Config is imported so daily-loss / max-trades limits are available
import config as cfg


# ── Log helpers ───────────────────────────────────────────────────────────────

def _load_log() -> list:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            pass
    return []


def _save_log(data: list):
    try:
        LOG_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ── Theme ─────────────────────────────────────────────────────────────────────
_BG    = "#0d1117"
_CARD  = "#161b22"
_BORD  = "#30363d"
_TEXT  = "#e6edf3"
_MUTED = "#8b949e"
_GREEN = "#3fb950"
_RED   = "#f85149"
_YELLOW = "#d29922"
_BLUE  = "#58a6ff"


def _page(title: str, message: str, color: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{background:{_BG};color:{_TEXT};font-family:'Segoe UI',Arial,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.box{{background:{_CARD};border:2px solid {color};border-radius:12px;
      padding:40px 48px;text-align:center;max-width:540px;line-height:1.7}}
h2{{color:{color};margin-top:0;font-size:22px}}
p{{color:{_MUTED};font-size:14px}}
a{{color:{_BLUE};text-decoration:none}}
</style>
</head>
<body>
<div class="box">
  <h2>{title}</h2>
  <p>{message}</p>
  <p style="margin-top:24px"><a href="/">← Dashboard</a></p>
</div>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/approve")
def approve():
    symbol   = request.args.get("symbol", "")
    shares   = int(request.args.get("shares",  0))
    entry    = float(request.args.get("entry",  0))
    stop     = float(request.args.get("stop",   0))
    target_1 = float(request.args.get("t1",     0))
    target_2 = float(request.args.get("t2",     0))
    rr       = float(request.args.get("rr",     0))
    ev       = float(request.args.get("ev",     0))
    setup    = request.args.get("setup",  "")
    rs_rank  = float(request.args.get("rs",     0))

    if not symbol or shares <= 0 or entry <= 0:
        return _page("Invalid Request",
                     "Missing or invalid order parameters. Check the email link.",
                     _RED)

    setup_dict = {
        "symbol":      symbol,
        "entry_price": entry,
        "stop_price":  stop,
        "target_1":    target_1,
        "target_2":    target_2,
        "shares":      shares,
        "setup_type":  setup,
        "rr_ratio":    rr,
        "ev_dollars":  ev,
        "rs_rank":     rs_rank,
    }

    result = ac.place_order(setup_dict, cfg)

    log = _load_log()
    log.append({"action": "APPROVED", "symbol": symbol, "shares": shares,
                 "entry": entry, "stop": stop, "time": datetime.now().isoformat()})
    _save_log(log)

    if "error" in result:
        return _page(
            f"Order Failed — {symbol}",
            f"<b>{result['error']}</b><br><br>"
            f"Check that ALPACA_API_KEY and ALPACA_API_SECRET are set correctly "
            f"in the Render environment variables.",
            _RED,
        )

    risk_usd = shares * (entry - stop)
    stop_pct  = (entry - stop) / entry
    t1_pct    = (target_1 / entry - 1) if entry else 0
    t2_pct    = (target_2 / entry - 1) if entry and target_2 else 0

    return _page(
        f"Order Placed — {symbol}",
        f"<b>BUY {shares:,} × {symbol}</b> @ ${entry:.2f} limit (DAY order)<br><br>"
        f"Stop loss: ${stop:.2f} &nbsp;({stop_pct:.1%} risk)<br>"
        f"Target 1:  ${target_1:.2f} &nbsp;(+{t1_pct:.0%} — sell 1/3)<br>"
        f"Target 2:  ${target_2:.2f} &nbsp;(+{t2_pct:.0%} — sell 1/3, trail 1/3)<br><br>"
        f"Risk: ${risk_usd:,.0f} &nbsp;|&nbsp; R:R = {rr:.1f}:1 &nbsp;|&nbsp; "
        f"EV = ${ev:+,.0f}<br><br>"
        f"<small style='color:{_MUTED}'>Stop loss order placed on Alpaca (GTC).<br>"
        f"Manage T1/T2 exits on alpaca.markets after entry fills.</small>",
        _GREEN,
    )


@app.route("/reject")
def reject():
    symbol = request.args.get("symbol", "?")
    log    = _load_log()
    log.append({"action": "REJECTED", "symbol": symbol,
                 "time": datetime.now().isoformat()})
    _save_log(log)
    return _page(
        f"Skipped — {symbol}",
        f"Setup for <b>{symbol}</b> was skipped. No order placed.",
        _YELLOW,
    )


@app.route("/status")
def status():
    loss  = ac.check_daily_loss(cfg.ACCOUNT_SIZE, cfg.DAILY_MAX_LOSS_PCT)
    open_t = ac.count_open_trades()
    return jsonify({
        "open_trades":  open_t,
        "daily_loss":   loss,
        "alpaca_value": ac.get_account_value(),
    })


@app.route("/test_order")
def test_order():
    """Place a $1 limit buy for 1 share of AAPL — will never fill. Confirms API works."""
    result = ac.place_order({
        "symbol":      "AAPL",
        "entry_price": 1.00,
        "stop_price":  0.50,
        "target_1":    1.20,
        "target_2":    1.40,
        "shares":      1,
        "setup_type":  "Test",
        "rr_ratio":    2.0,
        "ev_dollars":  0,
        "rs_rank":     0,
    }, cfg)

    if "error" in result:
        return _page(
            "Test Order Failed",
            f"<b>{result['error']}</b><br><br>"
            "Check ALPACA_API_KEY and ALPACA_API_SECRET in Render environment variables.",
            _RED,
        )

    return _page(
        "Test Order Placed",
        "1 share of AAPL @ $1.00 limit (paper) — will never fill.<br><br>"
        "Alpaca API is working correctly.<br>"
        "Cancel it on <a href='https://app.alpaca.markets' target='_blank'>alpaca.markets</a> "
        "under Open Orders.",
        _GREEN,
    )


@app.route("/")
def dashboard():
    log  = _load_log()
    rows = ""
    for item in reversed(log[-30:]):
        color = _GREEN if item["action"] == "APPROVED" else _YELLOW
        sym   = item.get("symbol", "")
        t     = item.get("time", "")[:19]
        ep    = f"${item.get('entry', 0):.2f}" if item.get("entry") else "—"
        sh    = item.get("shares", "—")
        rows += (f"<tr><td>{t}</td>"
                 f"<td style='color:{color};font-weight:700'>{item['action']}</td>"
                 f"<td>{sym}</td><td>{sh}</td><td>{ep}</td></tr>")

    acct_val = ac.get_account_value()
    loss     = ac.check_daily_loss(cfg.ACCOUNT_SIZE, cfg.DAILY_MAX_LOSS_PCT)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{background:{_BG};color:{_TEXT};font-family:'Segoe UI',Arial,sans-serif;padding:24px;max-width:900px;margin:0 auto}}
h2{{color:{_BLUE};border-bottom:2px solid {_BORD};padding-bottom:10px}}
h3{{color:{_TEXT};margin-top:24px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th{{background:{_BORD};color:{_MUTED};padding:8px 12px;text-align:left}}
td{{padding:8px 12px;border-bottom:1px solid {_BORD}}}
.stat{{display:inline-block;background:{_CARD};border:1px solid {_BORD};
       border-radius:8px;padding:12px 20px;margin:6px;min-width:140px;text-align:center}}
.stat-val{{font-size:20px;font-weight:700;color:{_GREEN}}}
.stat-lbl{{font-size:12px;color:{_MUTED};margin-top:4px}}
.btn{{display:inline-block;padding:8px 20px;border-radius:6px;font-size:13px;
      font-weight:700;text-decoration:none;background:{_BLUE};color:#000}}
</style>
</head>
<body>
<h2>Momentum Scanner — Approval Dashboard</h2>

<div>
  <div class="stat">
    <div class="stat-val">${acct_val:,.0f}</div>
    <div class="stat-lbl">Alpaca Portfolio</div>
  </div>
  <div class="stat">
    <div class="stat-val" style="color:{'#f85149' if loss['hit_limit'] else '#3fb950'}">{loss['pct_used']:.0f}%</div>
    <div class="stat-lbl">Daily Loss Used</div>
  </div>
  <div class="stat">
    <div class="stat-val">{ac.count_open_trades()}</div>
    <div class="stat-lbl">Open Trades (log)</div>
  </div>
</div>

<h3>Connectivity Test</h3>
<p style="color:{_MUTED};font-size:13px">
  Places 1 share of AAPL @ $1.00 limit (paper — will never fill). Confirms Alpaca API is wired up.
</p>
<a href="/test_order" class="btn">Run Test Order</a>

<h3 style="margin-top:28px">Recent Activity (last 30)</h3>
<table>
  <tr><th>Time</th><th>Action</th><th>Symbol</th><th>Shares</th><th>Entry</th></tr>
  {rows or f"<tr><td colspan='5' style='color:{_MUTED}'>No activity yet</td></tr>"}
</table>
</body>
</html>"""


# ── Entry point (Render starts this directly) ─────────────────────────────────

def start_server() -> str:
    """
    Start Flask in a background thread (used when running locally).
    On Render, gunicorn calls the `app` object directly — this function
    is only used for local testing.
    Returns the public URL (from env var or localhost fallback).
    """
    public_url = os.environ.get("RENDER_URL", "http://localhost:5002")

    thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5002,
                               debug=False, use_reloader=False),
        daemon=True,
    )
    thread.start()
    print(f"  [Server] Approval server running → {public_url}")
    return public_url


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5002))
    app.run(host="0.0.0.0", port=port, debug=False)
