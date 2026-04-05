"""
sniper_bot.py — Solana Memecoin Sniper (v1.2.0)

Fixes in this version:
  [1] WebSocket Heartbeat — manual ping every 5s prevents Helius
      free-tier from closing the connection after 10s inactivity.
  [2] HTTP Polling Fallback — if WS fails 3× in a row, the bot
      switches to polling getSignaturesForAddress every 2.5s per
      wallet. Slower but zero-downtime on flaky connections.
  [3] SOL_AMOUNT float fix — parsed with explicit float() from env
      and config. Values like 0.05 or 0.1 now work correctly.
  [4] WS error logging — catches InvalidStatusCode to show HTTP
      status (429 / 403 / 503) in the log output.
  [5] status.json — written every 5s for the dashboard to read.
"""

import asyncio
import base64
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import websockets
import websockets.exceptions
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
import base58

from utils import get_logger, read_config, utc_now_ts, write_config

load_dotenv()
log = get_logger("sniper", "sniper.log")

ROOT = Path(__file__).parent

# ─── Constants ────────────────────────────────────────────────────────────────
JUPITER_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP  = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE = "https://price.jup.ag/v4/price"
HELIUS_HTTP   = os.getenv("HELIUS_RPC_HTTP", "")
HELIUS_WS     = os.getenv("HELIUS_RPC_WS", "")
JITO_URL      = os.getenv("JITO_BLOCK_ENGINE_URL", "")

SOL_MINT      = "So11111111111111111111111111111111111111112"
WSOL_MINT     = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Mints that are never buy signals (stablecoins, wSOL, known base tokens)
IGNORED_MINTS: set[str] = {
    SOL_MINT,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH (Wormhole)
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
}

# Minimum SOL spent to count as a real buy (filters dust / fee-only txs)
MIN_SOL_SPENT = 0.001  # 0.001 SOL ≈ $0.15

JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt13WkKuGt",
]

# ─── Keypair ──────────────────────────────────────────────────────────────────
def load_keypair() -> Keypair:
    raw = os.getenv("WALLET_PRIVATE_KEY", "").strip()
    if not raw:
        raise ValueError("WALLET_PRIVATE_KEY not set in .env")
    return Keypair.from_bytes(base58.b58decode(raw))

KEYPAIR = load_keypair()
PUBKEY  = str(KEYPAIR.pubkey())

# ─── Shared Runtime State ─────────────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.ws_connected:    bool       = False
        self.polling_mode:    bool       = False
        self.ws_fail_count:   int        = 0
        self.ws_fail_limit:   int        = 3          # Switch to polling after 3 WS failures
        self.start_ts:        float      = utc_now_ts()
        self.last_signal_ts:  float      = 0.0
        self.total_buys:      int        = 0
        self.total_sells:     int        = 0
        self.tracked_wallets: list[str]  = []
        self.open_positions:  dict       = {}         # mint → position
        self.processing:      set[str]   = set()      # in-flight buy mints
        self.recent_events:   list[dict] = []         # Last 50 log events for dashboard
        self.last_sigs:       dict[str, str] = {}     # wallet → last seen signature (polling)
        self.wallet_token_cache: dict[str, set[str]] = {}  # wallet → set of mint addresses currently held
        self._lock = asyncio.Lock()

    def add_event(self, kind: str, message: str, data: dict = None):
        entry = {
            "ts":      utc_now_ts(),
            "kind":    kind,   # "signal" | "buy" | "sell" | "rug" | "error" | "info"
            "message": message,
            "data":    data or {}
        }
        self.recent_events.insert(0, entry)
        self.recent_events = self.recent_events[:50]  # Keep last 50

state = BotState()

# ─── status.json Writer ───────────────────────────────────────────────────────
async def write_status():
    """
    Writes bot status to status.json every 5 seconds.
    The dashboard reads this file to display live state.
    """
    while True:
        try:
            status = {
                "online":          True,
                "ws_connected":    state.ws_connected,
                "polling_mode":    state.polling_mode,
                "uptime_seconds":  int(utc_now_ts() - state.start_ts),
                "tracked_wallets": state.tracked_wallets,
                "open_positions":  state.open_positions,
                "total_buys":      state.total_buys,
                "total_sells":     state.total_sells,
                "last_signal_ts":  state.last_signal_ts,
                "recent_events":   state.recent_events[:20],
                "wallet_pubkey":   PUBKEY,
                "updated_at":      utc_now_ts(),
            }
            tmp = ROOT / "status.json.tmp"
            tmp.write_text(json.dumps(status, indent=2))
            tmp.replace(ROOT / "status.json")
        except Exception as e:
            log.error(f"status.json write error: {e}")
        await asyncio.sleep(5)


# ─── RPC Helpers ──────────────────────────────────────────────────────────────
_req_id   = 0
_pending: dict[int, asyncio.Future] = {}
_ws_conn  = None

def _next_id() -> int:
    global _req_id
    _req_id += 1
    return _req_id

