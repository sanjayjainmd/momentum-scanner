"""
email_reporter.py — Momentum Investing HTML Email Report
=========================================================
Sends a rich dark-theme HTML email with:
  - Market regime summary (SPY, QQQ, VIX)
  - Per-stock card:
      Setup type + rationale bullets
      Trade plan (entry, stop, T1, T2, R:R)
      Position sizing (shares, $ risk, $ exposure)
      Expected Value with win-rate assumption
      Options flow summary
      Approve / Reject buttons
  - No trade message if regime fails or no setups found
"""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime import datetime

# ── Theme ─────────────────────────────────────────────────────────────────────
BG     = "#0d1117"
CARD   = "#161b22"
CARD2  = "#1c2128"
BORD   = "#30363d"
TEXT   = "#e6edf3"
MUTED  = "#8b949e"
GREEN  = "#3fb950"
RED    = "#f85149"
YELLOW = "#d29922"
BLUE   = "#58a6ff"
ORANGE = "#ffa657"
PURPLE = "#bc8cff"

CSS = f"""
body{{background:{BG};color:{TEXT};font-family:'Segoe UI',Arial,sans-serif;margin:0;padding:0}}
.wrap{{max-width:820px;margin:0 auto;padding:20px 16px}}
h1{{color:{BLUE};border-bottom:2px solid {BORD};padding-bottom:10px;font-size:22px;margin-top:0}}
h2{{color:{TEXT};font-size:15px;margin:22px 0 8px 0;border-left:4px solid {BLUE};padding-left:10px}}
h3{{color:{MUTED};font-size:12px;font-weight:700;text-transform:uppercase;
    letter-spacing:.06em;margin:14px 0 6px 0}}
.card{{background:{CARD};border:1px solid {BORD};border-radius:10px;padding:16px 20px;margin-bottom:16px}}
.regime-row{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:4px}}
.metric{{background:{CARD2};border:1px solid {BORD};border-radius:6px;padding:10px 12px;text-align:center}}
.metric .label{{color:{MUTED};font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
.metric .val{{font-size:18px;font-weight:700;margin-top:4px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:4px;font-size:12px;font-weight:700}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:{BORD};color:{MUTED};padding:7px 10px;text-align:left;font-size:11px;
    text-transform:uppercase;letter-spacing:.04em}}
td{{padding:8px 10px;border-bottom:1px solid {BORD};vertical-align:top}}
tr:last-child td{{border-bottom:none}}
.approve-btn{{display:inline-block;background:{GREEN};color:#000;padding:13px 36px;
  border-radius:6px;font-size:15px;font-weight:700;text-decoration:none;margin:8px 6px 8px 0}}
.reject-btn{{display:inline-block;background:{BORD};color:{TEXT};padding:13px 28px;
  border-radius:6px;font-size:15px;font-weight:700;text-decoration:none;margin:8px 0}}
.ev-box{{background:{CARD2};border:1px solid {BORD};border-radius:6px;padding:12px 16px;margin:12px 0}}
.no-trade{{background:#1c1610;border-left:4px solid {ORANGE};border-radius:6px;
  padding:16px 20px;margin:12px 0;line-height:1.7}}
li{{margin:4px 0;line-height:1.5}}
hr{{border:none;border-top:1px solid {BORD};margin:14px 0}}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _p(val, fmt="$.2f"):
    if val is None: return "—"
    return f"${val:,.2f}"

def _pct(val):
    if val is None: return "—"
    return f"{val:+.1%}"

def _clr(val, good=">0"):
    if val is None: return MUTED
    if good == ">0": return GREEN if val > 0 else RED
    return GREEN if val >= 0 else RED

def _rr_clr(rr):
    if rr is None: return MUTED
    if rr >= 3:   return GREEN
    if rr >= 2:   return YELLOW
    return RED


# ── Regime Header ─────────────────────────────────────────────────────────────

def _regime_section(regime: dict, vix: float | None) -> str:
    spy_ok  = regime.get("spy_ok", False)
    qqq_ok  = regime.get("qqq_ok", False)
    spy_p   = regime.get("spy_price")
    spy_s   = regime.get("spy_sma50")
    qqq_p   = regime.get("qqq_price")
    qqq_s   = regime.get("qqq_sma50")

    spy_clr = GREEN if spy_ok else RED
    qqq_clr = GREEN if qqq_ok else RED

    vix_str = f"{vix:.1f}" if vix else "—"
    vix_clr = GREEN if vix and vix < 20 else YELLOW if vix and vix < 30 else RED

    spy_dist = ((spy_p - spy_s) / spy_s * 100) if spy_p and spy_s else 0
    qqq_dist = ((qqq_p - qqq_s) / qqq_s * 100) if qqq_p and qqq_s else 0

    return f"""
