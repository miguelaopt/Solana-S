# sniper_bot.py — Solana Memecoin Sniper (v1.2.1)
# Otimizações de Rede: DNS Retries, 429 Backoff & Wallet Spam Cooldown

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

# Mints that are never buy signals
IGNORED_MINTS: set[str] = {
    SOL_MINT,
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",  # ETH (Wormhole)
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",  # BONK
}

# Minimum SOL spent to count as a real buy (filters dust / fee-only txs)
MIN_SOL_SPENT = 0.00005

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
        self.ws_fail_limit:   int        = 3
        self.start_ts:        float      = utc_now_ts()
        self.last_signal_ts:  float      = 0.0
        self.total_buys:      int        = 0
        self.total_sells:     int        = 0
        self.tracked_wallets: list[str]  = []
        self.open_positions:  dict       = {}
        self.processing:      set[str]   = set()
        self.recent_events:   list[dict] = []
        self.last_sigs:       dict[str, str] = {}
        self.wallet_token_cache: dict[str, set[str]] = {}
        
        # OTIMIZAÇÃO DE REDE
        self.rate_limit_until: float = 0.0
        self.ignore_wallet_until: dict[str, float] = {}

    def add_event(self, kind: str, message: str, data: dict = None):
        entry = {
            "ts":      utc_now_ts(),
            "kind":    kind,
            "message": message,
            "data":    data or {}
        }
        self.recent_events.insert(0, entry)
        self.recent_events = self.recent_events[:50]

    def set_global_rate_limit(self, seconds: float = 2.0):
        self.rate_limit_until = utc_now_ts() + seconds

    def is_rate_limited(self) -> bool:
        return utc_now_ts() < self.rate_limit_until

    def ignore_wallet(self, wallet: str, seconds: float = 3.0):
        self.ignore_wallet_until[wallet] = utc_now_ts() + seconds

    def is_wallet_ignored(self, wallet: str) -> bool:
        return utc_now_ts() < self.ignore_wallet_until.get(wallet, 0.0)

state = BotState()

# ─── status.json Writer ───────────────────────────────────────────────────────
async def write_status():
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
            pass
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
    if _ws_conn is None:
        raise RuntimeError("WebSocket not connected")
    rid = _next_id()
    fut = asyncio.get_event_loop().create_future()
    _pending[rid] = fut
    await _ws_conn.send(json.dumps({
        "jsonrpc": "2.0", "id": rid, "method": method, "params": params
    }))
    return await asyncio.wait_for(fut, timeout=15.0)

async def http_rpc(client: httpx.AsyncClient, method: str, params: list) -> dict:
    if state.is_rate_limited():
        raise Exception("Global Rate Limit Active - Skipping request to save RPC")

    r = await client.post(HELIUS_HTTP, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params
    }, timeout=12.0)

    if r.status_code == 429:
        state.set_global_rate_limit(2.0)
        log.warning(f"RPC 429 Too Many Requests -> Backing off globally for 2s")
        r.raise_for_status()

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


def get_buy_amount(cfg_data: dict) -> tuple[float, int]:
    raw = cfg_data.get("risk", {}).get("buy_amount_sol", 0.1)
    sol = float(raw)
    lamports = int(sol * 1_000_000_000)
    return sol, lamports


# ─── Rug-Pull Check ───────────────────────────────────────────────────────────
async def is_token_safe(
    mint: str,
    client: Optional[httpx.AsyncClient] = None
) -> tuple[bool, str]:
    try:
        if state.ws_connected and _ws_conn:
            info = await get_account_info_ws(mint)
        elif client:
            info = await get_account_info_http(client, mint)
        else:
            return True, "No RPC connection (Assumed safe for speed)"

        if not info:
            return True, "Mint account not found (Ignored for speed)"

        parsed   = info.get("data", {}).get("parsed", {})
        mint_inf = parsed.get("info", {})

        if parsed and parsed.get("type") != "mint":
            return False, "Not an SPL mint"

        if mint_inf.get("mintAuthority") is not None:
            return False, f"Mint authority live: {str(mint_inf['mintAuthority'])[:10]}..."
        if mint_inf.get("freezeAuthority") is not None:
            return False, f"Freeze authority live: {str(mint_inf['freezeAuthority'])[:10]}..."

        return True, "OK"

    except asyncio.TimeoutError:
        return True, "RPC timeout during rug check (Ignored for speed)"
    except Exception as e:
        return True, f"Rug check error ignored: {e}"


