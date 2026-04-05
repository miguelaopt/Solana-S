"""
dashboard.py — Solana Syndicate Live Dashboard Server

FastAPI server that serves a real-time monitoring dashboard.
Reads from:
  - status.json    (written by sniper_bot.py every 5s)
  - config.json    (wallet list, risk params)
  - wallet_history.db (trade history via SQLite)

Run: python dashboard.py
Access: http://YOUR_VPS_IP:8080
"""

import json
import os
import time
from pathlib import Path
from datetime import datetime, timezone

import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

ROOT       = Path(__file__).parent
STATUS_F   = ROOT / "status.json"
CONFIG_F   = ROOT / "config.json"
DB_PATH    = ROOT / "data" / "wallet_history.db"

app = FastAPI(title="Solana Syndicate Dashboard", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─── API Endpoints ────────────────────────────────────────────────────────────
@app.get("/api/status")
async def api_status():
    try:
        if STATUS_F.exists():
            data = json.loads(STATUS_F.read_text())
            # Check if status is stale (bot crashed)
            age = time.time() - data.get("updated_at", 0)
            data["status_age_seconds"] = round(age)
            data["bot_responsive"]     = age < 15
            return JSONResponse(data)
        return JSONResponse({"online": False, "bot_responsive": False})
    except Exception as e:
        return JSONResponse({"online": False, "error": str(e)})

@app.get("/api/config")
async def api_config():
    try:
        return JSONResponse(json.loads(CONFIG_F.read_text()))
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get("/api/trades")
async def api_trades(limit: int = 50):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT wallet, token_mint, roi_pct, is_win,
                          entry_price_usd, exit_price_usd, trade_ts, source
                   FROM wallet_trades
                   ORDER BY trade_ts DESC
                   LIMIT ?""",
                (limit,)
            )
            rows = await cursor.fetchall()
            return JSONResponse([dict(r) for r in rows])
    except Exception:
        return JSONResponse([])

@app.get("/api/wallets")
async def api_wallets():
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT wallet, win_rate_all, win_rate_7d, total_trades,
                          unique_tokens, tx_per_day_avg, status, last_checked_ts
                   FROM wallet_meta
                   ORDER BY win_rate_all DESC"""
            )
            rows = await cursor.fetchall()
            return JSONResponse([dict(r) for r in rows])
    except Exception:
        return JSONResponse([])

@app.post("/api/wallets/add")
async def api_add_wallet(body: dict):
    wallet = body.get("wallet", "").strip()
    if not wallet or len(wallet) < 32:
        raise HTTPException(400, "Invalid wallet address")
    cfg = json.loads(CONFIG_F.read_text())
    if wallet not in cfg["TARGET_WALLETS"]:
        cfg["TARGET_WALLETS"].append(wallet)
        CONFIG_F.write_text(json.dumps(cfg, indent=2))
    return {"ok": True, "total": len(cfg["TARGET_WALLETS"])}

@app.delete("/api/wallets/{wallet}")
async def api_remove_wallet(wallet: str):
    cfg = json.loads(CONFIG_F.read_text())
    if wallet in cfg["TARGET_WALLETS"]:
        cfg["TARGET_WALLETS"].remove(wallet)
        if wallet not in cfg["PRUNED_WALLETS"]:
            cfg["PRUNED_WALLETS"].append(wallet)
        CONFIG_F.write_text(json.dumps(cfg, indent=2))
    return {"ok": True}

@app.get("/api/stats")
async def api_stats():
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            total = (await (await db.execute("SELECT COUNT(*) FROM wallet_trades")).fetchone())[0]
            wins  = (await (await db.execute("SELECT COUNT(*) FROM wallet_trades WHERE is_win=1")).fetchone())[0]
            wallets = (await (await db.execute("SELECT COUNT(*) FROM wallet_meta WHERE status='active'")).fetchone())[0]
            avg_roi = (await (await db.execute("SELECT AVG(roi_pct) FROM wallet_trades")).fetchone())[0] or 0
            return JSONResponse({
                "total_trades": total,
                "win_count":    wins,
                "win_rate":     round(wins / total, 3) if total > 0 else 0,
                "avg_roi_pct":  round(avg_roi, 2),
                "active_wallets": wallets,
            })
    except Exception:
        return JSONResponse({"total_trades": 0, "win_rate": 0, "avg_roi_pct": 0})


