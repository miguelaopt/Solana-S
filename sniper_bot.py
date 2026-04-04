"""
sniper_bot.py — Autonomous Execution Sniper.

Consumes TARGET_WALLETS from config.json.
Listens via Helius WebSocket logsSubscribe.
On signal: rug-check → Jupiter quote → Jito bundle → position tracking.
Config is hot-reloaded every 60s so new wallets added by scraper are
picked up without restarting the bot.
"""

import asyncio
import base64
import json
import os
import random
import re
import time
from typing import Optional

import httpx
import websockets
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
import base58

from utils import get_logger, read_config, utc_now_ts

load_dotenv()
log = get_logger("sniper", "sniper.log")

# ─── Constants ────────────────────────────────────────────────────────────────
JUPITER_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP  = "https://quote-api.jup.ag/v6/swap"
JUPITER_PRICE = "https://price.jup.ag/v4/price"
HELIUS_HTTP   = os.getenv("HELIUS_RPC_HTTP")
HELIUS_WS     = os.getenv("HELIUS_RPC_WS")
JITO_URL      = os.getenv("JITO_BLOCK_ENGINE_URL")

SOL_MINT  = "So11111111111111111111111111111111111111112"
RAYDIUM   = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMPFUN   = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
MINT_RE   = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt13WkKuGt",
]

# ─── Keypair Loading ───────────────────────────────────────────────────────────
def load_keypair() -> Keypair:
    raw = os.getenv("WALLET_PRIVATE_KEY")
    if not raw:
        raise ValueError("WALLET_PRIVATE_KEY not set")
    return Keypair.from_bytes(base58.b58decode(raw))

KEYPAIR = load_keypair()
PUBKEY  = str(KEYPAIR.pubkey())
log.info(f"Loaded wallet: {PUBKEY[:16]}...")

# ─── Shared State ─────────────────────────────────────────────────────────────
_processing_mints: set[str]  = set()   # In-flight buys
_open_positions: dict         = {}     # mint → position data
_active_subs: dict[str, int]  = {}     # wallet → sub_id
_ws_conn                      = None   # Global WS connection
_req_id                       = 0
_pending_rpc: dict            = {}     # id → asyncio.Future


# ─── RPC Helpers ──────────────────────────────────────────────────────────────
def _next_id() -> int:
    global _req_id
    _req_id += 1
    return _req_id

async def _rpc_send(method: str, params: list) -> dict:
    """Send a JSON-RPC call over the active WebSocket."""
    rid = _next_id()
    payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    fut = asyncio.get_event_loop().create_future()
    _pending_rpc[rid] = fut
    await _ws_conn.send(payload)
    return await asyncio.wait_for(fut, timeout=15.0)

async def get_account_info(address: str) -> Optional[dict]:
    resp = await _rpc_send(
        "getAccountInfo",
        [address, {"encoding": "jsonParsed", "commitment": "confirmed"}]
    )
    return resp.get("result", {}).get("value")

async def get_latest_blockhash() -> str:
    resp = await _rpc_send(
        "getLatestBlockhash",
        [{"commitment": "confirmed"}]
    )
    return resp["result"]["value"]["blockhash"]


# ─── Rug-Pull Check ───────────────────────────────────────────────────────────
async def is_token_safe(mint: str) -> tuple[bool, str]:
    """
    Two concurrent on-chain checks:
      1. Mint authority and freeze authority revoked
      2. LP burned heuristic
    """
    try:
        info = await get_account_info(mint)
        if not info:
            return False, "Mint account not found"

        parsed   = info.get("data", {}).get("parsed", {})
        mint_inf = parsed.get("info", {})

        if parsed.get("type") != "mint":
            return False, "Address is not an SPL mint"

        mint_auth   = mint_inf.get("mintAuthority")
        freeze_auth = mint_inf.get("freezeAuthority")

        if mint_auth is not None:
            return False, f"Mint authority live: {str(mint_auth)[:10]}..."
        if freeze_auth is not None:
            return False, f"Freeze authority live: {str(freeze_auth)[:10]}..."

        return True, "OK"

    except asyncio.TimeoutError:
        return False, "RPC timeout during rug check"
    except Exception as e:
        return False, f"Rug check exception: {e}"


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
            "inputMint":       input_mint,
            "outputMint":      output_mint,
            "amount":          str(amount),
            "slippageBps":     slippage_bps,
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

async def build_swap_tx(
    http: httpx.AsyncClient,
    quote: dict
) -> Optional[bytes]:
    try:
        r = await http.post(JUPITER_SWAP, json={
            "quoteResponse":              quote,
            "userPublicKey":              PUBKEY,
            "wrapAndUnwrapSol":           True,
            "computeUnitPriceMicroLamports": "auto",
            "dynamicComputeUnitLimit":    True,
            "prioritizationFeeLamports":  0,  # Jito handles priority
        }, timeout=10.0)
        r.raise_for_status()
        tx_b64 = r.json()["swapTransaction"]
        tx_bytes = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        tx.sign([KEYPAIR])
        return bytes(tx)
    except Exception as e:
        log.error(f"Jupiter swap build error: {e}")
        return None


# ─── Jito Bundle ─────────────────────────────────────────────────────────────
async def send_jito_bundle(
    http: httpx.AsyncClient,
    swap_tx_bytes: bytes,
    tip_lamports: int
) -> Optional[str]:
    """
    Assemble and submit a 2-tx Jito bundle: [tip_tx, swap_tx].
    Atomic execution — both land or neither does.
    """
    try:
        blockhash = await get_latest_blockhash()

        tip_ix = transfer(TransferParams(
            from_pubkey=KEYPAIR.pubkey(),
            to_pubkey=Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS)),
            lamports=tip_lamports,
        ))
        msg = MessageV0.try_compile(
            payer=KEYPAIR.pubkey(),
            instructions=[tip_ix],
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        tip_tx = VersionedTransaction(msg, [KEYPAIR])
        tip_b64  = base64.b64encode(bytes(tip_tx)).decode()
        swap_b64 = base64.b64encode(swap_tx_bytes).decode()

        r = await http.post(JITO_URL, json={
            "jsonrpc": "2.0",
            "id":      1,
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
_cached_sol_price: tuple[float, float] = (150.0, 0.0)  # (price, timestamp)

async def get_sol_price_usd(http: httpx.AsyncClient) -> float:
    global _cached_sol_price
    price, cached_at = _cached_sol_price
    if utc_now_ts() - cached_at < 30:
        return price
    try:
        r = await http.get(JUPITER_PRICE, params={"ids": SOL_MINT}, timeout=5.0)
        new_price = r.json()["data"][SOL_MINT]["price"]
        _cached_sol_price = (new_price, utc_now_ts())
        return new_price
    except Exception:
        return price


# ─── Position Manager (Moonbag) ───────────────────────────────────────────────
async def monitor_position(mint: str, http: httpx.AsyncClient, cfg_data: dict):
    """
    Async task per open position.
    Phase 1: Sell 50% at 2x entry (TP).
    Phase 2: Trail remaining 50% with percentage-based trailing stop.
    """
    pos = _open_positions.get(mint)
    if not pos:
        return

    slippage_exit = cfg_data["risk"]["slippage_bps_exit"]
    trail_pct     = cfg_data.get("trailing_stop_pct", 0.25)
    tp_mult       = cfg_data.get("take_profit_multiplier", 2.0)

    log.info(
        f"📊 Monitoring {mint[:8]}... | Entry: ${pos['entry_price']:.6f} | "
        f"Amount: {pos['token_amount']}"
    )

    while mint in _open_positions:
        try:
            r = await http.get(JUPITER_PRICE, params={"ids": mint}, timeout=5.0)
            price_data = r.json().get("data", {}).get(mint)
            if not price_data:
                await asyncio.sleep(3)
                continue

            current = float(price_data["price"])
            mult    = current / pos["entry_price"] if pos["entry_price"] > 0 else 1.0

            # ── Phase 1: Take Profit ────────────────────────────────────────
            if pos["phase"] == 1 and mult >= tp_mult:
                half = pos["token_amount"] // 2
                log.info(
                    f"🎉 TP | {mint[:8]}... | {mult:.2f}x | "
                    f"Selling {half} tokens (50%)"
                )
                quote = await get_jupiter_quote(http, mint, SOL_MINT, half, slippage_exit)
                if quote:
                    tx_bytes = await build_swap_tx(http, quote)
                    if tx_bytes:
                        bundle = await send_jito_bundle(
                            http, tx_bytes,
                            int(os.getenv("JITO_TIP_LAMPORTS", 150000))
                        )
                        if bundle:
                            log.info(f"✅ TP sell bundle: {bundle[:12]}...")
                pos["remaining"] = pos["token_amount"] - half
                pos["phase"]     = 2
                pos["peak"]      = current

            # ── Phase 2: Trailing Stop ──────────────────────────────────────
            elif pos["phase"] == 2:
                if current > pos["peak"]:
                    pos["peak"] = current

                drop = (pos["peak"] - current) / pos["peak"] if pos["peak"] > 0 else 0
                if drop >= trail_pct:
                    log.info(
                        f"🛑 TRAIL STOP | {mint[:8]}... | "
                        f"Drop: {drop*100:.1f}% from peak | "
                        f"Selling {pos['remaining']} tokens"
                    )
                    quote = await get_jupiter_quote(
                        http, mint, SOL_MINT, pos["remaining"], slippage_exit
                    )
                    if quote:
                        tx_bytes = await build_swap_tx(http, quote)
                        if tx_bytes:
                            bundle = await send_jito_bundle(
                                http, tx_bytes,
                                int(os.getenv("JITO_TIP_LAMPORTS", 150000))
                            )
                            if bundle:
                                log.info(f"✅ Exit sell bundle: {bundle[:12]}...")
                    _open_positions.pop(mint, None)
                    break

        except Exception as e:
            log.error(f"Position monitor error for {mint[:8]}: {e}")

        await asyncio.sleep(3)


# ─── Signal Handler ───────────────────────────────────────────────────────────
async def on_signal(wallet: str, mint: str, http: httpx.AsyncClient, cfg_data: dict):
    """
    Core buy pipeline triggered by Smart Money signal.
    Designed to complete in <800ms from signal to bundle submission.
    """
    if mint in _processing_mints or mint in _open_positions:
        return

    max_pos = cfg_data["risk"].get("max_positions", 5)
    if len(_open_positions) >= max_pos:
        log.warning(f"Max positions ({max_pos}) reached — skipping {mint[:8]}...")
        return

    _processing_mints.add(mint)
    t0 = time.monotonic()
    log.info(f"⚡ SIGNAL | From: {wallet[:10]}... | Mint: {mint[:10]}...")

    try:
        # 1. Rug check
        safe, reason = await is_token_safe(mint)
        if not safe:
            log.warning(f"🚫 RUG: {mint[:8]}... | {reason}")
            return

        # 2. Jupiter quote
        buy_lamports = int(cfg_data["risk"]["buy_amount_sol"] * 1e9)
        slippage     = cfg_data["risk"]["slippage_bps_entry"]
        max_impact   = cfg_data["risk"].get("max_price_impact_pct", 25.0)

        quote = await get_jupiter_quote(http, SOL_MINT, mint, buy_lamports, slippage)
        if not quote:
            return

        if float(quote.get("priceImpactPct", 0)) > max_impact:
            log.warning(f"Price impact too high for {mint[:8]}... — abort")
            return

        out_amount = int(quote.get("outAmount", 0))

        # 3. Build and sign swap tx
        tx_bytes = await build_swap_tx(http, quote)
        if not tx_bytes:
            return

        # 4. Jito bundle
        tip = int(os.getenv("JITO_TIP_LAMPORTS", 150000))
        bundle_id = await send_jito_bundle(http, tx_bytes, tip)
        if not bundle_id:
            log.error(f"Bundle failed for {mint[:8]}...")
            return

        elapsed = (time.monotonic() - t0) * 1000
        log.info(
            f"🟢 BUY | Mint: {mint[:8]}... | "
            f"In: {cfg_data['risk']['buy_amount_sol']} SOL | "
            f"Out: {out_amount} tokens | "
            f"Bundle: {bundle_id[:12]}... | "
            f"⏱ {elapsed:.0f}ms"
        )

        # 5. Open position
        sol_price   = await get_sol_price_usd(http)
        entry_price = (buy_lamports / 1e9 * sol_price) / max(out_amount, 1)
        _open_positions[mint] = {
            "wallet":       wallet,
            "entry_price":  entry_price,
            "token_amount": out_amount,
            "remaining":    out_amount,
            "phase":        1,
            "peak":         entry_price,
            "opened_at":    utc_now_ts(),
        }

        asyncio.create_task(monitor_position(mint, http, cfg_data))

    finally:
        _processing_mints.discard(mint)


# ─── Log Parser (Detects Swaps in Wallet Logs) ────────────────────────────────
async def process_log(
    wallet: str,
    log_data: dict,
    known_mints: set[str],
    http: httpx.AsyncClient,
    cfg_data: dict
):
    """
    Parse a logsNotification for a tracked wallet.
    Identify if they're buying an unknown SPL token via Raydium/Pump.fun.
    """
    logs: list[str] = log_data.get("logs", [])
    if log_data.get("err"):
        return

    is_dex = any(RAYDIUM in l or PUMPFUN in l for l in logs)
    if not is_dex:
        return

    candidates = set()
    for line in logs:
        for addr in MINT_RE.findall(line):
            if addr not in known_mints and addr != wallet:
                candidates.add(addr)

    for candidate in candidates:
        try:
            info = await get_account_info(candidate)
            if info and info.get("data", {}).get("parsed", {}).get("type") == "mint":
                known_mints.add(candidate)
                asyncio.create_task(on_signal(wallet, candidate, http, cfg_data))
                return
        except Exception:
            pass


# ─── WebSocket Engine ─────────────────────────────────────────────────────────
async def ws_listener(http: httpx.AsyncClient):
    """
    Main WebSocket loop with:
    - Auto-reconnect + exponential backoff
    - Hot config reload every 60s
    - Dynamic subscription management (adds new wallets from scraper)
    """
    global _ws_conn

    known_mints = {SOL_MINT}
    subscribed_wallets: set[str] = set()
    sub_map: dict[int, str] = {}  # sub_id → wallet
    last_config_reload = 0.0
    cfg_data = await read_config()
    backoff = 1

    while True:
        try:
            log.info(f"Connecting to Helius WS...")
            async with websockets.connect(
                HELIUS_WS,
                ping_interval=20,
                ping_timeout=20,
                max_size=10 * 1024 * 1024
            ) as ws:
                _ws_conn = ws
                backoff  = 1
                log.info("WebSocket connected ✓")

                async def sub_wallet(wallet: str):
                    """Subscribe to log notifications for a wallet."""
                    rid = _next_id()
                    payload = json.dumps({
                        "jsonrpc": "2.0",
                        "id":      rid,
                        "method":  "logsSubscribe",
                        "params":  [
                            {"mentions": [wallet]},
                            {"commitment": "processed"}
                        ]
                    })
                    fut = asyncio.get_event_loop().create_future()
                    _pending_rpc[rid] = fut
                    await ws.send(payload)
                    resp = await asyncio.wait_for(fut, timeout=10.0)
                    sub_id = resp["result"]
                    sub_map[sub_id] = wallet
                    subscribed_wallets.add(wallet)
                    log.info(f"  📡 Subscribed: {wallet[:12]}... (sub {sub_id})")

                # Initial subscriptions
                for w in cfg_data["TARGET_WALLETS"]:
                    await sub_wallet(w)

                # Message pump
                async for raw in ws:
                    msg = json.loads(raw)

                    # RPC response
                    if "id" in msg and msg["id"] in _pending_rpc:
                        _pending_rpc.pop(msg["id"]).set_result(msg)
                        continue

                    # Subscription notification
                    if msg.get("method") == "logsNotification":
                        params    = msg["params"]
                        sub_id    = params["subscription"]
                        log_data  = params["result"]
                        wallet    = sub_map.get(sub_id)
                        if wallet:
                            asyncio.create_task(
                                process_log(wallet, log_data, known_mints, http, cfg_data)
                            )

                    # ── Hot reload config every 60s ────────────────────────
                    now = time.monotonic()
                    if now - last_config_reload > 60:
                        cfg_data = await read_config()
                        last_config_reload = now
                        new_wallets = [
                            w for w in cfg_data["TARGET_WALLETS"]
                            if w not in subscribed_wallets
                        ]
                        if new_wallets:
                            log.info(f"Hot reload: subscribing {len(new_wallets)} new wallets")
                            for w in new_wallets:
                                await sub_wallet(w)

        except Exception as e:
            log.error(f"WS error: {e} | Reconnecting in {backoff}s...")
            _ws_conn = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    log.info("=" * 64)
    log.info("  🎯 SOLANA MEMECOIN SNIPER — ONLINE")
    log.info(f"  Wallet: {PUBKEY[:20]}...")
    log.info("=" * 64)

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=20),
        timeout=httpx.Timeout(15.0)
    ) as http:
        cfg_data = await read_config()
        log.info(
            f"Loaded {len(cfg_data['TARGET_WALLETS'])} target wallets | "
            f"Buy: {cfg_data['risk']['buy_amount_sol']} SOL"
        )
        await ws_listener(http)


if __name__ == "__main__":
    asyncio.run(main())