# ─── Jupiter Client (WITH DNS FIX / RETRIES) ──────────────────────────────────
async def get_jupiter_quote(
    http: httpx.AsyncClient,
    input_mint: str,
    output_mint: str,
    amount: int,
    slippage_bps: int
) -> Optional[dict]:
    for attempt in range(3):
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
            if attempt < 2:
                log.warning(f"Jupiter quote error (attempt {attempt+1}/3): {e} — retrying in 0.2s")
                await asyncio.sleep(0.2)
            else:
                log.error(f"Jupiter quote error final: {e}")
                return None

async def build_swap_tx(http: httpx.AsyncClient, quote: dict) -> Optional[bytes]:
    for attempt in range(3):
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
            if attempt < 2:
                log.warning(f"Swap build error (attempt {attempt+1}/3): {e} — retrying in 0.2s")
                await asyncio.sleep(0.2)
            else:
                log.error(f"Swap build error final: {e}")
                return None


# ─── Jito Bundle ──────────────────────────────────────────────────────────────
async def send_jito_bundle(
    http: httpx.AsyncClient,
    swap_tx_bytes: bytes,
    tip_lamports: int
) -> Optional[str]:
    for attempt in range(3):
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
            if attempt < 2:
                await asyncio.sleep(0.2)
            else:
                log.error(f"Jito bundle error final: {e}")
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
    pos           = state.open_positions.get(mint)
    slippage_exit = cfg_data["risk"]["slippage_bps_exit"]
    trail_pct     = float(cfg_data.get("trailing_stop_pct", 0.25))
    tp_mult       = float(cfg_data.get("take_profit_multiplier", 2.0))

    if not pos:
        return

    log.info(f"📊 Monitoring {mint[:8]}... | Entry: ${pos['entry_price']:.8f}")

    while mint in state.open_positions:
        try:
            r = await http.get(JUPITER_PRICE, params={"ids": mint}, timeout=5.0)
            price_data = r.json().get("data", {}).get(mint)
            if not price_data:
                await asyncio.sleep(3)
                continue

            current = float(price_data["price"])
            mult    = current / pos["entry_price"] if pos["entry_price"] > 0 else 1.0

            pos["current_price"]  = current
            pos["current_mult"]   = round(mult, 3)
            pos["unrealized_pnl"] = round((current - pos["entry_price"]) / pos["entry_price"] * 100, 2)

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
                            state.add_event("sell", f"TP hit {mint[:8]}... at {mult:.2f}×", {})
                pos["remaining"] = pos["token_amount"] - half
                pos["phase"]     = 2
                pos["peak"]      = current

            elif pos["phase"] == 2:
                if current > pos.get("peak", current):
                    pos["peak"] = current

                drop = (pos["peak"] - current) / pos["peak"] if pos["peak"] > 0 else 0
                pos["trail_drop_pct"] = round(drop * 100, 2)

                if drop >= trail_pct:
                    log.info(f"🛑 TRAIL STOP | {mint[:8]}... | Drop: {drop*100:.1f}%")
                    quote = await get_jupiter_quote(http, mint, SOL_MINT, pos["remaining"], slippage_exit)
                    if quote:
                        tx_bytes = await build_swap_tx(http, quote)
                        if tx_bytes:
                            tip = int(os.getenv("JITO_TIP_LAMPORTS", "150000"))
                            bundle = await send_jito_bundle(http, tx_bytes, tip)
                            if bundle:
                                state.total_sells += 1
                                state.add_event("sell", f"Trail stop {mint[:8]}...", {})
                    state.open_positions.pop(mint, None)
                    break

        except Exception as e:
            pass

        await asyncio.sleep(3)