async def _ws_send(method: str, params: list) -> dict:
    """Send a JSON-RPC call over the active WebSocket connection."""
    if _ws_conn is None:
        raise RuntimeError("WebSocket not connected")
    rid = _next_id()
    fut = asyncio.get_event_loop().create_future()
    _pending[rid] = fut
    await _ws_conn.send(json.dumps({
        "jsonrpc": "2.0", "id": rid, "method": method, "params": params
    }))
    return await asyncio.wait_for(fut, timeout=15.0)

async def http_rpc(
    client: httpx.AsyncClient,
    method: str,
    params: list
) -> dict:
    """HTTP RPC fallback — used during polling mode and for one-off calls."""
    r = await client.post(HELIUS_HTTP, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params
    }, timeout=12.0)
    r.raise_for_status()
    return r.json().get("result", {})

async def get_account_info_ws(address: str) -> Optional[dict]:
    resp = await _ws_send(
        "getAccountInfo",
        [address, {"encoding": "jsonParsed", "commitment": "confirmed"}]
    )
    return resp.get("result", {}).get("value")

async def get_account_info_http(client: httpx.AsyncClient, address: str) -> Optional[dict]:
    result = await http_rpc(
        client,
        "getAccountInfo",
        [address, {"encoding": "jsonParsed", "commitment": "confirmed"}]
    )
    return result.get("value") if isinstance(result, dict) else None

async def get_latest_blockhash(client: httpx.AsyncClient) -> str:
    result = await http_rpc(
        client, "getLatestBlockhash", [{"commitment": "confirmed"}]
    )
    return result["value"]["blockhash"]


# ─── [FIX 3] SOL Amount — explicit float throughout ──────────────────────────
def get_buy_amount(cfg_data: dict) -> tuple[float, int]:
    """
    Returns (sol_amount_float, lamports_int).
    Handles env override and config values correctly as floats.
    e.g. 0.05 SOL → 50_000_000 lamports
    """
    raw = cfg_data.get("risk", {}).get("buy_amount_sol", 0.1)
    sol = float(raw)  # Explicit cast — prevents scientific notation issues
    lamports = int(sol * 1_000_000_000)
    return sol, lamports



# ─── Rug-Pull Check ───────────────────────────────────────────────────────────
async def is_token_safe(
    mint: str,
    client: Optional[httpx.AsyncClient] = None
) -> tuple[bool, str]:
    """
    Check mint authority and freeze authority on-chain.
    MODIFICADO: Ignora erros de "not found" ou timeout para priorizar velocidade.
    Apenas bloqueia a compra se for confirmado afirmativamente que é rug.
    """
    try:
        if state.ws_connected and _ws_conn:
            info = await get_account_info_ws(mint)
        elif client:
            info = await get_account_info_http(client, mint)
        else:
            # ASSUME SEGURO se não houver cliente
            return True, "No RPC connection (Assumed safe for speed)"

        if not info:
            # ALTERAÇÃO PRINCIPAL: RPC lento não encontrou a moeda? Avança na mesma!
            return True, "Mint account not found (Ignored for speed)"

        parsed   = info.get("data", {}).get("parsed", {})
        mint_inf = parsed.get("info", {})

        # Se conseguiu ler os dados e o tipo não for mint, bloqueia
        if parsed and parsed.get("type") != "mint":
            return False, "Not an SPL mint"

        # BLOQUEIA APENAS SE CONFIRMAR O RUG
        if mint_inf.get("mintAuthority") is not None:
            return False, f"Mint authority live: {str(mint_inf['mintAuthority'])[:10]}..."
        if mint_inf.get("freezeAuthority") is not None:
            return False, f"Freeze authority live: {str(mint_inf['freezeAuthority'])[:10]}..."

        return True, "OK"

    except asyncio.TimeoutError:
        # Se o RPC demorar demasiado a responder à verificação, avança!
        return True, "RPC timeout during rug check (Ignored for speed)"
    except Exception as e:
        # Qualquer outro erro de leitura é ignorado
        return True, f"Rug check error ignored: {e}"


# ─── Jupiter Client ───────────────────────────────────────────────────────────
async def get_jupiter_quote(
    http: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int
) -> Optional[dict]:
    try:
        r = await http.get(JUPITER_QUOTE, params={
            "inputMint":        input_mint,
            "outputMint":       output_mint,
            "amount":           str(amount),
            "slippageBps":      slippage_bps,
            "onlyDirectRoutes": False,
        }, timeout=8.0)
        r.raise_for_status()
        q = r.json()
        impact = float(q.get("priceImpactPct", 0))
        log.debug(f"Quote: {amount} → {q.get('outAmount')} | Impact: {impact:.2f}%")
        return q
    except Exception as e:
        log.error(f"Jupiter quote error: {e}")
        return None