<div class="card">
  <h3>Market Regime</h3>
  <div class="regime-row">
    <div class="metric">
      <div class="label">SPY vs SMA50</div>
      <div class="val" style="color:{spy_clr}">{"ABOVE" if spy_ok else "BELOW"}</div>
      <div style="color:{MUTED};font-size:11px;margin-top:4px">{spy_dist:+.1f}%</div>
    </div>
    <div class="metric">
      <div class="label">QQQ vs SMA50</div>
      <div class="val" style="color:{qqq_clr}">{"ABOVE" if qqq_ok else "BELOW"}</div>
      <div style="color:{MUTED};font-size:11px;margin-top:4px">{qqq_dist:+.1f}%</div>
    </div>
    <div class="metric">
      <div class="label">VIX</div>
      <div class="val" style="color:{vix_clr}">{vix_str}</div>
      <div style="color:{MUTED};font-size:11px;margin-top:4px">
        {"Low Fear" if vix and vix < 20 else "Moderate" if vix and vix < 30 else "High Fear"}
      </div>
    </div>
    <div class="metric">
      <div class="label">Regime</div>
      <div class="val" style="color:{GREEN if spy_ok and qqq_ok else RED}">
        {"RISK ON" if spy_ok and qqq_ok else "CAUTION"}
      </div>
      <div style="color:{MUTED};font-size:11px;margin-top:4px">
        {"Trade both setups" if spy_ok and qqq_ok else "Reduce size / pullbacks only"}
      </div>
    </div>
  </div>
</div>"""


# ── Expected Value Box ─────────────────────────────────────────────────────────

def _ev_section(result: dict, config) -> str:
    setup_type = result.get("setup", "")
    rr         = result.get("rr_ratio", 0) or 0
    shares     = result.get("shares", 0) or 0
    risk_sh    = result.get("risk_per_share", 0) or 0
    risk_usd   = shares * risk_sh

    # Win rate by setup type
    if "Breakout" in setup_type:
        win_rate  = config.EV_WIN_RATE_BREAKOUT
        wr_label  = f"{win_rate:.0%} — 20-day breakout, within 25% of 52wk high (IBD research)"
    else:
        win_rate  = config.EV_WIN_RATE_PULLBACK
        wr_label  = f"{win_rate:.0%} — Pullback in strong trend (Minervini research)"

    loss_rate  = 1 - win_rate

    # Blended reward: 1/3 at T1, 1/3 at T2, 1/3 trail (approximated as T2)
    entry  = result.get("price", 0)
    t1     = result.get("target_1", 0) or 0
    t2     = result.get("target_2", 0) or 0
    if entry and t1:
        t2_val = (t2 - entry) if t2 else (t1 - entry)
        reward_per_share = (1/3) * (t1 - entry) + (1/3) * t2_val + (1/3) * t2_val
        reward_usd       = shares * reward_per_share
    else:
        reward_usd       = risk_usd * rr

    ev_usd   = win_rate * reward_usd - loss_rate * risk_usd
    ev_per_r = win_rate * rr - loss_rate   # EV in R-units (> 0 = positive expectancy)

    ev_clr   = GREEN if ev_usd > 0 else RED
    evr_clr  = GREEN if ev_per_r > 0 else RED

    return f"""
