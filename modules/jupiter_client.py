"""
Jupiter v6 Aggregator — Swap Executor.
Fetches optimal routes and builds/submits swap transactions.
Uses dynamic slippage based on trade direction (entry vs. exit).
"""

import asyncio
import base64
import httpx
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from config import cfg, keypair
from utils.logger import get_logger

log = get_logger("jupiter")

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL  = "https://quote-api.jup.ag/v6/swap"

class JupiterClient:
    def __init__(self):
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=20)
        )
        self.wallet_pubkey = str(keypair.pubkey())

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int
    ) -> dict | None:
        """Fetch best route quote from Jupiter."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "onlyDirectRoutes": False,      # Allow multi-hop for obscure memecoins
            "asLegacyTransaction": False,   # Use versioned transactions
        }
        try:
            r = await self._http.get(JUPITER_QUOTE_URL, params=params)
            r.raise_for_status()
            quote = r.json()
            log.debug(
                f"Quote: {amount} {input_mint[:6]} → "
                f"{quote.get('outAmount')} {output_mint[:6]} | "
                f"Price impact: {quote.get('priceImpactPct')}%"
            )
            return quote
        except Exception as e:
            log.error(f"Quote fetch failed: {e}")
            return None

    async def build_swap_transaction(
        self,
        quote: dict,
        use_jito: bool = True
    ) -> VersionedTransaction | None:
        """
        Build the swap transaction from a Jupiter quote.
        
        When use_jito=True, we request priority fee info but Jito bundle
        wrapping is handled separately in jito_client.py.
        """
        payload = {
            "quoteResponse": quote,
            "userPublicKey": self.wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": "auto",  # Jupiter auto-sets CU price
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto" if not use_jito else 0,
        }
        try:
            r = await self._http.post(JUPITER_SWAP_URL, json=payload)
            r.raise_for_status()
            data = r.json()
            tx_b64 = data["swapTransaction"]
            tx_bytes = base64.b64decode(tx_b64)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            return tx
        except Exception as e:
            log.error(f"Swap tx build failed: {e}")
            return None

    async def sign_and_serialize(self, tx: VersionedTransaction) -> bytes:
        """Sign the transaction with our wallet keypair."""
        tx.sign([keypair])
        return bytes(tx)

    async def execute_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int,
        use_jito: bool = True
    ) -> str | None:
        """
        Full swap pipeline:
        quote → build tx → sign → (Jito bundle | direct submit)
        Returns transaction signature or None on failure.
        """
        quote = await self.get_quote(input_mint, output_mint, amount_lamports, slippage_bps)
        if not quote:
            return None

        # Sanity check: reject if price impact is catastrophic
        price_impact = float(quote.get("priceImpactPct", 0))
        if price_impact > 30.0:
            log.warning(f"Price impact {price_impact:.2f}% too high — aborting swap.")
            return None

        tx = await self.build_swap_transaction(quote, use_jito=use_jito)
        if not tx:
            return None

        signed_bytes = await self.sign_and_serialize(tx)
        return signed_bytes, quote  # Return to caller for Jito bundling or direct send

    async def close(self):
        await self._http.aclose()