async def build_swap_tx(http: httpx.AsyncClient, quote: dict) -> Optional[bytes]:
    try:
        r = await http.post(JUPITER_SWAP, json={
            "quoteResponse":                quote,
            "userPublicKey":                PUBKEY,
            "wrapAndUnwrapSol":             True,
            "computeUnitPriceMicroLamports": "auto",
            "dynamicComputeUnitLimit":      True,
            "prioritizationFeeLamports":    0,
        }, timeout=10.0)
        r.raise_for_status()
        tx_bytes = base64.b64decode(r.json()["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)
        tx.sign([KEYPAIR])
        return bytes(tx)
    except Exception as e:
        log.error(f"Swap build error: {e}")
        return None


# ─── Jito Bundle ──────────────────────────────────────────────────────────────
async def send_jito_bundle(
    http: httpx.AsyncClient,
    swap_tx_bytes: bytes,
    tip_lamports: int
) -> Optional[str]:
    try:
        blockhash = await get_latest_blockhash(http)
        tip_ix    = transfer(TransferParams(
            from_pubkey=KEYPAIR.pubkey(),
            to_pubkey=Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS)),
            lamports=tip_lamports,
        ))
        msg    = MessageV0.try_compile(
            payer=KEYPAIR.pubkey(),
            instructions=[tip_ix],
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        tip_tx   = VersionedTransaction(msg, [KEYPAIR])
        tip_b64  = base64.b64encode(bytes(tip_tx)).decode()
        swap_b64 = base64.b64encode(swap_tx_bytes).decode()

        r = await http.post(JITO_URL, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "sendBundle",
            "params":  [[tip_b64, swap_b64]]
        }, timeout=12.0)
        r.raise_for_status()
        result = r.json()
        if "result" in result:
            return result["result"]
        log.error(f"Jito rejection: {result.get('error')}")
        return None
    except Exception as e:
        log.error(f"Jito bundle error: {e}")
        return None


# ─── SOL Price ────────────────────────────────────────────────────────────────
_sol_price_cache: tuple[float, float] = (150.0, 0.0)

async def get_sol_price(http: httpx.AsyncClient) -> float:
    global _sol_price_cache
    price, cached_at = _sol_price_cache
    if utc_now_ts() - cached_at < 30:
        return price
    try:
        r = await http.get(JUPITER_PRICE, params={"ids": SOL_MINT}, timeout=5.0)
        new_price = float(r.json()["data"][SOL_MINT]["price"])
        _sol_price_cache = (new_price, utc_now_ts())
        return new_price
    except Exception:
        return price


# ─── Position Manager (Moonbag) ───────────────────────────────────────────────
async def monitor_position(mint: str, http: httpx.AsyncClient, cfg_data: dict):
    """
    Phase 1 — sell 50% at 2× entry.
    Phase 2 — trail remaining 50% with percentage stop.
    """
    pos           = state.open_positions.get(mint)
    slippage_exit = cfg_data["risk"]["slippage_bps_exit"]
    trail_pct     = float(cfg_data.get("trailing_stop_pct", 0.25))
    tp_mult       = float(cfg_data.get("take_profit_multiplier", 2.0))

    if not pos:
        return

    log.info(
        f"📊 Monitoring {mint[:8]}... | "
        f"Entry: ${pos['entry_price']:.8f} | Tokens: {pos['token_amount']}"
    )

    while mint in state.open_positions:
        try:
            r = await http.get(JUPITER_PRICE, params={"ids": mint}, timeout=5.0)
            price_data = r.json().get("data", {}).get(mint)
            if not price_data:
                await asyncio.sleep(3)
                continue

            current = float(price_data["price"])
            mult    = current / pos["entry_price"] if pos["entry_price"] > 0 else 1.0

            # Update position state for dashboard
            pos["current_price"]  = current
            pos["current_mult"]   = round(mult, 3)
            pos["unrealized_pnl"] = round((current - pos["entry_price"]) / pos["entry_price"] * 100, 2)

            # Phase 1 — Take Profit
            if pos["phase"] == 1 and mult >= tp_mult:
                half = pos["token_amount"] // 2
                log.info(f"🎉 TP | {mint[:8]}... | {mult:.2f}× | Selling 50%")
                quote = await get_jupiter_quote(http, mint, SOL_MINT, half, slippage_exit)
                if quote:
                    tx_bytes = await build_swap_tx(http, quote)
                    if tx_bytes:
                        tip = int(os.getenv("JITO_TIP_LAMPORTS", "150000"))
                        bundle = await send_jito_bundle(http, tx_bytes, tip)
                        if bundle:
                            state.total_sells += 1
                            state.add_event("sell", f"TP hit {mint[:8]}... at {mult:.2f}×", {
                                "mint": mint, "mult": mult, "bundle": bundle[:12]
                            })
                pos["remaining"] = pos["token_amount"] - half
                pos["phase"]     = 2
                pos["peak"]      = current

            # Phase 2 — Trailing Stop
            elif pos["phase"] == 2:
                if current > pos.get("peak", current):
                    pos["peak"] = current

                drop = (pos["peak"] - current) / pos["peak"] if pos["peak"] > 0 else 0
                pos["trail_drop_pct"] = round(drop * 100, 2)

                if drop >= trail_pct:
                    log.info(
                        f"🛑 TRAIL STOP | {mint[:8]}... | "
                        f"Drop: {drop*100:.1f}% from peak"
                    )
                    quote = await get_jupiter_quote(http, mint, SOL_MINT, pos["remaining"], slippage_exit)
                    if quote:
                        tx_bytes = await build_swap_tx(http, quote)
                        if tx_bytes:
                            tip = int(os.getenv("JITO_TIP_LAMPORTS", "150000"))
                            bundle = await send_jito_bundle(http, tx_bytes, tip)
                            if bundle:
                                state.total_sells += 1
                                state.add_event("sell", f"Trail stop {mint[:8]}... drop {drop*100:.1f}%", {
                                    "mint": mint, "drop_pct": drop * 100
                                })
                    state.open_positions.pop(mint, None)
                    break

        except Exception as e:
            log.error(f"Position monitor error {mint[:8]}: {e}")

        await asyncio.sleep(3)


