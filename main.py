"""
Main Bot Loop — Orchestrates all modules.

Execution flow per signal:
  1. WalletTracker fires on_signal(wallet, mint)
  2. RugChecker.is_safe(mint) — abort if fails
  3. JupiterClient.execute_swap(SOL → mint)
  4. JitoClient.send_bundle(signed_tx)
  5. PositionManager.open_position(...)
  6. PositionManager monitors price + executes exits via JupiterClient
"""

import asyncio
from modules.rpc_client import SolanaRPCClient
from modules.wallet_tracker import WalletTracker
from modules.rug_checker import RugChecker
from modules.jupiter_client import JupiterClient
from modules.jito_client import JitoClient
from modules.position_manager import PositionManager
from config import cfg, keypair
from utils.logger import get_logger

log = get_logger("main")

# Track mints we're already buying to prevent duplicate signals
_processing: set[str] = set()

# Global module instances
rpc = SolanaRPCClient()
rug_checker = RugChecker(rpc)
jupiter = JupiterClient()
jito = JitoClient()

async def on_sell_signal(mint: str, amount: float, slippage_bps: int):
    """Called by PositionManager when exit conditions are met."""
    log.info(f"🔴 SELL | Mint: {mint[:8]}... | Amount: {amount:.4f}")
    result = await jupiter.execute_swap(
        input_mint=mint,
        output_mint=cfg.SOL_MINT,
        amount_lamports=int(amount),  # Note: for SPL tokens, amount is in base units
        slippage_bps=slippage_bps,
        use_jito=True
    )
    if result:
        signed_bytes, quote = result
        bundle_id = await jito.send_bundle(signed_bytes)
        if bundle_id:
            log.info(f"✅ SELL bundle sent | Mint: {mint[:8]}... | Bundle: {bundle_id[:12]}")
        else:
            log.warning(f"Jito failed for sell, no fallback implemented. Review manually.")

position_manager = PositionManager(on_sell=on_sell_signal)

async def on_signal(wallet: str, mint: str):
    """
    Core signal handler — called when a Smart Money wallet buys a new token.
    This must complete in <500ms ideally. Every step is critical path.
    """
    if mint in _processing:
        log.debug(f"Already processing mint {mint[:8]}... — skip.")
        return
    _processing.add(mint)

    log.info(f"⚡ SIGNAL received | From: {wallet[:8]}... | Token: {mint[:8]}...")

    try:
        # --- STEP 1: Rug-pull check (concurrent internal calls) ---
        is_safe, reason = await rug_checker.is_safe(mint)
        if not is_safe:
            log.warning(f"🚫 RUG CHECK FAILED | Mint: {mint[:8]}... | Reason: {reason}")
            _processing.discard(mint)
            return
        log.info(f"🛡️  Rug check passed | Mint: {mint[:8]}...")

        # --- STEP 2: Get quote and build swap tx ---
        result = await jupiter.execute_swap(
            input_mint=cfg.SOL_MINT,
            output_mint=mint,
            amount_lamports=cfg.BUY_AMOUNT_LAMPORTS,
            slippage_bps=cfg.SLIPPAGE_BPS_ENTRY,
            use_jito=True
        )
        if not result:
            log.error(f"Jupiter swap build failed for {mint[:8]}...")
            _processing.discard(mint)
            return

        signed_bytes, quote = result
        out_amount = int(quote.get("outAmount", 0))
        in_amount = cfg.BUY_AMOUNT_LAMPORTS

        # --- STEP 3: Submit via Jito bundle ---
        bundle_id = await jito.send_bundle(signed_bytes)
        if not bundle_id:
            log.error(f"Jito bundle submission failed for {mint[:8]}...")
            _processing.discard(mint)
            return

        log.info(
            f"🟢 BUY SENT | Mint: {mint[:8]}... | "
            f"In: {in_amount/1e9:.3f} SOL | "
            f"Out: {out_amount} tokens | "
            f"Bundle: {bundle_id[:12]}..."
        )

        # --- STEP 4: Open position for monitoring ---
        # Estimate entry price: SOL amount in USD / token amount
        # In production: fetch SOL/USD price from Pyth oracle or Jupiter Price API
        sol_price_usd = await _get_sol_price()
        entry_price_usd = (in_amount / 1e9 * sol_price_usd) / max(out_amount, 1)

        position_manager.open_position(
            mint=mint,
            entry_price=entry_price_usd,
            token_amount=float(out_amount)
        )

    except Exception as e:
        log.error(f"Signal handler exception for {mint[:8]}: {e}", exc_info=True)
        _processing.discard(mint)

async def _get_sol_price() -> float:
    """Quick SOL/USD price fetch from Jupiter Price API."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                "https://price.jup.ag/v4/price",
                params={"ids": cfg.SOL_MINT}
            )
            data = r.json().get("data", {})
            return data.get(cfg.SOL_MINT, {}).get("price", 150.0)
    except Exception:
        return 150.0  # Fallback

async def main():
    log.info("=" * 60)
    log.info("  SOLANA MEMECOIN SNIPER — STARTING")
    log.info(f"  Wallet: {str(keypair.pubkey())[:16]}...")
    log.info(f"  Buy amount: {cfg.BUY_AMOUNT_SOL} SOL per trade")
    log.info(f"  Tracking {len(cfg.SMART_MONEY_WALLETS)} smart money wallets")
    log.info("=" * 60)

    tracker = WalletTracker(rpc=rpc, on_signal=on_signal)

    # Start WebSocket connection + wallet subscriptions concurrently
    await asyncio.gather(
        rpc.connect(),     # Persistent WS loop (reconnects internally)
        tracker.start(),   # Subscribes all smart money wallets
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down...")