# Solana Autonomous Memecoin Syndicate

A fully autonomous, two-process Python trading system for Solana memecoins.
Discovers its own target wallets, executes copy-trades with MEV protection, and manages positions automatically.

---

## Architecture Overview

```
wallet_scraper.py   (runs every 6h)          sniper_bot.py   (runs 24/7)
─────────────────────────────────            ──────────────────────────────
DexScreener → hot pools (24h)                Helius WS → logsSubscribe
     ↓                                              ↓
BirdEye → top traders per pool               Detect: SOL → unknown SPL token
     ↓                                              ↓
Helius RPC → filter MEV bots                 Rug check (mint auth revoked?)
     ↓                                              ↓
SQLite → win rate > 60%                      Jupiter v6 → best swap route
     ↓                                              ↓
config.json ← add/prune wallets              Jito bundle → atomic MEV-safe tx
                    ↑                               ↓
                    └──── hot-reload (60s) ─── Position manager (Moonbag)
```

---

## Project Structure

```
solana-syndicate/
│
├── .env                      # Private keys & API URLs (NEVER commit)
├── config.json               # Shared state: wallets, risk params, filters
├── requirements.txt          # Python dependencies
│
├── wallet_scraper.py         # Auto-Discovery Engine (background process)
├── sniper_bot.py             # Execution Sniper (async 24/7 engine)
├── utils.py                  # Shared: logging, config I/O, SQLite schema
│
├── modules/
│   ├── __init__.py           # Empty — marks folder as Python package
│   ├── rpc_client.py         # Helius WebSocket engine (reconnect + subscriptions)
│   ├── jupiter_client.py     # Jupiter v6 quote + swap builder
│   ├── jito_client.py        # Jito bundle assembler (MEV protection)
│   ├── rug_checker.py        # On-chain mint authority + LP checks
│   └── position_manager.py   # Moonbag TP + trailing stop logic
│
├── data/
│   └── wallet_history.db     # SQLite — wallet trade history & win rates
│
├── logs/
│   ├── sniper.log
│   └── scraper.log
│
└── deploy/
    ├── sniper.service        # systemd unit — sniper bot (continuous)
    ├── scraper.service       # systemd unit — scraper (oneshot)
    └── scraper.timer         # systemd timer — fires every 6h
```

> `data/` and `logs/` are created automatically on first run by `utils.py`.
> `modules/__init__.py` is an empty file.

---

## Prerequisites

| Requirement | Where to get |
|---|---|
| Python 3.11+ | `sudo apt install python3.11` |
| Helius RPC (HTTP + WS) | [helius.dev](https://helius.dev) — free tier works |
| BirdEye API key | [birdeye.so](https://birdeye.so) — Starter plan ($50/mo) |
| Phantom Wallet private key | Phantom → Settings → Export Private Key |
| Ubuntu 22.04 VPS | Hetzner / Contabo (4 vCPU, 8GB RAM minimum) |

---

## Step-by-Step Setup

### 1. VPS Initial Setup

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git build-essential curl

# Create a dedicated non-root user
sudo useradd -m -s /bin/bash syndicate
sudo su - syndicate
```

### 2. Clone / Upload Project

```bash
mkdir -p ~/solana-syndicate
cd ~/solana-syndicate

# Upload your files or clone from your private repo
# git clone https://github.com/YOU/solana-syndicate.git .
```

### 3. Python Virtual Environment

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure `.env`

```bash
nano .env
```

Paste and fill in:

```env
WALLET_PRIVATE_KEY=your_base58_phantom_private_key_here
HELIUS_RPC_HTTP=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
HELIUS_RPC_WS=wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY
BIRDEYE_API_KEY=your_birdeye_api_key_here
JITO_BLOCK_ENGINE_URL=https://mainnet.block-engine.jito.wtf/api/v1/bundles
JITO_TIP_LAMPORTS=150000
```

```bash
chmod 600 .env   # Restrict read access to owner only
```

### 5. Get a Helius RPC Node

1. Go to [helius.dev](https://helius.dev) → Create account → **New Project**
2. Select **Mainnet Beta**
3. Enable **Enhanced WebSockets** in the dashboard (required for `logsSubscribe`)
4. Copy your **HTTP URL** and **WebSocket URL** into `.env`

> **Free tier** supports up to ~10 concurrent WebSocket subscriptions — sufficient for bootstrapping.
> Upgrade to **Growth** plan when tracking 20+ wallets.

### 6. Verify Jito Integration

```bash
curl -X POST https://mainnet.block-engine.jito.wtf/api/v1/bundles \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"getTipAccounts","params":[]}'
# Expected: JSON response with 8 tip account addresses
```

### 7. Initialize the Database & Test Scraper

```bash
cd ~/solana-syndicate
source venv/bin/activate

# Run scraper manually first to populate wallet history
python wallet_scraper.py
```

> **Important — Observation Phase:**
> On first run the scraper will log that it is in the 12–18 hour observation phase.
> `TARGET_WALLETS` will be empty until enough trade history is collected in SQLite
> to calculate reliable win rates. This is expected behaviour.

After 1–2 scraper cycles, check the config:

```bash
python3 -c "import json; cfg=json.load(open('config.json')); print(cfg['TARGET_WALLETS'])"
```

### 8. Test Sniper in Paper Mode

Before risking real SOL, set a tiny buy amount:

```bash
# In config.json temporarily set:
#   "buy_amount_sol": 0.000001

python sniper_bot.py
```

Watch the logs — you should see WebSocket connection confirmations and wallet subscription messages.

### 9. Deploy with systemd (Production)

```bash
# Copy service files
sudo cp deploy/sniper.service  /etc/systemd/system/sniper.service
sudo cp deploy/scraper.service /etc/systemd/system/scraper.service
sudo cp deploy/scraper.timer   /etc/systemd/system/scraper.timer

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable --now sniper.service
sudo systemctl enable --now scraper.timer

# Verify
sudo systemctl status sniper.service
sudo systemctl status scraper.timer
```

### 10. Monitor Live Logs

```bash
# Sniper live feed
sudo journalctl -u sniper -f --output=cat

# Scraper live feed (fires every 6h)
sudo journalctl -u scraper -f --output=cat

# Both simultaneously
sudo journalctl -u sniper -u scraper -f --output=cat
```

---

## Alternative: pm2 (Simpler for Development)

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g pm2

source ~/solana-syndicate/venv/bin/activate
cd ~/solana-syndicate

pm2 start "python sniper_bot.py"    --name sniper
pm2 start "python wallet_scraper.py" --name scraper --cron "0 */6 * * *" --no-autorestart
pm2 save
pm2 startup   # Follow the printed command

pm2 logs sniper   # Live logs
pm2 monit         # Dashboard
```

---

## systemd Service Files

### `deploy/sniper.service`

```ini
[Unit]
Description=Solana Memecoin Sniper Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=syndicate
WorkingDirectory=/home/syndicate/solana-syndicate
EnvironmentFile=/home/syndicate/solana-syndicate/.env
ExecStart=/home/syndicate/solana-syndicate/venv/bin/python sniper_bot.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=sniper

[Install]
WantedBy=multi-user.target
```

### `deploy/scraper.service`

```ini
[Unit]
Description=Wallet Auto-Discovery Scraper
After=network-online.target

[Service]
Type=oneshot
User=syndicate
WorkingDirectory=/home/syndicate/solana-syndicate
EnvironmentFile=/home/syndicate/solana-syndicate/.env
ExecStart=/home/syndicate/solana-syndicate/venv/bin/python wallet_scraper.py
StandardOutput=journal
StandardError=journal
SyslogIdentifier=scraper
```

### `deploy/scraper.timer`

```ini
[Unit]
Description=Run Wallet Scraper Every 6 Hours

[Timer]
OnBootSec=2min
OnCalendar=*-*-* 00,06,12,18:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

---

## Risk Management Summary

| Parameter | Default | Description |
|---|---|---|
| `buy_amount_sol` | 0.1 | SOL spent per trade |
| `slippage_bps_entry` | 1000 | 10% entry slippage |
| `slippage_bps_exit` | 2000 | 20% exit slippage (dumps) |
| `max_positions` | 5 | Maximum concurrent open trades |
| `max_price_impact_pct` | 25.0 | Abort if Jupiter impact > 25% |
| `take_profit_multiplier` | 2.0 | Sell 50% at 2x entry (100% profit) |
| `trailing_stop_pct` | 0.25 | Trail remaining 50% with 25% stop |
| `jito_tip_lamports` | 150000 | ~$0.02 tip; raise during congestion |

---

## Wallet Filtration Criteria

A wallet must pass **all** of the following to enter `TARGET_WALLETS`:

| Check | Threshold | Purpose |
|---|---|---|
| TX per day | < 500 | Filters MEV bots & Jito validators |
| Unique tokens traded | ≥ 3 | Filters one-hit wonders |
| Minimum trade history | ≥ 5 trades | Ensures statistical validity |
| Overall win rate | ≥ 60% | Core signal quality filter |
| Funding source | Not dev wallet | Filters wash traders |

Wallets already in `TARGET_WALLETS` are pruned if their **7-day win rate drops below 40%**.

---

## Security Checklist

- [ ] `.env` has `chmod 600` permissions
- [ ] `.env` is in `.gitignore` — never committed
- [ ] Bot runs as non-root `syndicate` user
- [ ] `data/` and `logs/` are in `.gitignore`
- [ ] Private key is only stored in `.env` — not hardcoded anywhere
- [ ] VPS firewall (UFW) enabled — only SSH port 22 open

```bash
# .gitignore
.env
data/
logs/
__pycache__/
venv/
*.pyc
```

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| WebSocket disconnects constantly | Helius free tier rate limit | Reduce wallet count; upgrade plan |
| `TARGET_WALLETS` stays empty after 24h | Bootstrap phase / BirdEye rate limits | Wait 2–3 scraper cycles; check scraper logs |
| Jito bundles never land | Tip too low | Raise `JITO_TIP_LAMPORTS` to 500000+ |
| Jupiter returns no quote | Token has no liquidity route | Expected for very new/illiquid mints |
| `429 Too Many Requests` from BirdEye | Free tier limit | Add `asyncio.sleep()` delays or upgrade plan |
| Rug check always fails | Helius WS timeout | Check RPC URL; verify Enhanced WS is enabled |

---

## Disclaimer

This software is for educational and research purposes. Memecoin trading carries extreme financial risk. Never trade with funds you cannot afford to lose entirely. The authors accept no liability for financial losses.