# ─── Dashboard HTML ───────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Solana Syndicate — Control Room</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:        #080b0f;
    --bg2:       #0d1117;
    --bg3:       #131920;
    --border:    #1e2d3d;
    --green:     #00ff88;
    --green-dim: #00cc6a;
    --red:       #ff3d5a;
    --yellow:    #ffcc00;
    --blue:      #00aaff;
    --text:      #c8d6e8;
    --text-dim:  #5a7a96;
    --mono:      'Space Mono', monospace;
    --sans:      'Syne', sans-serif;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    font-size: 13px;
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* Animated scan line */
  body::after {
    content: '';
    position: fixed;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, var(--green), transparent);
    animation: scan 4s ease-in-out infinite;
    pointer-events: none;
    z-index: 999;
  }
  @keyframes scan { 0%,100%{opacity:0} 50%{opacity:.6} }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--bg2);
    position: sticky; top: 0; z-index: 100;
  }

  .logo {
    font-family: var(--sans);
    font-size: 18px;
    font-weight: 800;
    letter-spacing: -0.5px;
  }
  .logo span { color: var(--green); }

  .header-right { display: flex; align-items: center; gap: 16px; }

  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
    text-transform: uppercase;
    border: 1px solid;
  }
  .status-pill.online  { color: var(--green); border-color: var(--green); background: rgba(0,255,136,.06); }
  .status-pill.offline { color: var(--red);   border-color: var(--red);   background: rgba(255,61,90,.06); }
  .status-pill.polling { color: var(--yellow);border-color: var(--yellow);background: rgba(255,204,0,.06); }

  .dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: currentColor;
  }
  .dot.pulse { animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.4;transform:scale(.8)} }

  .refresh-ts { color: var(--text-dim); font-size: 11px; }

  main {
    padding: 20px 24px;
    max-width: 1400px;
    margin: 0 auto;
  }

  /* Stat cards row */
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }

  .card {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    position: relative;
    overflow: hidden;
  }
  .card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
  }
  .card.green::before  { background: var(--green); }
  .card.red::before    { background: var(--red); }
  .card.blue::before   { background: var(--blue); }
  .card.yellow::before { background: var(--yellow); }

  .card-label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
  .card-value {
    font-family: var(--sans);
    font-size: 26px;
    font-weight: 800;
    line-height: 1;
  }
  .card-sub { font-size: 11px; color: var(--text-dim); margin-top: 4px; }

  /* Two-column grid */
  .grid2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 16px;
  }
  @media (max-width: 900px) { .grid2 { grid-template-columns: 1fr; } }

  .panel {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }

  .panel-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--bg3);
  }
  .panel-title {
    font-family: var(--sans);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.3px;
  }
  .badge {
    background: var(--border);
    color: var(--text-dim);
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
  }

  /* Table */
  .tbl { width: 100%; border-collapse: collapse; }
  .tbl th {
    text-align: left;
    padding: 8px 12px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    border-bottom: 1px solid var(--border);
    font-weight: 400;
  }
  .tbl td {
    padding: 10px 12px;
    border-bottom: 1px solid rgba(30,45,61,.5);
    vertical-align: middle;
  }
  .tbl tr:last-child td { border-bottom: none; }
  .tbl tr:hover td { background: rgba(255,255,255,.02); }

  .mono { font-family: var(--mono); font-size: 12px; }
  .addr { color: var(--blue); }
  .win  { color: var(--green); }
  .loss { color: var(--red); }
  .neutral { color: var(--text-dim); }

  /* Events feed */
  .events { max-height: 320px; overflow-y: auto; padding: 8px 0; }
  .event {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 8px 14px;
    border-bottom: 1px solid rgba(30,45,61,.4);
    animation: fadeIn .3s ease;
  }
  @keyframes fadeIn { from{opacity:0;transform:translateY(-4px)} to{opacity:1;transform:none} }
  .event-icon { font-size: 14px; flex-shrink: 0; margin-top: 1px; }
  .event-msg  { flex: 1; line-height: 1.5; }
  .event-ts   { color: var(--text-dim); font-size: 10px; flex-shrink: 0; }

  /* Positions */
  .pos-card {
    margin: 8px 12px;
    background: var(--bg3);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
  }
  .pos-mint { color: var(--green); font-size: 12px; }
  .pos-meta { color: var(--text-dim); font-size: 11px; margin-top: 4px; }
  .pos-pnl  { font-size: 18px; font-family: var(--sans); font-weight: 700; }
  .pos-bar  {
    height: 3px;
    background: var(--border);
    border-radius: 2px;
    margin-top: 8px;
    overflow: hidden;
  }
  .pos-bar-fill {
    height: 100%;
    border-radius: 2px;
    background: var(--green);
    transition: width .5s ease;
  }

  /* Wallet list */
  .wallet-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-bottom: 1px solid rgba(30,45,61,.4);
  }
  .wallet-row:last-child { border-bottom: none; }
  .wallet-addr { color: var(--blue); font-size: 12px; }
  .wallet-stats { display: flex; gap: 12px; align-items: center; }
  .wr-badge {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
  }
  .wr-high { background: rgba(0,255,136,.12); color: var(--green); }
  .wr-mid  { background: rgba(255,204,0,.12);  color: var(--yellow); }
  .wr-low  { background: rgba(255,61,90,.12);  color: var(--red); }

  /* Add wallet form */
  .add-wallet-form {
    display: flex;
    gap: 8px;
    padding: 12px 14px;
    border-top: 1px solid var(--border);
    background: var(--bg3);
  }
  .add-wallet-form input {
    flex: 1;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 6px 10px;
    color: var(--text);
    font-family: var(--mono);
    font-size: 12px;
    outline: none;
  }
  .add-wallet-form input:focus { border-color: var(--green); }
  .btn {
    background: var(--green);
    color: #000;
    border: none;
    border-radius: 4px;
    padding: 6px 14px;
    cursor: pointer;
    font-family: var(--sans);
    font-weight: 700;
    font-size: 12px;
    transition: opacity .15s;
  }
  .btn:hover { opacity: .85; }
  .btn.danger { background: var(--red); color: #fff; padding: 3px 8px; font-size: 11px; }

  /* Empty state */
  .empty {
    padding: 32px;
    text-align: center;
    color: var(--text-dim);
    font-size: 12px;
  }

  /* Full-width section */
  .full-panel { margin-bottom: 16px; }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: var(--bg2); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }
</style>
</head>
<body>

<header>
  <div class="logo">SOLANA <span>SYNDICATE</span></div>
  <div class="header-right">
    <span id="mode-pill" class="status-pill offline">
      <span class="dot"></span>
      <span id="mode-text">OFFLINE</span>
    </span>
    <span class="refresh-ts" id="refresh-ts">—</span>
  </div>
</header>

<main>

  <!-- Stat Cards -->
  <div class="cards" id="stat-cards">
    <div class="card green">
      <div class="card-label">Uptime</div>
      <div class="card-value" id="c-uptime">—</div>
      <div class="card-sub">bot running</div>
    </div>
    <div class="card blue">
      <div class="card-label">Tracked Wallets</div>
      <div class="card-value" id="c-wallets">—</div>
      <div class="card-sub">via WebSocket / polling</div>
    </div>
    <div class="card green">
      <div class="card-label">Open Positions</div>
      <div class="card-value" id="c-positions">—</div>
      <div class="card-sub">active trades</div>
    </div>
    <div class="card yellow">
      <div class="card-label">Total Buys</div>
      <div class="card-value" id="c-buys">—</div>
      <div class="card-sub">executed</div>
    </div>
    <div class="card blue">
      <div class="card-label">Total Sells</div>
      <div class="card-value" id="c-sells">—</div>
      <div class="card-sub">executed</div>
    </div>
    <div class="card green">
      <div class="card-label">DB Win Rate</div>
      <div class="card-value" id="c-wr">—</div>
      <div class="card-sub">across all tracked wallets</div>
    </div>
  </div>

  <!-- Row 1: Positions + Events -->
  <div class="grid2">

    <!-- Open Positions -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Open Positions</span>
        <span class="badge" id="pos-count">0</span>
      </div>
      <div id="positions-body">
        <div class="empty">No open positions</div>
      </div>
    </div>

    <!-- Live Events Feed -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Live Events</span>
        <span class="badge" id="events-count">0</span>
      </div>
      <div class="events" id="events-body">
        <div class="empty">Waiting for events...</div>
      </div>
    </div>

  </div>

  <!-- Row 2: Wallets (full width) -->
  <div class="panel full-panel">
    <div class="panel-header">
      <span class="panel-title">Target Wallets</span>
      <span class="badge" id="wallets-count">0</span>
    </div>
    <div id="wallets-body">
      <div class="empty">No wallets loaded — run wallet_scraper.py or add manually below</div>
    </div>
    <div class="add-wallet-form">
      <input type="text" id="new-wallet-input" placeholder="Paste Solana wallet address to add manually..." />
      <button class="btn" onclick="addWallet()">+ Add Wallet</button>
    </div>
  </div>

  <!-- Row 3: Recent Trades (full width) -->
  <div class="panel full-panel">
    <div class="panel-header">
      <span class="panel-title">Trade History (SQLite)</span>
      <span class="badge" id="trades-count">0</span>
    </div>
    <div style="overflow-x:auto">
      <table class="tbl">
        <thead>
          <tr>
            <th>Time</th>
            <th>Wallet</th>
            <th>Token Mint</th>
            <th>ROI</th>
            <th>Result</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody id="trades-body">
          <tr><td colspan="8" style="text-align:center;color:var(--text-dim);padding:24px">No trades recorded yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</main>