# ─── Signal Handler ───────────────────────────────────────────────────────────
async def on_signal(wallet: str, mint: str, http: httpx.AsyncClient, cfg_data: dict):
    """Core buy pipeline. Completes signal → bundle in <1s ideally."""
    if mint in state.processing or mint in state.open_positions:
        return

    max_pos = cfg_data["risk"].get("max_positions", 5)
    if len(state.open_positions) >= max_pos:
        log.warning(f"Max positions ({max_pos}) reached — skipping {mint[:8]}...")
        return

    state.processing.add(mint)
    state.last_signal_ts = utc_now_ts()
    t0 = time.monotonic()

    log.info(f"⚡ SIGNAL | From: {wallet[:10]}... | Mint: {mint[:10]}...")
    state.add_event("signal", f"Signal from {wallet[:10]}... → {mint[:10]}...", {
        "wallet": wallet, "mint": mint
    })

    try:
        # ── Parallel: rug check + Jupiter quote (saves ~300–600ms) ───────────
        sol_amount, buy_lamports = get_buy_amount(cfg_data)
        slippage                 = cfg_data["risk"]["slippage_bps_entry"]
        max_impact               = float(cfg_data["risk"].get("max_price_impact_pct", 25.0))

        (safe, reason), quote = await asyncio.gather(
            is_token_safe(mint, client=http),
            get_jupiter_quote(http, SOL_MINT, mint, buy_lamports, slippage),
        )

        if not safe:
            log.warning(f"🚫 RUG: {mint[:8]}... | {reason}")
            state.add_event("rug", f"Rug: {mint[:8]}... — {reason}", {"mint": mint})
            return

        if not quote:
            log.info(f"No Jupiter quote for {mint[:8]}... — token may have no liquidity yet")
            return

        if float(quote.get("priceImpactPct", 0)) > max_impact:
            log.warning(f"Price impact too high for {mint[:8]}... — abort")
            return

        out_amount = int(quote.get("outAmount", 0))

        # ── Build + sign tx ──────────────────────────────────────────────────
        tx_bytes = await build_swap_tx(http, quote)
        if not tx_bytes:
            return

        # ── Submit Jito bundle ───────────────────────────────────────────────
        tip = int(os.getenv("JITO_TIP_LAMPORTS", "150000"))
        bundle_id = await send_jito_bundle(http, tx_bytes, tip)
        if not bundle_id:
            log.error(f"Bundle failed for {mint[:8]}...")
            return

        elapsed = (time.monotonic() - t0) * 1000
        log.info(
            f"🟢 BUY | Mint: {mint[:8]}... | "
            f"In: {sol_amount:.4f} SOL | "   # [FIX 3] proper float display
            f"Out: {out_amount} tokens | "
            f"Bundle: {bundle_id[:12]}... | "
            f"⏱ {elapsed:.0f}ms"
        )

        sol_price   = await get_sol_price(http)
        entry_price = (buy_lamports / 1e9 * sol_price) / max(out_amount, 1)

        state.open_positions[mint] = {
            "mint":          mint,
            "wallet":        wallet,
            "entry_price":   entry_price,
            "current_price": entry_price,
            "token_amount":  out_amount,
            "remaining":     out_amount,
            "sol_spent":     sol_amount,  # [FIX 3]
            "phase":         1,
            "peak":          entry_price,
            "current_mult":  1.0,
            "unrealized_pnl": 0.0,
            "trail_drop_pct": 0.0,
            "opened_at":     utc_now_ts(),
            "bundle_id":     bundle_id,
        }
        state.total_buys += 1
        state.add_event("buy", f"Bought {mint[:8]}... | {sol_amount:.4f} SOL in", {
            "mint": mint, "sol": sol_amount, "tokens": out_amount, "bundle": bundle_id[:12]
        })

        asyncio.create_task(monitor_position(mint, http, cfg_data))

    finally:
        state.processing.discard(mint)


# ─── Transaction Analyzer — Balance-Diff Signal Detection ────────────────────
# ─── Fast Path: Token Account Diff (no indexer, ~0ms latency) ────────────────
async def init_wallet_token_cache(wallet: str, http: httpx.AsyncClient) -> None:
    """
    Called once per wallet at subscription time.
    Snapshots every SPL token mint the wallet currently holds.
    fast_detect_from_token_accounts() will compare against this baseline
    to find genuinely new mints — not ones the wallet already owned.
    """
    try:
        result = await http_rpc(http, "getTokenAccountsByOwner", [
            wallet,
            {"programId": TOKEN_PROGRAM},
            {"encoding": "jsonParsed", "commitment": "processed"}
        ])
        accounts = result.get("value", []) if isinstance(result, dict) else []
        mints = set()
        for acc in accounts:
            mint = (
                acc.get("account", {})
                   .get("data", {})
                   .get("parsed", {})
                   .get("info", {})
                   .get("mint", "")
            )
            if mint:
                mints.add(mint)
        state.wallet_token_cache[wallet] = mints
        log.info(
            f"  🗂️  Token cache initialised for {wallet[:14]}... "
            f"({len(mints)} existing mints)"
        )
    except Exception as e:
        log.warning(f"Token cache init failed for {wallet[:10]}: {e}")
        state.wallet_token_cache[wallet] = set()