<div class="ev-box">
  <h3>Expected Value Analysis</h3>
  <table>
  <tr>
    <th>Metric</th><th>Value</th><th>Assumption</th>
  </tr>
  <tr>
    <td style="color:{MUTED}">Win Rate</td>
    <td style="color:{TEXT};font-weight:700">{win_rate:.0%}</td>
    <td style="color:{MUTED};font-size:12px">{wr_label}</td>
  </tr>
  <tr>
    <td style="color:{MUTED}">Risk (if stopped out)</td>
    <td style="color:{RED};font-weight:700">${risk_usd:,.0f}</td>
    <td style="color:{MUTED};font-size:12px">{shares:,} shares × ${risk_sh:.2f}/share stop distance</td>
  </tr>
  <tr>
    <td style="color:{MUTED}">Reward (blended T1+T2+trail)</td>
    <td style="color:{GREEN};font-weight:700">${reward_usd:,.0f}</td>
    <td style="color:{MUTED};font-size:12px">1/3 at T1 ({_pct((t1/entry)-1) if entry else "?"}), 1/3 at T2 ({_pct((t2/entry)-1) if entry and t2 else "?"}), 1/3 trail EMA20</td>
  </tr>
  <tr>
    <td style="color:{MUTED}">EV per trade</td>
    <td style="color:{ev_clr};font-weight:700;font-size:16px">${ev_usd:+,.0f}</td>
    <td style="color:{MUTED};font-size:12px">EV in R-units: {ev_per_r:+.2f}R  (positive = edge exists)</td>
  </tr>
  </table>
</div>"""


# ── Per-Stock Card ─────────────────────────────────────────────────────────────

def _stock_card(result: dict, idx: int, public_url: str, config) -> str:
    sym      = result["symbol"]
    setup    = result.get("setup", "")
    rr       = result.get("rr_ratio")
    rs_rank  = result.get("rs_rank")
    price    = result.get("price", 0)
    sma50    = result.get("sma_50", 0)
    sma200   = result.get("sma_200", 0)
    rsi      = result.get("rsi")
    vol_r    = result.get("vol_ratio")
    stop     = result.get("stop", 0)
    t1       = result.get("target_1", 0)
    t2       = result.get("target_2", 0)
    shares   = result.get("shares", 0)
    risk_sh  = result.get("risk_per_share", 0)
    risk_usd = shares * risk_sh if shares and risk_sh else 0
    opt_msg  = result.get("options_msg", "No data")
    cpr      = result.get("call_put")
    otm_pct  = result.get("otm_pct")
    rationale = result.get("rationale", [])
    warnings  = result.get("warnings", [])
    near_52wk = result.get("near_52wk_high", False)

    # Badge colors
    setup_clr = BLUE if "Breakout" in setup else PURPLE
    rr_clr    = _rr_clr(rr)
    rs_str    = f"{rs_rank:.0%}" if rs_rank is not None else "—"
    rs_clr    = GREEN if rs_rank and rs_rank >= 0.7 else YELLOW if rs_rank and rs_rank >= 0.5 else RED

    # Approve URL
    approve_url = (
        f"{public_url}/approve?"
        f"symbol={sym}&shares={shares}&entry={price:.2f}&stop={stop:.2f}"
        f"&t1={t1:.2f}&t2={t2 or 0:.2f}&rr={rr or 0:.1f}"
        f"&ev={shares * ((0.58 if 'Pullback' in setup else 0.47) * rr * risk_sh - (1-(0.58 if 'Pullback' in setup else 0.47)) * risk_sh):.0f}"
        f"&setup={setup}&rs={rs_rank or 0:.2f}"
    )
    reject_url = f"{public_url}/reject?symbol={sym}"

    rationale_html = "".join(f'<li style="color:{GREEN}">✅ {r}</li>' for r in rationale)
    warnings_html  = "".join(f'<li style="color:{YELLOW}">⚠️ {w}</li>'  for w in warnings)

    # 52-week badge
    wk52_badge = (f'&nbsp;<span class="badge" style="background:{GREEN}22;color:{GREEN}">Near 52wk High</span>'
                  if near_52wk else "")

    opt_clr   = GREEN if result.get("options_ok") is True else YELLOW if result.get("options_ok") is None else RED
    cpr_str   = f"{cpr:.1f}x" if cpr is not None else "—"
    otm_str   = f"{otm_pct:.0%}" if otm_pct is not None else "—"

    ev_html = _ev_section(result, config)

    return f"""