# ─── Signal Handler ───────────────────────────────────────────────────────────
async def on_signal(wallet: str, mint: str, http: httpx.AsyncClient, cfg_data: dict):
    if mint in state.processing or mint in state.open_positions:
        return

    max_pos = cfg_data["risk"].get("max_positions", 5)
    if len(state.open_positions) >= max_pos:
        return

    state.processing.add(mint)
    state.last_signal_ts = utc_now_ts()
    t0 = time.monotonic()

    log.info(f"⚡ SIGNAL | From: {wallet[:10]}... | Mint: {mint[:10]}...")

    try:
        sol_amount, buy_lamports = get_buy_amount(cfg_data)
        slippage                 = cfg_data["risk"]["slippage_bps_entry"]
        max_impact               = float(cfg_data["risk"].get("max_price_impact_pct", 25.0))

        (safe, reason), quote = await asyncio.gather(
            is_token_safe(mint, client=http),
            get_jupiter_quote(http, SOL_MINT, mint, buy_lamports, slippage),
        )

        if not safe:
            log.warning(f"🚫 RUG: {mint[:8]}... | {reason}")
            return

        if not quote:
            log.info(f"No Jupiter quote for {mint[:8]}... — token may have no liquidity yet")
            return

        if float(quote.get("priceImpactPct", 0)) > max_impact:
            return

        out_amount = int(quote.get("outAmount", 0))

        tx_bytes = await build_swap_tx(http, quote)
        if not tx_bytes:
            return

        tip = int(os.getenv("JITO_TIP_LAMPORTS", "150000"))
        bundle_id = await send_jito_bundle(http, tx_bytes, tip)
        if not bundle_id:
            log.error(f"Bundle failed for {mint[:8]}...")
            return

        elapsed = (time.monotonic() - t0) * 1000
        log.info(
            f"🟢 BUY | Mint: {mint[:8]}... | "
            f"In: {sol_amount:.4f} SOL | Out: {out_amount} tokens | "
            f"Bundle: {bundle_id[:12]}... | ⏱ {elapsed:.0f}ms"
        )

        sol_price   = await get_sol_price(http)
        entry_price = (buy_lamports / 1e9 * sol_price) / max(out_amount, 1)

        state.open_positions[mint] = {
            "mint":          mint,
            "entry_price":   entry_price,
            "current_price": entry_price,
            "token_amount":  out_amount,
            "remaining":     out_amount,
            "sol_spent":     sol_amount,
            "phase":         1,
            "peak":          entry_price,
            "current_mult":  1.0,
        }
        state.total_buys += 1
        asyncio.create_task(monitor_position(mint, http, cfg_data))

    finally:
        state.processing.discard(mint)


# ─── Transaction Analyzer ─────────────────────────────────────────────────────
# ─── Transaction Analyzer ─────────────────────────────────────────────────────
async def init_wallet_token_cache(wallet: str, http: httpx.AsyncClient) -> None:
    try:
        result = await http_rpc(http, "getTokenAccountsByOwner", [
            wallet,
            {"programId": TOKEN_PROGRAM},
            {"encoding": "jsonParsed", "commitment": "processed"}
        ])
        accounts = result.get("value", []) if isinstance(result, dict) else []
        mints = set()
        for acc in accounts:
            mint = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("mint", "")
            if mint: mints.add(mint)
        state.wallet_token_cache[wallet] = mints
        log.info(f"  🗂️  Token cache initialised for {wallet[:14]}... ({len(mints)} mints)")
    except Exception:
        state.wallet_token_cache[wallet] = set()


async def fast_detect_from_token_accounts(
    wallet: str, signature: str, http: httpx.AsyncClient, cfg_data: dict, known_mints: set[str]
) -> bool:
    if state.is_rate_limited() or state.is_wallet_ignored(wallet):
        return False

    try:
        result = await http_rpc(http, "getTokenAccountsByOwner", [
            wallet, {"programId": TOKEN_PROGRAM}, {"encoding": "jsonParsed", "commitment": "processed"}
        ])
        accounts = result.get("value", []) if isinstance(result, dict) else []
    except Exception as e:
        return False

    current = {}
    for acc in accounts:
        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        mint   = info.get("mint", "")
        amount = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
        if mint and amount > 0:
            current[mint] = amount

    cached   = state.wallet_token_cache.get(wallet, set())
    new_mints = [m for m in current if m not in cached and m not in IGNORED_MINTS and m not in known_mints]

    if not new_mints:
        return False

    state.wallet_token_cache[wallet] = set(current.keys())

    fired = False
    for mint in new_mints:
        log.info(f"⚡ FAST DETECT | Wallet: {wallet[:10]}... | Mint: {mint[:10]}...")
        known_mints.add(mint)
        asyncio.create_task(on_signal(wallet, mint, http, cfg_data))
        fired = True

    return fired