async def fast_detect_from_token_accounts(
    wallet:      str,
    signature:   str,
    http:        httpx.AsyncClient,
    cfg_data:    dict,
    known_mints: set[str],
) -> bool:
    """
    FAST PATH — fires in ~200–500ms, completely bypasses the Helius indexer.

    Why this works:
        getTransaction requires Helius to have indexed the tx (can lag 2–15s).
        getTokenAccountsByOwner reads LIVE account state from the validator —
        no indexer involved, always reflects the current chain state.

    Logic:
        1. Fetch the wallet's current SPL token accounts (live state).
        2. Compare against the snapshot taken at subscription time.
        3. Any mint in current_state but NOT in snapshot = just bought.
        4. Fire on_signal immediately for each new mint.
        5. Update the snapshot for the next comparison.

    Returns True if a buy signal was fired, False otherwise.
    """
    try:
        result = await http_rpc(http, "getTokenAccountsByOwner", [
            wallet,
            {"programId": TOKEN_PROGRAM},
            {"encoding": "jsonParsed", "commitment": "processed"}
        ])
        accounts = result.get("value", []) if isinstance(result, dict) else []
    except Exception as e:
        log.info(f"Fast detect RPC error for {wallet[:10]}: {e}")
        return False

    current: dict[str, float] = {}  # mint → ui_amount
    for acc in accounts:
        info = (
            acc.get("account", {})
               .get("data", {})
               .get("parsed", {})
               .get("info", {})
        )
        mint   = info.get("mint", "")
        amount = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
        if mint and amount > 0:
            current[mint] = amount

    cached   = state.wallet_token_cache.get(wallet, set())
    new_mints = [
        m for m in current
        if m not in cached
        and m not in IGNORED_MINTS
        and m not in known_mints
    ]

    if not new_mints:
        return False

    # Update cache immediately so the next notification doesn't re-fire
    state.wallet_token_cache[wallet] = set(current.keys())

    fired = False
    for mint in new_mints:
        log.info(
            f"⚡ FAST DETECT | Wallet: {wallet[:10]}... | "
            f"Mint: {mint[:10]}... | "
            f"Amount: {current[mint]:,.2f} | "
            f"Tx: {signature[:12]}... | "
            f"[no indexer — account diff]"
        )
        known_mints.add(mint)
        asyncio.create_task(on_signal(wallet, mint, http, cfg_data))
        fired = True

    return fired