<div class="card">

  <!-- Header row -->
  <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:14px">
    <div>
      <span style="font-size:24px;font-weight:700;color:{TEXT}">{sym}</span>
      &nbsp;
      <span class="badge" style="background:{setup_clr}22;color:{setup_clr}">{setup}</span>
      {wk52_badge}
    </div>
    <div style="text-align:right">
      <div style="color:{rs_clr};font-weight:700;font-size:13px">RS Rank: {rs_str}</div>
      <div style="color:{MUTED};font-size:12px">Top {100 - (rs_rank or 0)*100:.0f}% of universe</div>
    </div>
  </div>

  <!-- Trade plan table -->
  <h3>Trade Plan</h3>
  <table>
  <tr>
    <th>Current Price</th><th>Entry (Limit)</th><th>Stop Loss</th><th>Target 1</th><th>Target 2</th><th>Risk / Reward</th>
  </tr>
  <tr>
    <td style="color:{TEXT};font-weight:700;font-size:15px">{_p(price)}</td>
    <td style="color:{BLUE};font-weight:700">{_p(price)}</td>
    <td style="color:{RED}">{_p(stop)}<br>
        <span style="color:{MUTED};font-size:11px">{_pct((stop/price)-1) if price else ""} away</span>
    </td>
    <td style="color:{GREEN}">{_p(t1)}<br>
        <span style="color:{MUTED};font-size:11px">{_pct((t1/price)-1) if price else ""} — sell 1/3</span>
    </td>
    <td style="color:{GREEN}">{_p(t2)}<br>
        <span style="color:{MUTED};font-size:11px">{_pct((t2/price)-1) if price and t2 else "—"} — sell 1/3, trail final 1/3</span>
    </td>
    <td style="color:{rr_clr};font-weight:700;font-size:16px">{f"{rr:.1f}:1" if rr else "—"}</td>
  </tr>
  </table>

  <!-- Position sizing -->
  <h3>Position Sizing</h3>
  <table>
  <tr><th>Shares</th><th>$ Risk</th><th>% Account Risked</th><th>Total Exposure</th><th>SMA 50</th><th>SMA 200</th><th>RSI</th><th>Vol Ratio</th></tr>
  <tr>
    <td style="color:{TEXT};font-weight:700">{shares:,}</td>
    <td style="color:{RED}">${risk_usd:,.0f}</td>
    <td style="color:{RED}">{risk_usd / config.ACCOUNT_SIZE:.2%}</td>
    <td style="color:{MUTED}">${shares * price:,.0f}</td>
    <td style="color:{MUTED}">{_p(sma50)}</td>
    <td style="color:{MUTED}">{_p(sma200)}</td>
    <td style="color:{YELLOW if rsi and 40 <= rsi <= 55 else MUTED}">{f"{rsi:.0f}" if rsi else "—"}</td>
    <td style="color:{GREEN if vol_r and vol_r >= 1.5 else MUTED}">{f"{vol_r:.1f}x" if vol_r else "—"}</td>
  </tr>
  </table>

  <!-- EV -->
  {ev_html}

  <!-- Rationale -->
  <h3>Why This Trade</h3>
  <ul style="margin:6px 0;padding-left:18px">
    {rationale_html}
    {warnings_html}
  </ul>

  <!-- Options -->
  <hr>
  <h3>Options Flow (30-90 DTE, EOD Volume)</h3>
  <table>
  <tr><th>Signal</th><th>Call / Put Ratio</th><th>OTM Call %</th></tr>
  <tr>
    <td style="color:{opt_clr}">{opt_msg}</td>
    <td style="color:{GREEN if result.get("options_ok") else MUTED}">{cpr_str}</td>
    <td style="color:{MUTED}">{otm_str}</td>
  </tr>
  </table>
  <p style="color:{MUTED};font-size:11px;margin:6px 0 0 0">
    Note: EOD volume only. For live sweep detection use Unusual Whales / Cheddar Flow.
  </p>

  <!-- Buttons -->
  <div style="text-align:center;margin:20px 0 4px 0">
    <a href="{approve_url}" class="approve-btn">✅ APPROVE — Place Order</a>
    <a href="{reject_url}"  class="reject-btn">❌ Skip</a>
  </div>
  <p style="color:{MUTED};font-size:11px;text-align:center;margin:4px 0 0 0">
    Links expire when the scanner restarts. Only use buttons from the most recent email.
  </p>