async def analyze_transaction(
    wallet: str, signature: str, http: httpx.AsyncClient, cfg_data: dict, known_mints: set[str]
) -> None:
    if state.is_wallet_ignored(wallet):
        return

    RETRIES = 8
    RETRY_DELAY = 0.5
    tx = None

    for attempt in range(1, RETRIES + 1):
        if state.is_rate_limited():
            log.info(f"⏳ Global Rate Limit Active — pausing TX scan for {signature[:8]}")
            await asyncio.sleep(RETRY_DELAY)
            continue

        try:
            tx = await http_rpc(http, "getTransaction", [
                signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}
            ])
        except Exception:
            tx = None

        if tx:
            log.info(f"📥 TX indexed: {signature[:12]}...")
            break

        if attempt < RETRIES:
            await asyncio.sleep(RETRY_DELAY)

    if not tx:
        return

    meta = tx.get("meta") or {}
    if meta.get("err"): return

    account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
    wallet_idx = next((i for i, ak in enumerate(account_keys) if (ak.get("pubkey") if isinstance(ak, dict) else str(ak)) == wallet), None)
    if wallet_idx is None: return

    pre_bal  = meta.get("preBalances",  [])
    post_bal = meta.get("postBalances", [])
    if wallet_idx >= len(pre_bal) or wallet_idx >= len(post_bal): return

    # 1. Calcular SOL nativo gasto
    sol_spent = -(post_bal[wallet_idx] - pre_bal[wallet_idx]) / 1e9

    # 2. Construir mapas de tokens ANTES da verificação de threshold
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

    # 3. Calcular WSOL gasto
    wsol_pre = pre_map.get((WSOL_MINT, wallet), 0.0)
    wsol_post = post_map.get((WSOL_MINT, wallet), 0.0)
    wsol_spent = wsol_pre - wsol_post

    # 4. Calcular o Gasto Total (Nativo + Wrapped)
    total_sol_spent = sol_spent + wsol_spent

    # 5. Verificação do Threshold usando o valor total
    if total_sol_spent < MIN_SOL_SPENT:
        log.info(f"🗑️ Skipping {signature[:8]}: Dust/Fee/WSOL ({total_sol_spent:+.6f} SOL total). Cooldown wallet {wallet[:8]} for 3s.")
        state.ignore_wallet(wallet, 3.0)
        return

    # 6. Procurar a moeda comprada
    for (mint, owner), post_amt in post_map.items():
        if owner != wallet: continue
        if mint in IGNORED_MINTS or mint in known_mints: continue
        delta = post_amt - pre_map.get((mint, owner), 0.0)
        if delta > 0:
            log.info(f"💡 SLOW DETECT | Wallet: {wallet[:10]}... | Mint: {mint[:10]}...")
            known_mints.add(mint)
            asyncio.create_task(on_signal(wallet, mint, http, cfg_data))


# ─── WebSocket Engine / Fallbacks / Main ──────────────────────────────────────
async def heartbeat_task(ws):
    while True:
        try:
            await ws.ping()
        except Exception:
            break
        await asyncio.sleep(5)

async def polling_fallback_loop(http: httpx.AsyncClient, cfg_data: dict):
    state.polling_mode = True
    known_mints: set[str] = set(IGNORED_MINTS)
    while state.polling_mode:
        cfg_data = await read_config()
        for wallet in cfg_data["TARGET_WALLETS"][:10]:
            try:
                sigs = await http_rpc(http, "getSignaturesForAddress", [wallet, {"limit": 3, "commitment": "confirmed"}])
                if not sigs or not isinstance(sigs, list) or sigs[0].get("err"): continue
                newest_sig = sigs[0].get("signature", "")
                if newest_sig == state.last_sigs.get(wallet): continue
                state.last_sigs[wallet] = newest_sig
                await analyze_transaction(wallet, newest_sig, http, cfg_data, known_mints)
            except Exception:
                pass
            await asyncio.sleep(0.5)
        await asyncio.sleep(2.5)

async def message_pump(ws, sub_map: dict, known_mints: set, http: httpx.AsyncClient, cfg_ref: list):
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in _pending:
                    fut = _pending.pop(msg_id)
                    if not fut.done(): fut.set_result(msg)
                    continue

                method = msg.get("method", "")
                if method == "pong": continue
                if method == "logsNotification":
                    params    = msg.get("params", {})
                    sub_id    = params.get("subscription")
                    wallet    = sub_map.get(sub_id)
                    signature = params.get("result", {}).get("value", {}).get("signature", "")
                    err       = params.get("result", {}).get("value", {}).get("err")

                    if wallet and signature and not err:
                        if state.is_wallet_ignored(wallet):
                            continue
                        asyncio.create_task(fast_detect_from_token_accounts(wallet, signature, http, cfg_ref[0], known_mints))
                        asyncio.create_task(analyze_transaction(wallet, signature, http, cfg_ref[0], known_mints))
            except Exception:
                pass
    except websockets.exceptions.ConnectionClosedOK:
        pass
    except Exception:
        pass

