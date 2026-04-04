"""
Position Manager — Moonbag Strategy.

On buy confirmation:
  - Records entry price and token amount.
  - Spawns an async monitoring task per position.

Exit logic:
  - Phase 1 (TP): When price hits 2x entry → sell 50% (recoup full investment)
  - Phase 2 (Moonbag): Trail the remaining 50% with a 25% trailing stop
    - Peak is updated every price tick
    - If price drops 25% from peak → sell remainder
"""

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import httpx
from config import cfg
from utils.logger import get_logger

log = get_logger("position_manager")

JUPITER_PRICE_URL = "https://price.jup.ag/v4/price"

@dataclass
class Position:
    mint: str
    entry_price_usd: float
    token_amount: float          # Total tokens bought
    remaining_amount: float      # Tokens still held
    phase: int = 1               # 1 = watching for TP, 2 = trailing stop
    peak_price: float = 0.0
    tp_triggered: bool = False

class PositionManager:
    def __init__(self, on_sell: Callable[[str, float, int], Awaitable[None]]):
        """
        on_sell(mint, amount, slippage_bps) → called when exit signal fires.
        """
        self.on_sell = on_sell
        self._positions: dict[str, Position] = {}
        self._http = httpx.AsyncClient(timeout=5.0)

    def open_position(self, mint: str, entry_price: float, token_amount: float):
        """Register a new open position and start monitoring."""
        pos = Position(
            mint=mint,
            entry_price_usd=entry_price,
            token_amount=token_amount,
            remaining_amount=token_amount,
            peak_price=entry_price,
        )
        self._positions[mint] = pos
        log.info(
            f"📂 Position opened | Mint: {mint[:8]}... | "
            f"Entry: ${entry_price:.6f} | Amount: {token_amount:.2f}"
        )
        asyncio.create_task(self._monitor(mint))

    async def _monitor(self, mint: str):
        """
        Price monitoring loop for a single position.
        Polls Jupiter Price API every 2 seconds.
        """
        while mint in self._positions:
            try:
                price = await self._get_price(mint)
                if price is None:
                    await asyncio.sleep(2)
                    continue

                pos = self._positions[mint]
                await self._evaluate(pos, price)

                if mint not in self._positions:
                    break  # Position was fully closed

            except Exception as e:
                log.error(f"Monitor error for {mint[:8]}: {e}")

            await asyncio.sleep(2)

    async def _evaluate(self, pos: Position, current_price: float):
        """Evaluate exit conditions based on current price."""
        mint = pos.mint
        multiplier = current_price / pos.entry_price_usd

        # --- Phase 1: Take Profit at 2x ---
        if pos.phase == 1 and multiplier >= cfg.TAKE_PROFIT_MULTIPLIER:
            half = pos.token_amount / 2
            log.info(
                f"🎉 TP HIT | Mint: {mint[:8]}... | "
                f"{multiplier:.2f}x | Selling 50% ({half:.2f} tokens)"
            )
            await self.on_sell(mint, half, cfg.SLIPPAGE_BPS_EXIT)
            pos.remaining_amount = half
            pos.phase = 2
            pos.peak_price = current_price

        # --- Phase 2: Trailing Stop on Moonbag ---
        elif pos.phase == 2:
            if current_price > pos.peak_price:
                pos.peak_price = current_price
                log.debug(f"📈 New peak for {mint[:8]}: ${pos.peak_price:.6f}")

            drop_from_peak = (pos.peak_price - current_price) / pos.peak_price
            if drop_from_peak >= cfg.TRAILING_STOP_PCT:
                log.info(
                    f"🛑 TRAILING STOP | Mint: {mint[:8]}... | "
                    f"Dropped {drop_from_peak*100:.1f}% from peak | "
                    f"Selling {pos.remaining_amount:.2f} tokens"
                )
                await self.on_sell(mint, pos.remaining_amount, cfg.SLIPPAGE_BPS_EXIT)
                del self._positions[mint]

    async def _get_price(self, mint: str) -> float | None:
        """Fetch USD price from Jupiter Price API."""
        try:
            r = await self._http.get(
                JUPITER_PRICE_URL,
                params={"ids": mint}
            )
            r.raise_for_status()
            data = r.json().get("data", {})
            return data.get(mint, {}).get("price")
        except Exception:
            return None

    async def close(self):
        await self._http.aclose()