</div>"""


# ── No Trade Section ──────────────────────────────────────────────────────────

def _no_trade_section(reason: str) -> str:
    return f"""
<div class="no-trade">
  <b style="color:{ORANGE}">⚠️ NO TRADE TODAY</b><br><br>
  {reason}<br><br>
  <span style="color:{MUTED}">Sit on hands. Capital preservation is a position.</span>
</div>"""


# ── Main Build ────────────────────────────────────────────────────────────────

def build_and_send(
    results:    list,
    regime:     dict,
    vix:        float | None,
    no_trade:   bool,
    no_reason:  str,
    public_url: str,
    config,
) -> bool:
    today   = datetime.now().strftime("%B %d, %Y")
    n_pass  = len(results)

    if no_trade or n_pass == 0:
        subject = f"Momentum Scanner — NO TRADE | {today}"
    else:
        subject = f"Momentum Scanner — {n_pass} Setup{'s' if n_pass > 1 else ''} Found | {today}"

    regime_html = _regime_section(regime, vix)

    if no_trade:
        body = regime_html + _no_trade_section(no_reason)
    elif n_pass == 0:
        body = regime_html + _no_trade_section("No stocks passed all 4 layers today. Raise cash. Market may need to reset.")
    else:
        cards = "".join(_stock_card(r, i, public_url, config) for i, r in enumerate(results))
        body  = regime_html + f"<h2>Passing Setups ({n_pass})</h2>" + cards

    # Trade management reminder
    body += f"""
<div class="card" style="margin-top:24px">
  <h3>Trade Management Rules</h3>
  <ul style="color:{MUTED};font-size:13px;padding-left:18px;line-height:1.8">
    <li>Entry: limit order at current close price — adjust if market gaps significantly</li>
    <li>Stop: hard stop already placed in IBKR. Do NOT move it lower.</li>
    <li>Target 1 (+{config.TARGET_1_PCT:.0%}): sell 1/3 — lock in profit, move stop to breakeven</li>
    <li>Target 2 (+{config.TARGET_2_PCT:.0%}): sell another 1/3 — then trail final 1/3 with 20 EMA until exit</li>
    <li>VIX > 25: reduce position size by 50% on breakouts</li>
    <li>Do NOT add to losers. If stopped out — walk away.</li>
  </ul>
</div>"""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{CSS}</style></head>
<body>
<div class="wrap">
<h1>Momentum Investing Scanner — {today}</h1>
{body}
<p style="color:{MUTED};font-size:11px;margin-top:30px;text-align:center">
  Automated scan. Win rates are historical estimates, not guarantees. Always verify before trading.
</p>
</div>
</body>
</html>"""

    return _send(subject, html, config.GMAIL_SENDER, config.GMAIL_APP_PASSWORD, config.GMAIL_RECIPIENT)


def _send(subject: str, html: str, sender: str, password: str, recipient: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = sender
        msg["To"]      = recipient
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(sender, password)
            s.sendmail(sender, recipient, msg.as_string())

        print(f"  [Email] Sent: {subject}")
        return True

    except Exception as e:
        print(f"  [Email] Failed: {e}")
        return False