async def hot_reload_task(ws, subscribed: set, sub_map: dict, cfg_ref: list, max_wallets: int):
    while True:
        await asyncio.sleep(60)
        try:
            cfg_ref[0] = await read_config()
            new_wallets = [w for w in cfg_ref[0]["TARGET_WALLETS"][:max_wallets] if w not in subscribed]
            for wallet in new_wallets:
                rid = _next_id()
                fut = asyncio.get_event_loop().create_future()
                _pending[rid] = fut
                await ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": rid, "method":  "logsSubscribe",
                    "params":  [{"mentions": [wallet]}, {"commitment": "processed"}]
                }))
                resp = await asyncio.wait_for(fut, timeout=10.0)
                sub_map[resp["result"]] = wallet
                subscribed.add(wallet)
                state.tracked_wallets = list(subscribed)
        except Exception:
            pass

async def ws_listener(http: httpx.AsyncClient):
    global _ws_conn
    known_mints: set[str] = {SOL_MINT}
    subscribed: set[str]  = set()
    sub_map: dict         = {}
    cfg_ref: list         = [await read_config()]
    backoff: int          = 1
    polling_task          = None

    while True:
        if state.ws_fail_count >= state.ws_fail_limit and not state.polling_mode:
            if polling_task is None or polling_task.done():
                polling_task = asyncio.create_task(polling_fallback_loop(http, cfg_ref[0]))
            await asyncio.sleep(60)
            state.ws_fail_count = 0
            state.polling_mode  = False
            if polling_task and not polling_task.done(): polling_task.cancel()
            continue

        try:
            async with websockets.connect(
                HELIUS_WS, ping_interval=None, ping_timeout=None, open_timeout=20, max_size=10*1024*1024
            ) as ws:
                _ws_conn            = ws
                state.ws_connected  = True
                state.ws_fail_count = 0
                state.polling_mode  = False
                backoff             = 1
                cfg_ref[0]          = await read_config()
                log.info("✅ WebSocket connected to Helius")

                max_wallets = cfg_ref[0]["free_tier"].get("max_tracked_wallets", 10)
                pump = asyncio.create_task(message_pump(ws, sub_map, known_mints, http, cfg_ref))
                hb   = asyncio.create_task(heartbeat_task(ws))

                async def subscribe(wallet: str):
                    rid = _next_id()
                    fut = asyncio.get_event_loop().create_future()
                    _pending[rid] = fut
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": rid, "method": "logsSubscribe",
                        "params": [{"mentions": [wallet]}, {"commitment": "processed"}]
                    }))
                    resp = await asyncio.wait_for(fut, timeout=15.0)
                    sub_map[resp["result"]] = wallet
                    subscribed.add(wallet)
                    state.tracked_wallets = list(subscribed)
                    await init_wallet_token_cache(wallet, http)

                for w in cfg_ref[0]["TARGET_WALLETS"][:max_wallets]:
                    if w not in subscribed:
                        await subscribe(w)

                reload_t = asyncio.create_task(hot_reload_task(ws, subscribed, sub_map, cfg_ref, max_wallets))
                await pump

                for t in (hb, reload_t): t.cancel()
                state.ws_connected = False
                _ws_conn           = None

        except websockets.exceptions.InvalidStatusCode as e:
            state.ws_connected   = False
            state.ws_fail_count += 1
            _ws_conn             = None
            wait = min(backoff * 10, 120) if e.status_code == 429 else min(backoff * 2, 30)
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, 60)
        except Exception:
            state.ws_connected   = False
            state.ws_fail_count += 1
            _ws_conn             = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

async def main():
    cfg_data = await read_config()
    log.info("  🎯 SOLANA MEMECOIN SNIPER v1.2.1 — ONLINE")
    
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=20), timeout=httpx.Timeout(15.0)) as http:
        await asyncio.gather(ws_listener(http), write_status())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass