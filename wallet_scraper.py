"""
wallet_scraper.py — Free-Tier Auto-Discovery Engine (v1.1.0)

Zero-cost API stack:
  - DexScreener (completely free, no key needed)
  - Helius RPC   (free tier — for tx history + account checks)
  - SQLite       (local — wallet trade history + win rates)

No BirdEye. No paid APIs.

Pipeline:
  1. DexScreener  → top Solana pools created <24h, volume >$500k
  2. DexScreener  → fetch transactions for each pool's pair address
  3. Helius RPC   → getSignaturesForAddress per candidate wallet
  4. Helius RPC   → getTransaction to reconstruct trade history
  5. SQLite       → compute historical win rate across multiple tokens
  6. config.json  → add qualifying wallets / prune underperformers

Rate limiting strategy:
  - All DexScreener calls: 6-8s delay between requests
  - All Helius RPC calls:  2-4s delay per wallet
  - Full market sweep may take 20-40 minutes — this is intentional.
  - A slow scraper that never 429s beats a fast one that gets banned.

Bootstrap note:
  - On first run, SQLite is empty and no wallets will be qualified.
  - The scraper must run 3-5 cycles (18-30h) to build enough history.
  - Clear log output guides you through this observation phase.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
import httpx
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential

from utils import (
    get_logger, init_db, insert_trade, upsert_wallet_meta,
    add_wallet_to_config, prune_wallet_from_config,
    read_config, write_config, utc_now_ts, DB_PATH
)

load_dotenv()
log = get_logger("scraper", "scraper.log")

# ─── API Endpoints ────────────────────────────────────────────────────────────
HELIUS_HTTP       = os.getenv("HELIUS_RPC_HTTP")
DEXSCREENER_PAIRS = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_PAIR  = "https://api.dexscreener.com/latest/dex/pairs/solana"
DEXSCREENER_TOKEN = "https://api.dexscreener.com/latest/dex/tokens"
JUPITER_PRICE     = "https://price.jup.ag/v4/price"

# Solana program IDs for DEX detection
RAYDIUM_AMM  = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMPFUN      = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_PROG   = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOL_MINT     = "So11111111111111111111111111111111111111112"
WSOL_MINT    = "So11111111111111111111111111111111111111112"
SYSTEM_PROG  = "11111111111111111111111111111111"

# Wallets to always ignore (known infrastructure / validators)
BLACKLIST = {
    SYSTEM_PROG,
    TOKEN_PROG,
    RAYDIUM_AMM,
    PUMPFUN,
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe8bv",  # ATA program
    "11111111111111111111111111111111",
}


# ─── Helius RPC Helpers ───────────────────────────────────────────────────────
async def rpc_post(
    client: httpx.AsyncClient,
    method: str,
    params: list,
    delay: float = 2.0
) -> dict:
    """
    Single Helius RPC call with built-in rate-limit delay.
    All Helius calls go through here so delays are centralised.
    """
    await asyncio.sleep(delay)
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  method,
        "params":  params
    }
    try:
        r = await client.post(HELIUS_HTTP, json=payload, timeout=15.0)
        if r.status_code == 429:
            log.warning("Helius 429 — backing off 30s...")
            await asyncio.sleep(30)
            r = await client.post(HELIUS_HTTP, json=payload, timeout=15.0)
        r.raise_for_status()
        return r.json().get("result", {})
    except httpx.HTTPStatusError as e:
        log.error(f"RPC HTTP error {e.response.status_code} for {method}")
        return {}
    except Exception as e:
        log.error(f"RPC error ({method}): {e}")
        return {}


# ─── Step 1: DexScreener Pool Discovery ──────────────────────────────────────
async def fetch_hot_pools(
    client: httpx.AsyncClient,
    cfg: dict
) -> list[dict]:
    """
    Fetch hot Solana pools from DexScreener using the search endpoint.
    Completely free — no API key required.

    Filters applied:
      - Chain: Solana only
      - Created within last 24h
      - 24h volume > $500k (configurable)
      - Liquidity > $50k (rules out ghost pools)
    """
    min_vol   = cfg["filters"]["discovery_pool_min_volume_usd"]
    max_age_h = cfg["filters"]["discovery_pool_max_age_hours"]
    cutoff_ts = utc_now_ts() - (max_age_h * 3600)
    max_pools = cfg["free_tier"]["dexscreener_max_pools_per_run"]

    log.info(f"Fetching DexScreener pools (min vol ${min_vol:,.0f}, age <{max_age_h}h)...")

    # DexScreener search: query Solana broadly
    # Free tier — no key, no authentication
    try:
        await asyncio.sleep(3)  # Courtesy delay before first call
        r = await client.get(
            DEXSCREENER_PAIRS,
            params={"q": "solana"},
            timeout=20.0
        )
        if r.status_code == 429:
            log.warning("DexScreener 429 — waiting 60s...")
            await asyncio.sleep(60)
            r = await client.get(DEXSCREENER_PAIRS, params={"q": "solana"}, timeout=20.0)
        r.raise_for_status()
        pairs = r.json().get("pairs", [])
    except Exception as e:
        log.error(f"DexScreener pool fetch failed: {e}")
        return []

    qualifying = []
    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue

        created_ms  = pair.get("pairCreatedAt") or 0
        created_ts  = created_ms / 1000
        volume_24h  = float(pair.get("volume", {}).get("h24", 0) or 0)
        liquidity   = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        base_mint   = pair.get("baseToken", {}).get("address", "")

        if not base_mint or base_mint in BLACKLIST:
            continue
        if created_ts < cutoff_ts:
            continue
        if volume_24h < min_vol:
            continue
        if liquidity < 50_000:
            continue

        qualifying.append({
            "pair_address": pair.get("pairAddress", ""),
            "base_mint":    base_mint,
            "base_symbol":  pair.get("baseToken", {}).get("symbol", "?"),
            "quote_mint":   pair.get("quoteToken", {}).get("address", SOL_MINT),
            "volume_24h":   volume_24h,
            "liquidity_usd": liquidity,
            "price_change_24h": float(pair.get("priceChange", {}).get("h24", 0) or 0),
            "created_ts":   created_ts,
            "dex":          pair.get("dexId", ""),
        })

    # Sort by volume descending — highest signal first
    qualifying.sort(key=lambda p: p["volume_24h"], reverse=True)
    qualifying = qualifying[:max_pools]

    log.info(f"  → {len(qualifying)} pools qualify (showing top {max_pools})")
    for p in qualifying:
        log.info(
            f"    {p['base_symbol']:<10} | Vol: ${p['volume_24h']:>12,.0f} | "
            f"Liq: ${p['liquidity_usd']:>10,.0f} | DEX: {p['dex']}"
        )
    return qualifying


# ─── Step 2: Extract Wallet Candidates from DexScreener Transactions ──────────
async def extract_wallets_from_pool(
    client: httpx.AsyncClient,
    pool: dict,
    delay: float
) -> list[dict]:
    """
    DexScreener doesn't expose a direct "top traders" endpoint for free.

    Strategy: fetch the token page from DexScreener which includes
    recent transactions, then extract the unique maker addresses.
    We then validate each wallet via Helius RPC.

    Returns a list of candidate wallet dicts with basic PnL estimates.
    """
    base_mint = pool["base_mint"]
    log.info(f"  Extracting wallets from pool: {pool['base_symbol']} ({base_mint[:8]}...)")

    await asyncio.sleep(delay)

    try:
        # DexScreener token endpoint — includes recent swaps with maker addresses
        r = await client.get(
            f"{DEXSCREENER_TOKEN}/{base_mint}",
            timeout=15.0
        )
        if r.status_code == 429:
            log.warning("DexScreener token 429 — waiting 45s...")
            await asyncio.sleep(45)
            r = await client.get(f"{DEXSCREENER_TOKEN}/{base_mint}", timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error(f"DexScreener token fetch failed for {base_mint[:8]}: {e}")
        return []

    pairs = data.get("pairs", [])
    if not pairs:
        return []

    # Collect unique maker addresses from recent txs across all pairs for this token
    candidate_wallets: dict[str, dict] = {}

    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue

        txns = pair.get("txns", {})
        # DexScreener txns is a summary (buys/sells counts), not individual txs
        # We use the pair address to fetch on-chain data via Helius
        pair_addr = pair.get("pairAddress", "")
        if pair_addr:
            wallets = await extract_wallets_from_pair_onchain(
                client, pair_addr, pool, candidate_wallets
            )

    candidates = list(candidate_wallets.values())
    log.info(f"    Found {len(candidates)} candidate wallets")
    return candidates


async def extract_wallets_from_pair_onchain(
    client: httpx.AsyncClient,
    pair_address: str,
    pool: dict,
    wallet_map: dict
) -> dict:
    """
    Fetch recent transaction signatures for the pool's pair account via Helius.
    Parse each transaction to extract the initiating wallet (index 0 = fee payer).
    This is the ground truth — wallets that actually traded this token.
    """
    max_traders = 8  # Free tier: keep RPC calls minimal

    # Fetch recent sigs for the pair account
    sigs_result = await rpc_post(
        client,
        "getSignaturesForAddress",
        [pair_address, {"limit": 40, "commitment": "confirmed"}],
        delay=2.0
    )

    if not isinstance(sigs_result, list) or not sigs_result:
        return wallet_map

    # Deduplicate — only fetch first `max_traders` unique wallets
    processed_wallets = set(wallet_map.keys())
    sigs_to_process   = [s["signature"] for s in sigs_result if not s.get("err")]

    for sig in sigs_to_process:
        if len(wallet_map) >= max_traders:
            break

        await asyncio.sleep(1.5)  # Conservative delay per tx fetch

        tx = await rpc_post(
            client,
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            delay=0  # Already slept above
        )

        if not tx:
            continue

        account_keys = (
            tx.get("transaction", {})
            .get("message", {})
            .get("accountKeys", [])
        )

        # Index 0 = fee payer = the human wallet initiating the trade
        if not account_keys:
            continue

        initiator = account_keys[0].get("pubkey", "")
        if not initiator or initiator in BLACKLIST or initiator in processed_wallets:
            continue

        # Only count if this looks like a swap (SOL balance change)
        pre_balances  = tx.get("meta", {}).get("preBalances", [])
        post_balances = tx.get("meta", {}).get("postBalances", [])

        if not pre_balances or not post_balances:
            continue

        sol_delta_lamports = post_balances[0] - pre_balances[0]
        sol_delta_sol      = sol_delta_lamports / 1e9

        # Negative delta = wallet spent SOL = BUY
        # Positive delta = wallet received SOL = SELL
        if abs(sol_delta_sol) < 0.001:
            continue  # Dust tx — not a real trade

        processed_wallets.add(initiator)
        wallet_map[initiator] = {
            "wallet":     initiator,
            "token_mint": pool["base_mint"],
            "sol_delta":  sol_delta_sol,
            "is_buy":     sol_delta_sol < 0,
            "tx_sig":     sig,
        }

    return wallet_map


# ─── Step 3: Helius RPC Wallet Validation ────────────────────────────────────
async def get_tx_per_day(client: httpx.AsyncClient, wallet: str, delay: float) -> int:
    """
    Estimate daily transaction count from getSignaturesForAddress.
    Fetch last 200 sigs, measure time window, extrapolate to 24h.
    MEV bots typically exceed 500 tx/day.
    """
    sigs = await rpc_post(
        client,
        "getSignaturesForAddress",
        [wallet, {"limit": 200, "commitment": "confirmed"}],
        delay=delay
    )

    if not isinstance(sigs, list) or len(sigs) < 2:
        return len(sigs) if isinstance(sigs, list) else 0

    newest_ts = sigs[0].get("blockTime", 0) or 0
    oldest_ts = sigs[-1].get("blockTime", 1) or 1
    window    = max(newest_ts - oldest_ts, 1)
    return int((len(sigs) / window) * 86400)


async def get_wallet_trade_history(
    client: httpx.AsyncClient,
    wallet: str,
    known_mints: set[str],
    delay_per_tx: float
) -> list[dict]:
    """
    Reconstruct a wallet's SPL token trade history from on-chain data.

    Process:
      1. Fetch last 100 signatures for the wallet
      2. For each tx, check if it involves a known memecoin mint
      3. Compute SOL in/out as a proxy for ROI
         (entry = SOL spent buying, exit = SOL received selling)
      4. Return list of completed round-trips (buy + sell for same mint)

    This is slower than BirdEye but costs $0.
    """
    trades = []

    sigs = await rpc_post(
        client,
        "getSignaturesForAddress",
        [wallet, {"limit": 100, "commitment": "confirmed"}],
        delay=delay_per_tx
    )

    if not isinstance(sigs, list):
        return trades

    # Group transactions by token mint
    token_positions: dict[str, dict] = {}

    for sig_info in sigs:
        if sig_info.get("err"):
            continue

        sig = sig_info.get("signature", "")
        if not sig:
            continue

        await asyncio.sleep(delay_per_tx)

        tx = await rpc_post(
            client,
            "getTransaction",
            [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            delay=0
        )

        if not tx:
            continue

        block_time = tx.get("blockTime", 0) or 0
        meta       = tx.get("meta", {}) or {}
        account_keys = (
            tx.get("transaction", {})
            .get("message", {})
            .get("accountKeys", [])
        )

        # Map pubkey to index for balance lookups
        pubkey_to_idx = {
            ak.get("pubkey", ""): i
            for i, ak in enumerate(account_keys)
        }
        wallet_idx = pubkey_to_idx.get(wallet)
        if wallet_idx is None:
            continue

        pre_balances  = meta.get("preBalances", [])
        post_balances = meta.get("postBalances", [])

        if not pre_balances or not post_balances:
            continue

        sol_delta = (
            (post_balances[wallet_idx] - pre_balances[wallet_idx]) / 1e9
            if wallet_idx < len(pre_balances) else 0
        )

        # Inspect token balance changes
        pre_token  = meta.get("preTokenBalances", [])
        post_token = meta.get("postTokenBalances", [])

        mints_in_tx = set()
        for tb in pre_token + post_token:
            mint = tb.get("mint", "")
            if mint and mint not in {SOL_MINT, WSOL_MINT}:
                mints_in_tx.add(mint)

        for mint in mints_in_tx:
            if mint not in known_mints:
                continue

            # Find token balance delta for this wallet + mint
            pre_bal  = next(
                (float(tb.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                 for tb in pre_token
                 if tb.get("mint") == mint and tb.get("owner") == wallet), 0.0
            )
            post_bal = next(
                (float(tb.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
                 for tb in post_token
                 if tb.get("mint") == mint and tb.get("owner") == wallet), 0.0
            )
            token_delta = post_bal - pre_bal

            if token_delta > 0 and sol_delta < 0:
                # BUY: received tokens, spent SOL
                if mint not in token_positions:
                    token_positions[mint] = {
                        "mint":      mint,
                        "sol_in":    abs(sol_delta),
                        "tokens":    token_delta,
                        "buy_ts":    block_time,
                        "sol_out":   0.0,
                        "sell_ts":   None,
                    }
                else:
                    # Averaging in
                    token_positions[mint]["sol_in"]  += abs(sol_delta)
                    token_positions[mint]["tokens"]  += token_delta

            elif token_delta < 0 and sol_delta > 0:
                # SELL: spent tokens, received SOL
                if mint in token_positions:
                    pos = token_positions[mint]
                    pos["sol_out"]  += sol_delta
                    pos["sell_ts"]  = block_time
                    roi = ((pos["sol_out"] - pos["sol_in"]) / pos["sol_in"] * 100
                           if pos["sol_in"] > 0 else 0)
                    trades.append({
                        "mint":       mint,
                        "sol_in":     pos["sol_in"],
                        "sol_out":    pos["sol_out"],
                        "roi_pct":    roi,
                        "is_win":     1 if roi > 0 else 0,
                        "buy_ts":     pos["buy_ts"],
                        "sell_ts":    block_time,
                    })
                    del token_positions[mint]  # Position closed

    return trades


# ─── Step 4: Win Rate Computation ────────────────────────────────────────────
def compute_win_rate_from_db_rows(rows: list[tuple]) -> dict:
    """
    Compute performance stats from SQLite trade rows.
    Each row: (roi_pct, is_win, trade_ts, token_mint)
    """
    if not rows:
        return {
            "win_rate":     0.0,
            "total_trades": 0,
            "unique_tokens": 0,
            "avg_roi_pct":  0.0,
        }

    rois         = [r[0] for r in rows]
    wins         = sum(r[1] for r in rows)
    unique_mints = len(set(r[3] for r in rows))
    total        = len(rows)

    return {
        "win_rate":      wins / total,
        "total_trades":  total,
        "unique_tokens": unique_mints,
        "avg_roi_pct":   sum(rois) / total,
        "max_win_pct":   max(rois),
        "max_loss_pct":  min(rois),
    }


async def get_stats_from_db(wallet: str, lookback_days: int | None = None) -> dict:
    """Load wallet's trade history from SQLite and compute win rate."""
    cutoff = utc_now_ts() - (lookback_days * 86400) if lookback_days else 0

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT roi_pct, is_win, trade_ts, token_mint
               FROM wallet_trades
               WHERE wallet=? AND trade_ts >= ?
               ORDER BY trade_ts DESC""",
            (wallet, cutoff)
        )
        rows = await cursor.fetchall()

    return compute_win_rate_from_db_rows(rows)


# ─── Step 5: Full Vetting Pipeline ───────────────────────────────────────────
async def vet_wallet(
    client: httpx.AsyncClient,
    wallet: str,
    token_mint: str,
    cfg_data: dict,
    known_mints: set[str]
) -> tuple[bool, str]:
    """
    Full 5-gate filtration. Returns (passed, reason).
    Conservative delays throughout to respect Helius free tier.
    """
    filters   = cfg_data["filters"]
    ft        = cfg_data["free_tier"]
    tx_delay  = ft["scraper_delay_between_tx_fetches_sec"]

    # ── Gate 1: MEV / Bot Filter ─────────────────────────────────────────────
    log.info(f"    [1/4] Checking tx volume: {wallet[:10]}...")
    tx_day = await get_tx_per_day(client, wallet, delay=tx_delay)
    log.info(f"          → {tx_day} tx/day")

    if tx_day > filters["max_tx_per_day"]:
        return False, f"MEV/bot: {tx_day} tx/day > {filters['max_tx_per_day']}"

    # ── Gate 2: Load historical trades from DB ────────────────────────────────
    stats_all = await get_stats_from_db(wallet)

    # ── Gate 3: Build trade history if insufficient data ──────────────────────
    if stats_all["total_trades"] < filters["min_trades_for_qualification"]:
        log.info(
            f"    [2/4] Insufficient DB history ({stats_all['total_trades']} trades) — "
            f"fetching on-chain history..."
        )
        trades = await get_wallet_trade_history(client, wallet, known_mints, tx_delay)

        if trades:
            for t in trades:
                await insert_trade(
                    wallet=wallet,
                    token_mint=t["mint"],
                    roi_pct=t["roi_pct"],
                    entry_price=t["sol_in"],
                    exit_price=t["sol_out"],
                    trade_ts=float(t["sell_ts"] or t["buy_ts"]),
                    source="rpc"
                )
            log.info(f"          → Ingested {len(trades)} trades into SQLite")
            stats_all = await get_stats_from_db(wallet)
        else:
            log.info(f"          → No closed trades found on-chain yet")
    else:
        log.info(f"    [2/4] DB history: {stats_all['total_trades']} trades found ✓")

    # ── Gate 4: Minimum Trade Count ───────────────────────────────────────────
    if stats_all["total_trades"] < filters["min_trades_for_qualification"]:
        return False, (
            f"Insufficient history: {stats_all['total_trades']} trades "
            f"< min {filters['min_trades_for_qualification']}"
        )

    # ── Gate 5: Unique Token Diversity ───────────────────────────────────────
    log.info(f"    [3/4] Unique tokens: {stats_all['unique_tokens']}")
    if stats_all["unique_tokens"] < filters["min_unique_tokens_traded"]:
        return False, (
            f"One-hit wonder: {stats_all['unique_tokens']} unique tokens "
            f"< min {filters['min_unique_tokens_traded']}"
        )

    # ── Gate 6: Win Rate ──────────────────────────────────────────────────────
    wr = stats_all["win_rate"]
    log.info(f"    [4/4] Win rate: {wr:.0%} (need ≥{filters['min_win_rate']:.0%})")

    if wr < filters["min_win_rate"]:
        return False, f"Win rate {wr:.0%} < {filters['min_win_rate']:.0%}"

    # ── All gates passed — persist metadata ──────────────────────────────────
    stats_7d = await get_stats_from_db(wallet, lookback_days=7)
    await upsert_wallet_meta(
        wallet=wallet,
        win_rate_all=wr,
        win_rate_7d=stats_7d["win_rate"],
        total_trades=stats_all["total_trades"],
        unique_tokens=stats_all["unique_tokens"],
        tx_per_day_avg=tx_day,
        last_checked_ts=utc_now_ts(),
        status="candidate"
    )

    return True, "PASS"


# ─── Step 6: Prune Underperformers ───────────────────────────────────────────
async def prune_underperformers(cfg_data: dict):
    """
    Check 7-day win rate of all active TARGET_WALLETS.
    Prune any that have dropped below the configured threshold.
    """
    prune_threshold = cfg_data["filters"]["prune_win_rate"]
    lookback        = cfg_data["filters"]["prune_lookback_days"]

    log.info(f"Pruning wallets with 7d win rate < {prune_threshold:.0%}...")
    pruned_count = 0

    for wallet in list(cfg_data["TARGET_WALLETS"]):
        stats_7d = await get_stats_from_db(wallet, lookback_days=lookback)

        if stats_7d["total_trades"] < 3:
            log.debug(f"  {wallet[:10]}... — not enough recent data to prune")
            continue

        if stats_7d["win_rate"] < prune_threshold:
            await prune_wallet_from_config(
                wallet,
                reason=f"7d WR {stats_7d['win_rate']:.0%} < {prune_threshold:.0%}"
            )
            await upsert_wallet_meta(
                wallet=wallet,
                win_rate_7d=stats_7d["win_rate"],
                status="pruned",
                last_checked_ts=utc_now_ts()
            )
            pruned_count += 1

    log.info(f"Pruning complete — {pruned_count} wallets removed.")


# ─── Bootstrap Phase Manager ─────────────────────────────────────────────────
async def check_bootstrap_phase(cfg_data: dict) -> bool:
    """
    Returns True if we're still in the observation / bootstrap phase.
    Logs clear, informative messages so the operator knows the system
    is working even when no wallets have been qualified yet.
    """
    meta = cfg_data.get("meta", {})

    # Record bootstrap start time on first run
    if not meta.get("bootstrap_started_ts"):
        cfg_data["meta"]["bootstrap_started_ts"] = utc_now_ts()
        await write_config(cfg_data)

    started_ts    = float(cfg_data["meta"]["bootstrap_started_ts"])
    elapsed_hours = (utc_now_ts() - started_ts) / 3600
    min_hours     = cfg_data["free_tier"]["observation_phase_min_hours"]
    total_targets = len(cfg_data["TARGET_WALLETS"])

    log.info("─" * 64)
    log.info("  📡 OBSERVATION PHASE STATUS")
    log.info(f"  Time elapsed:      {elapsed_hours:.1f}h / {min_hours}h minimum")
    log.info(f"  Wallets qualified: {total_targets}")

    if total_targets == 0 and elapsed_hours < min_hours:
        log.info("")
        log.info("  ⏳ This is NORMAL. The scraper needs to observe wallets")
        log.info("     across multiple cycles before statistical win rates")
        log.info("     become reliable. No wallets will be added until:")
        log.info(f"     → At least {int(min_hours)}h of observation has passed")
        log.info(f"     → A wallet has ≥ {cfg_data['filters']['min_trades_for_qualification']} "
                 f"completed trades in SQLite")
        log.info(f"     → That wallet's win rate is ≥ "
                 f"{cfg_data['filters']['min_win_rate']:.0%} across "
                 f"≥ {cfg_data['filters']['min_unique_tokens_traded']} unique tokens")
        log.info("")
        log.info("  💡 Run the scraper again in 6h. After 2-3 cycles, the")
        log.info("     SQLite database will have enough data to qualify wallets.")
        log.info("─" * 64)
        return True

    if total_targets == 0:
        log.info("")
        log.info("  ⚠️  Observation phase complete but no wallets qualified yet.")
        log.info("     Possible reasons:")
        log.info("     → Memecoins discovered so far had no wallets with >60% WR")
        log.info("     → Wallet histories are too short (< 5 completed trades)")
        log.info("     → Market conditions: few new pools with >$500k volume")
        log.info("  Continuing to collect data — this is expected behaviour.")
        log.info("─" * 64)

    return False


# ─── Main Scraper Loop ────────────────────────────────────────────────────────
async def run_scraper():
    log.info("=" * 64)
    log.info("  🔍 WALLET AUTO-DISCOVERY ENGINE — FREE TIER MODE")
    log.info("  APIs: DexScreener (free) + Helius RPC (free tier)")
    log.info("  BirdEye: DISABLED (zero-cost mode)")
    log.info("=" * 64)

    await init_db()
    cfg_data = await read_config()

    log.info(f"Config loaded:")
    log.info(f"  Max tracked wallets:    {cfg_data['free_tier']['max_tracked_wallets']}")
    log.info(f"  Min win rate:           {cfg_data['filters']['min_win_rate']:.0%}")
    log.info(f"  Min unique tokens:      {cfg_data['filters']['min_unique_tokens_traded']}")
    log.info(f"  Delay between wallets:  {cfg_data['free_tier']['scraper_delay_between_wallets_sec']}s")
    log.info(f"  Delay between pools:    {cfg_data['free_tier']['scraper_delay_between_pools_sec']}s")

    # Check and report on bootstrap phase
    await check_bootstrap_phase(cfg_data)

    existing = set(cfg_data["TARGET_WALLETS"])
    pruned   = set(cfg_data["PRUNED_WALLETS"])
    skip_set = existing | pruned
    known_mints: set[str] = {SOL_MINT, WSOL_MINT}

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"},
        limits=httpx.Limits(max_connections=5),  # Conservative for free tier
        timeout=httpx.Timeout(20.0)
    ) as client:

        # ── Phase 1: Prune underperformers ────────────────────────────────────
        log.info("\n📋 Phase 1/3 — Pruning underperforming wallets...")
        await prune_underperformers(cfg_data)
        cfg_data = await read_config()

        # ── Phase 2: Discover hot pools ───────────────────────────────────────
        log.info("\n🔥 Phase 2/3 — Discovering hot pools via DexScreener...")
        pools = await fetch_hot_pools(client, cfg_data)

        if not pools:
            log.warning("No qualifying pools found this cycle. Will retry in 6h.")
            return

        # Build known_mints from discovered pools (for trade history reconstruction)
        for p in pools:
            known_mints.add(p["base_mint"])

        # ── Phase 3: Vet wallet candidates ────────────────────────────────────
        log.info(f"\n🎯 Phase 3/3 — Vetting wallets from {len(pools)} pools...")
        log.info("  (This will take 20-40 minutes — that's normal for free tier)")

        pool_delay   = cfg_data["free_tier"]["scraper_delay_between_pools_sec"]
        wallet_delay = cfg_data["free_tier"]["scraper_delay_between_wallets_sec"]

        added   = 0
        checked = 0
        max_cap = cfg_data["free_tier"]["max_tracked_wallets"]

        for i, pool in enumerate(pools):
            # Stop if we've hit the free-tier wallet cap
            if len(cfg_data["TARGET_WALLETS"]) >= max_cap:
                log.info(
                    f"  Wallet cap reached ({max_cap}) — "
                    f"skipping remaining pools."
                )
                break

            log.info(
                f"\n  Pool {i+1}/{len(pools)}: {pool['base_symbol']} | "
                f"Vol: ${pool['volume_24h']:,.0f} | "
                f"DEX: {pool['dex']}"
            )

            # Extract candidates from pool via on-chain data
            candidates = await extract_wallets_from_pool(client, pool, delay=pool_delay)

            for candidate in candidates:
                wallet = candidate["wallet"]
                if wallet in skip_set:
                    continue
                if len(cfg_data["TARGET_WALLETS"]) >= max_cap:
                    break

                checked += 1
                skip_set.add(wallet)
                log.info(f"\n  Vetting wallet {checked}: {wallet[:14]}...")

                # Courtesy delay between wallet checks
                await asyncio.sleep(wallet_delay)

                passed, reason = await vet_wallet(
                    client=client,
                    wallet=wallet,
                    token_mint=pool["base_mint"],
                    cfg_data=cfg_data,
                    known_mints=known_mints
                )

                if passed:
                    stats = await get_stats_from_db(wallet)
                    was_added = await add_wallet_to_config(wallet)
                    if was_added:
                        added += 1
                        log.info(
                            f"  ✅ QUALIFIED: {wallet[:14]}... | "
                            f"WR: {stats['win_rate']:.0%} | "
                            f"Trades: {stats['total_trades']} | "
                            f"Tokens: {stats['unique_tokens']}"
                        )
                    cfg_data = await read_config()
                else:
                    log.info(f"  ❌ REJECTED: {wallet[:14]}... | {reason}")

            # Delay between pools
            if i < len(pools) - 1:
                log.info(f"\n  Waiting {pool_delay}s before next pool...")
                await asyncio.sleep(pool_delay)

    # ── Final Summary ──────────────────────────────────────────────────────────
    cfg_data = await read_config()
    elapsed  = (utc_now_ts() - float(cfg_data["meta"].get("bootstrap_started_ts", utc_now_ts()))) / 3600

    log.info("\n" + "=" * 64)
    log.info("  SCRAPER CYCLE COMPLETE")
    log.info(f"  Wallets checked this run:  {checked}")
    log.info(f"  Wallets added this run:    {added}")
    log.info(f"  Total TARGET_WALLETS:      {len(cfg_data['TARGET_WALLETS'])}")
    log.info(f"  Total PRUNED_WALLETS:      {len(cfg_data['PRUNED_WALLETS'])}")
    log.info(f"  Observation phase time:    {elapsed:.1f}h")

    if len(cfg_data["TARGET_WALLETS"]) == 0:
        log.info("")
        log.info("  ⏳ Still in observation phase — no wallets qualified yet.")
        log.info("     Sniper bot will remain idle until wallets are added.")
        log.info("     Next scraper run in ~6h via systemd timer.")
    else:
        log.info("")
        log.info("  🎯 Active wallets:")
        for w in cfg_data["TARGET_WALLETS"]:
            log.info(f"     → {w}")

    log.info("=" * 64)


if __name__ == "__main__":
    asyncio.run(run_scraper())