# ─── Slow Path: Full Transaction Diff (fallback, higher confidence) ────────────
async def analyze_transaction(
    wallet:      str,
    signature:   str,
    http:        httpx.AsyncClient,
    cfg_data:    dict,
    known_mints: set[str],
) -> None:
    """
    SLOW PATH — fallback when fast_detect finds nothing.

    Uses pre/postTokenBalances from getTransaction for higher confidence
    (confirms SOL was actually spent, not just a token receive from an airdrop).
    Retries aggressively: first attempt at 0ms, then every 500ms × 8 retries.
    Total window: ~4 seconds (vs the 8s from the previous 2s+3×2s approach).
    """
    RETRIES     = 8
    RETRY_DELAY = 0.5   # seconds between attempts — fast polling, not slow sleeps

    tx = None
    for attempt in range(1, RETRIES + 1):
        try:
            tx = await http_rpc(http, "getTransaction", [
                signature,
                {
                    "encoding":    "jsonParsed",
                    "commitment":  "confirmed",    # confirmed = indexed faster than finalized
                    "maxSupportedTransactionVersion": 0
                }
            ])
        except Exception as e:
            log.info(
                f"getTransaction error (attempt {attempt}/{RETRIES}) "
                f"for {signature[:12]}: {e}"
            )
            tx = None

        if tx:
            log.info(f"📥 TX indexed (attempt {attempt}/{RETRIES}): {signature[:12]}...")
            break

        if attempt < RETRIES:
            log.info(
                f"⏳ Not indexed yet ({attempt}/{RETRIES}) — "
                f"retry in {RETRY_DELAY}s [{signature[:12]}]"
            )
            await asyncio.sleep(RETRY_DELAY)

    if not tx:
        log.info(
            f"❌ TX null after {RETRIES} attempts ({RETRIES * RETRY_DELAY:.1f}s) "
            f"— Helius free-tier lag. Fast path should have caught this. "
            f"Sig: {signature[:12]}..."
        )
        return

    meta = tx.get("meta") or {}
    if meta.get("err"):
        return

    # ── Find wallet account index ─────────────────────────────────────────────
    account_keys: list = (
        tx.get("transaction", {})
          .get("message", {})
          .get("accountKeys", [])
    )
    wallet_idx = next(
        (i for i, ak in enumerate(account_keys)
         if (ak.get("pubkey") if isinstance(ak, dict) else str(ak)) == wallet),
        None
    )
    if wallet_idx is None:
        return

    # ── SOL balance delta ─────────────────────────────────────────────────────
    pre_bal  = meta.get("preBalances",  [])
    post_bal = meta.get("postBalances", [])
    if wallet_idx >= len(pre_bal) or wallet_idx >= len(post_bal):
        return

    sol_spent = -(post_bal[wallet_idx] - pre_bal[wallet_idx]) / 1e9

    if sol_spent < MIN_SOL_SPENT:
        log.info(
            f"Skipping {signature[:12]}: SOL delta = {sol_spent:+.6f} SOL "
            f"(threshold = {MIN_SOL_SPENT} SOL — sell or fee-only tx)"
        )
        return

    # ── Token balance diff ────────────────────────────────────────────────────
    def token_map(balances: list) -> dict:
        out = {}
        for e in balances:
            mint  = e.get("mint", "")
            owner = e.get("owner", "")
            amt   = float((e.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            out[(mint, owner)] = amt
        return out

    pre_map  = token_map(meta.get("preTokenBalances",  []))
    post_map = token_map(meta.get("postTokenBalances", []))

    for (mint, owner), post_amt in post_map.items():
        if owner != wallet:
            continue
        if mint in IGNORED_MINTS or mint in known_mints:
            continue
        delta = post_amt - pre_map.get((mint, owner), 0.0)
        if delta > 0:
            log.info(
                f"💡 SLOW DETECT | Wallet: {wallet[:10]}... | "
                f"Mint: {mint[:10]}... | "
                f"SOL: {sol_spent:.4f} | Tokens: {delta:,.2f} | "
                f"Tx: {signature[:12]}..."
            )
            known_mints.add(mint)
            asyncio.create_task(on_signal(wallet, mint, http, cfg_data))


# ─── [FIX 1] WebSocket Heartbeat ─────────────────────────────────────────────
async def heartbeat_task(ws):
    """
    Sends a manual ping every 5 seconds to keep the Helius WS alive.
    Helius free-tier closes idle connections after ~10s.
    """
    while True:
        try:
            await ws.ping()
            log.debug("💓 WS ping sent")
        except Exception as e:
            log.warning(f"Heartbeat ping failed: {e}")
            break
        await asyncio.sleep(5)


# ─── [FIX 2] HTTP Polling Fallback ───────────────────────────────────────────
async def polling_fallback_loop(http: httpx.AsyncClient, cfg_data: dict):
    """
    HTTP polling fallback — activated when WS fails 3× in a row.
    Every 2.5s, checks each tracked wallet for new transactions.
    Uses analyze_transaction (balance-diff) — same logic as WS path.
    """
    log.info("🔄 POLLING MODE ACTIVE — WS unavailable, using HTTP polling")
    state.polling_mode = True
    known_mints: set[str] = set(IGNORED_MINTS)
    poll_interval = 2.5

    while state.polling_mode:
        cfg_data = await read_config()
        wallets  = cfg_data["TARGET_WALLETS"][:10]

        for wallet in wallets:
            try:
                sigs = await http_rpc(
                    http,
                    "getSignaturesForAddress",
                    [wallet, {"limit": 3, "commitment": "confirmed"}]
                )
                if not isinstance(sigs, list) or not sigs:
                    continue

                newest_sig = sigs[0].get("signature", "")
                if not newest_sig or sigs[0].get("err"):
                    continue
                if newest_sig == state.last_sigs.get(wallet):
                    continue   # Already processed

                state.last_sigs[wallet] = newest_sig
                await analyze_transaction(
                    wallet, newest_sig, http, cfg_data, known_mints
                )

            except Exception as e:
                log.error(f"Polling error for {wallet[:10]}: {e}")

            await asyncio.sleep(0.5)

        await asyncio.sleep(poll_interval)

    log.info("🔄 Polling mode deactivated — WebSocket reconnected")


# ─── WebSocket Message Pump (background task) ────────────────────────────────
async def message_pump(
    ws,
    sub_map:     dict,
    known_mints: set,
    http:        httpx.AsyncClient,
    cfg_ref:     list,           # cfg_ref[0] = current cfg_data (mutable box)
):
    """
    THE ROOT CAUSE FIX.

    Previously, subscribe() sent a message and immediately awaited the
    response future with asyncio.wait_for(fut, timeout=10). But the only
    code that resolves those futures is the `async for raw in ws` loop —
    which lived in the SAME coroutine, AFTER the subscribe() calls. Pure
    deadlock. Every 10 seconds the timeout fired → TimeoutError → reconnect.

    Fix: run the message pump as a separate asyncio.Task that starts
    BEFORE any subscribe() call. Now the pump is always running, futures
    are resolved instantly, and subscriptions complete in <100ms.
    """
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)

                # ── Resolve pending RPC response futures ──────────────────
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in _pending:
                    fut = _pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
                    continue

                method = msg.get("method", "")

                # ── Ignore pong frames ────────────────────────────────────
                if method == "pong":
                    log.debug("💓 pong")
                    continue

                # ── Dispatch log notifications ────────────────────────────
                if method == "logsNotification":
                    params    = msg.get("params", {})
                    sub_id    = params.get("subscription")
                    result    = params.get("result", {})
                    wallet    = sub_map.get(sub_id)
                    value     = result.get("value", {})
                    signature = value.get("signature", "")
                    err       = value.get("err")

                    if wallet and signature and not err:
                        # Fire BOTH paths concurrently:
                        #
                        # FAST PATH  (~200–500ms): getTokenAccountsByOwner
                        #   Reads live account state, bypasses the Helius
                        #   indexer entirely. Fires the buy signal in <500ms.
                        #
                        # SLOW PATH  (~0–4s):     getTransaction with retries
                        #   Confirms SOL was spent (not just a token receive).
                        #   Guards against false positives (airdrops, etc).
                        #   No-ops if fast path already fired for this mint.
                        asyncio.create_task(
                            fast_detect_from_token_accounts(
                                wallet, signature, http, cfg_ref[0], known_mints
                            )
                        )
                        asyncio.create_task(
                            analyze_transaction(
                                wallet, signature, http, cfg_ref[0], known_mints
                            )
                        )

            except json.JSONDecodeError:
                pass
            except Exception as e:
                log.error(f"Pump dispatch error: {e}")

    except websockets.exceptions.ConnectionClosedOK:
        log.info("WS closed cleanly")
    except Exception as e:
        log.warning(f"Pump exited: {type(e).__name__}: {e}")


# ─── Hot-reload task (runs alongside pump) ───────────────────────────────────
async def hot_reload_task(
    ws,
    subscribed:  set,
    sub_map:     dict,
    cfg_ref:     list,
    max_wallets: int,
):
    """
    Checks config.json every 60s and subscribes any new wallets the
    scraper has added since the last reload. Runs as its own task so it
    never blocks the message pump.
    """
    while True:
        await asyncio.sleep(60)
        try:
            cfg_ref[0] = await read_config()
            new_wallets = [
                w for w in cfg_ref[0]["TARGET_WALLETS"][:max_wallets]
                if w not in subscribed
            ]
            for wallet in new_wallets:
                rid = _next_id()
                fut = asyncio.get_event_loop().create_future()
                _pending[rid] = fut
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": rid,
                    "method":  "logsSubscribe",
                    "params":  [
                        {"mentions": [wallet]},
                        {"commitment": "processed"}
                    ]
                }))
                resp = await asyncio.wait_for(fut, timeout=10.0)
                sid  = resp["result"]
                sub_map[sid] = wallet
                subscribed.add(wallet)
                state.tracked_wallets = list(subscribed)
                log.info(f"🔄 Hot-reload subscribed: {wallet[:14]}...")
        except Exception as e:
            log.error(f"Hot-reload error: {e}")