<script>
const fmt = {
  addr: a => a ? a.slice(0,6)+'...'+a.slice(-4) : '—',
  time: ts => ts ? new Date(ts*1000).toLocaleTimeString() : '—',
  date: ts => ts ? new Date(ts*1000).toLocaleString() : '—',
  uptime: s => {
    if (!s) return '—';
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${ss}s`;
    return `${ss}s`;
  },
  pct: n => n != null ? (n > 0 ? '+' : '') + n.toFixed(2) + '%' : '—',
  pctClass: n => n > 0 ? 'win' : n < 0 ? 'loss' : 'neutral',
};

const eventIcons = {
  signal: '⚡', buy: '🟢', sell: '🔴', rug: '🚫', error: '❌', info: '💬'
};

let lastEventCount = 0;

async function fetchStatus() {
  try {
    const s = await fetch('/api/status').then(r=>r.json());

    // Header pill
    const pill = document.getElementById('mode-pill');
    const modeText = document.getElementById('mode-text');
    pill.className = 'status-pill ' + (
      !s.online || !s.bot_responsive ? 'offline' :
      s.polling_mode ? 'polling' : 'online'
    );
    modeText.textContent = !s.online || !s.bot_responsive ? 'OFFLINE' :
      s.polling_mode ? 'POLLING' : 'WS LIVE';
    const dot = pill.querySelector('.dot');
    dot.className = 'dot' + (s.bot_responsive ? ' pulse' : '');

    document.getElementById('refresh-ts').textContent =
      'updated ' + fmt.time(s.updated_at);

    // Cards
    document.getElementById('c-uptime').textContent    = fmt.uptime(s.uptime_seconds);
    document.getElementById('c-wallets').textContent   = (s.tracked_wallets||[]).length;
    document.getElementById('c-positions').textContent = Object.keys(s.open_positions||{}).length;
    document.getElementById('c-buys').textContent      = s.total_buys || 0;
    document.getElementById('c-sells').textContent     = s.total_sells || 0;

    // Positions
    const pos = s.open_positions || {};
    const posBody = document.getElementById('positions-body');
    document.getElementById('pos-count').textContent = Object.keys(pos).length;
    if (Object.keys(pos).length === 0) {
      posBody.innerHTML = '<div class="empty">No open positions</div>';
    } else {
      posBody.innerHTML = Object.values(pos).map(p => {
        const mult = p.current_mult || 1;
        const pnl  = p.unrealized_pnl || 0;
        const barW = Math.min(Math.max(((mult-1)/3)*100, 0), 100);
        const phase = p.phase === 1 ? 'Waiting for TP (2×)' : `Trail drop: ${p.trail_drop_pct||0}%`;
        return `
          <div class="pos-card">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span class="pos-mint">${fmt.addr(p.mint)}</span>
              <span class="pos-pnl ${pnl>=0?'win':'loss'}">${fmt.pct(pnl)}</span>
            </div>
            <div class="pos-meta">
              From: ${fmt.addr(p.wallet)} | Spent: ${p.sol_spent||'?'} SOL | ${phase}
            </div>
            <div class="pos-meta" style="color:var(--text)">
              ${mult.toFixed(3)}× | Phase ${p.phase}
            </div>
            <div class="pos-bar">
              <div class="pos-bar-fill" style="width:${barW}%;background:${pnl>=0?'var(--green)':'var(--red)'}"></div>
            </div>
          </div>`;
      }).join('');
    }

    // Events
    const events = (s.recent_events || []);
    document.getElementById('events-count').textContent = events.length;
    if (events.length !== lastEventCount) {
      lastEventCount = events.length;
      const evBody = document.getElementById('events-body');
      if (events.length === 0) {
        evBody.innerHTML = '<div class="empty">Waiting for events...</div>';
      } else {
        evBody.innerHTML = events.map(e => `
          <div class="event">
            <span class="event-icon">${eventIcons[e.kind]||'•'}</span>
            <span class="event-msg">${e.message}</span>
            <span class="event-ts">${fmt.time(e.ts)}</span>
          </div>`).join('');
      }
    }

  } catch(e) {
    document.getElementById('mode-pill').className = 'status-pill offline';
    document.getElementById('mode-text').textContent = 'OFFLINE';
  }
}

async function fetchWallets() {
  try {
    const [cfg, meta] = await Promise.all([
      fetch('/api/config').then(r=>r.json()),
      fetch('/api/wallets').then(r=>r.json()),
    ]);

    const targets  = cfg.TARGET_WALLETS || [];
    const metaMap  = Object.fromEntries(meta.map(m => [m.wallet, m]));

    document.getElementById('wallets-count').textContent = targets.length;

    const body = document.getElementById('wallets-body');
    if (targets.length === 0) {
      body.innerHTML = '<div class="empty">No wallets — run wallet_scraper.py or add below</div>';
    } else {
      body.innerHTML = targets.map(w => {
        const m   = metaMap[w] || {};
        const wr  = m.win_rate_all;
        const wrc = wr >= 0.6 ? 'wr-high' : wr >= 0.4 ? 'wr-mid' : 'wr-low';
        return `
          <div class="wallet-row">
            <span class="wallet-addr mono">${w.slice(0,14)}...${w.slice(-6)}</span>
            <div class="wallet-stats">
              ${wr != null ? `<span class="wr-badge ${wrc}">${(wr*100).toFixed(0)}% WR</span>` : ''}
              ${m.total_trades ? `<span class="neutral">${m.total_trades} trades</span>` : ''}
              ${m.unique_tokens ? `<span class="neutral">${m.unique_tokens} tokens</span>` : ''}
              <button class="btn danger" onclick="removeWallet('${w}')">✕</button>
            </div>
          </div>`;
      }).join('');
    }

    // DB win rate card
    const stats = await fetch('/api/stats').then(r=>r.json());
    document.getElementById('c-wr').textContent =
      stats.win_rate ? (stats.win_rate*100).toFixed(0)+'%' : '—';
  } catch(e) {}
}

async function fetchTrades() {
  try {
    const trades = await fetch('/api/trades?limit=30').then(r=>r.json());
    document.getElementById('trades-count').textContent = trades.length;
    const body = document.getElementById('trades-body');
    if (!trades.length) {
      body.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-dim);padding:24px">No trades recorded yet</td></tr>';
      return;
    }
    body.innerHTML = trades.map(t => `
      <tr>
        <td class="neutral">${fmt.time(t.trade_ts)}</td>
        <td class="addr mono">${fmt.addr(t.wallet)}</td>
        <td class="addr mono">${fmt.addr(t.token_mint)}</td>
        <td class="${fmt.pctClass(t.roi_pct)}">${fmt.pct(t.roi_pct)}</td>
        <td class="${t.is_win?'win':'loss'}">${t.is_win?'WIN':'LOSS'}</td>
        <td class="neutral">${t.entry_price_usd?'$'+t.entry_price_usd.toFixed(8):'—'}</td>
        <td class="neutral">${t.exit_price_usd?'$'+t.exit_price_usd.toFixed(8):'—'}</td>
        <td class="neutral">${t.source||'rpc'}</td>
      </tr>`).join('');
  } catch(e) {}
}

async function addWallet() {
  const input = document.getElementById('new-wallet-input');
  const w = input.value.trim();
  if (!w || w.length < 32) { alert('Invalid wallet address'); return; }
  try {
    const r = await fetch('/api/wallets/add', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({wallet: w})
    }).then(r=>r.json());
    if (r.ok) { input.value = ''; fetchWallets(); }
  } catch(e) { alert('Error adding wallet'); }
}

async function removeWallet(w) {
  if (!confirm(`Remove wallet ${w.slice(0,14)}...?`)) return;
  await fetch('/api/wallets/'+w, {method:'DELETE'});
  fetchWallets();
}

// Refresh intervals
fetchStatus();
fetchWallets();
fetchTrades();
setInterval(fetchStatus, 3000);
setInterval(fetchWallets, 15000);
setInterval(fetchTrades, 20000);
</script>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


# ─── Run ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")