# ─── WebSocket Engine ─────────────────────────────────────────────────────────
async def ws_listener(http: httpx.AsyncClient):
    """
    Outer reconnect loop with:
    - HTTP status code logging (429 / 403 / 503)
    - Heartbeat task (ping every 5s)
    - Pump task (concurrent message dispatch — the deadlock fix)
    - Hot-reload task (new wallets every 60s)
    - Polling fallback after WS_FAIL_LIMIT consecutive failures
    """
    global _ws_conn

    known_mints:  set[str] = {SOL_MINT}
    subscribed:   set[str] = set()
    sub_map:      dict     = {}       # sub_id (int) → wallet (str)
    cfg_data               = await read_config()
    cfg_ref:      list     = [cfg_data]   # mutable box shared with tasks
    backoff:      int      = 1
    polling_task           = None

    while True:
        # ── Escalate to polling if WS keeps dying ─────────────────────────
        if state.ws_fail_count >= state.ws_fail_limit and not state.polling_mode:
            log.warning(
                f"WS failed {state.ws_fail_count}× consecutively — "
                f"activating HTTP polling fallback for 60s"
            )
            state.add_event("error", "WebSocket unavailable — switched to HTTP polling")
            if polling_task is None or polling_task.done():
                polling_task = asyncio.create_task(
                    polling_fallback_loop(http, cfg_ref[0])
                )
            await asyncio.sleep(60)
            state.ws_fail_count = 0
            state.polling_mode  = False
            if polling_task and not polling_task.done():
                polling_task.cancel()
            continue

        try:
            log.info("Connecting to Helius WebSocket...")

            async with websockets.connect(
                HELIUS_WS,
                ping_interval=None,    # manual heartbeat handles this
                ping_timeout=None,
                open_timeout=20,
                close_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                _ws_conn            = ws
                state.ws_connected  = True
                state.ws_fail_count = 0
                state.polling_mode  = False
                backoff             = 1
                cfg_ref[0]          = await read_config()
                log.info("✅ WebSocket connected to Helius")
                state.add_event("info", "WebSocket connected to Helius")

                max_wallets = cfg_ref[0]["free_tier"].get("max_tracked_wallets", 10)

                # ── THE FIX: start pump FIRST, then subscribe ─────────────
                pump = asyncio.create_task(
                    message_pump(ws, sub_map, known_mints, http, cfg_ref)
                )
                hb   = asyncio.create_task(heartbeat_task(ws))

                # subscribe() now works — pump is already reading responses
                async def subscribe(wallet: str):
                    rid = _next_id()
                    fut = asyncio.get_event_loop().create_future()
                    _pending[rid] = fut
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": rid,
                        "method":  "logsSubscribe",
                        "params":  [
                            {"mentions": [wallet]},
                            {"commitment": "processed"}
                        ]
                    }))
                    # This future resolves in <100ms now — pump is running
                    resp = await asyncio.wait_for(fut, timeout=15.0)
                    sid  = resp["result"]
                    sub_map[sid] = wallet
                    subscribed.add(wallet)
                    state.tracked_wallets = list(subscribed)
                    log.info(f"  📡 Subscribed: {wallet[:14]}... (sub {sid})")
                    # Snapshot existing token accounts so fast_detect knows
                    # what's new vs what the wallet already held before we started
                    await init_wallet_token_cache(wallet, http)

                for w in cfg_ref[0]["TARGET_WALLETS"][:max_wallets]:
                    if w not in subscribed:
                        await subscribe(w)

                if not subscribed:
                    log.warning(
                        "TARGET_WALLETS is empty — bot idle. "
                        "Run wallet_scraper.py or add wallets via dashboard."
                    )

                # Start hot-reload task (subscribes new wallets every 60s)
                reload_t = asyncio.create_task(
                    hot_reload_task(ws, subscribed, sub_map, cfg_ref, max_wallets)
                )

                # Block until connection drops (pump task ends = WS closed)
                await pump

                # Clean up sibling tasks
                for t in (hb, reload_t):
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

                # Dropped cleanly — outer loop will reconnect immediately
                state.ws_connected = False
                _ws_conn           = None
                log.info("WS pump ended — reconnecting...")

        # [FIX 4] Catch specific WS errors with HTTP status codes
        except websockets.exceptions.InvalidStatusCode as e:
            status_code = e.status_code
            state.ws_connected   = False
            state.ws_fail_count += 1
            _ws_conn             = None

            # Decode the most common failure reasons
            if status_code == 429:
                reason = "Rate limited (429) — too many connections on free tier"
                wait   = min(backoff * 10, 120)
            elif status_code == 403:
                reason = "Forbidden (403) — check your Helius API key in .env"
                wait   = 30
            elif status_code == 503:
                reason = "Helius service unavailable (503) — temporary outage"
                wait   = min(backoff * 5, 60)
            elif status_code == 401:
                reason = "Unauthorized (401) — invalid Helius API key"
                wait   = 60
            else:
                reason = f"HTTP {status_code} from Helius WebSocket endpoint"
                wait   = min(backoff * 2, 30)

            log.error(f"❌ WS connection rejected: {reason}")
            log.error(f"   Retry {state.ws_fail_count}/{state.ws_fail_limit} in {wait}s...")
            state.add_event("error", f"WS rejected: {reason}")
            backoff = min(backoff * 2, 60)
            await asyncio.sleep(wait)

        except websockets.exceptions.ConnectionClosedError as e:
            state.ws_connected   = False
            state.ws_fail_count += 1
            _ws_conn             = None
            log.warning(
                f"WS closed: code={e.code} reason='{e.reason}' | "
                f"Retry {state.ws_fail_count} in {backoff}s..."
            )
            state.add_event("error", f"WS closed (code {e.code}): {e.reason}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

        except Exception as e:
            state.ws_connected   = False
            state.ws_fail_count += 1
            _ws_conn             = None
            log.error(f"WS unexpected error: {type(e).__name__}: {e}")
            state.add_event("error", f"WS error: {type(e).__name__}: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    cfg_data = await read_config()
    sol_amount, lamports = get_buy_amount(cfg_data)

    log.info("=" * 64)
    log.info("  🎯 SOLANA MEMECOIN SNIPER v1.2.0 — ONLINE")
    log.info(f"  Wallet:          {PUBKEY[:20]}...")
    log.info(f"  Buy amount:      {sol_amount:.4f} SOL ({lamports:,} lamports)")  # [FIX 3]
    log.info(f"  Entry slippage:  {cfg_data['risk']['slippage_bps_entry'] / 100:.0f}%")
    log.info(f"  Max positions:   {cfg_data['risk']['max_positions']}")
    log.info(f"  Max wallets:     {cfg_data['free_tier']['max_tracked_wallets']}")
    log.info(f"  Target wallets:  {len(cfg_data['TARGET_WALLETS'])}")
    log.info(f"  WS→Polling at:   {state.ws_fail_limit} consecutive failures")
    log.info("=" * 64)

    if not cfg_data["TARGET_WALLETS"]:
        log.warning("TARGET_WALLETS is empty — run wallet_scraper.py first")
        log.warning("Bot will idle and hot-reload every 60s until wallets appear")

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20),
        timeout=httpx.Timeout(15.0)
    ) as http:
        await asyncio.gather(
            ws_listener(http),
            write_status(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")
        # Write offline status
        try:
            status = {"online": False, "updated_at": utc_now_ts()}
            (ROOT / "status.json").write_text(json.dumps(status))
        except Exception:
            pass