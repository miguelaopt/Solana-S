"""
Jito MEV Protection — Bundle Executor.

Jito bundles guarantee atomic execution of multiple transactions in a
specific order within a single block. This means:
  - No MEV bot can sandwich our trade (they can't insert txs before/after us)
  - All txs in the bundle succeed or all fail (atomic)

Architecture:
  Bundle = [tip_tx, our_swap_tx]
  - tip_tx: SOL transfer to a random Jito tip account
  - swap_tx: our Jupiter swap

The bundle is submitted to Jito's block engine, which forwards it to
validator clients running the Jito-Solana fork.
"""

import asyncio
import base64
import random
import httpx
from solders.transaction import VersionedTransaction
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from config import cfg, keypair
from utils.logger import get_logger

log = get_logger("jito")

# Jito tip accounts — randomly select one per bundle
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt13WkKuGt",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6iv",
]

class JitoClient:
    def __init__(self):
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0))

    def _get_tip_account(self) -> Pubkey:
        return Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))

    async def _build_tip_transaction(self, recent_blockhash: str) -> bytes:
        """Build a SOL tip transfer transaction to a Jito tip account."""
        tip_ix = transfer(TransferParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=self._get_tip_account(),
            lamports=cfg.JITO_TIP_LAMPORTS,
        ))
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=[tip_ix],
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash,
        )
        tx = VersionedTransaction(msg, [keypair])
        return base64.b64encode(bytes(tx)).decode()

    async def _get_recent_blockhash(self) -> str:
        """Fetch recent blockhash via HTTP RPC."""
        async with httpx.AsyncClient() as client:
            r = await client.post(cfg.RPC_HTTP, json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "confirmed"}]
            })
            r.raise_for_status()
            return r.json()["result"]["value"]["blockhash"]

    async def send_bundle(self, swap_tx_bytes: bytes) -> str | None:
        """
        Submit a Jito bundle: [tip_tx, swap_tx].
        
        Returns the bundle ID if accepted, None otherwise.
        """
        try:
            blockhash = await self._get_recent_blockhash()
            tip_tx_b64 = await self._build_tip_transaction(blockhash)
            swap_tx_b64 = base64.b64encode(swap_tx_bytes).decode()

            bundle_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [[tip_tx_b64, swap_tx_b64]]
            }

            r = await self._http.post(cfg.JITO_URL, json=bundle_payload)
            r.raise_for_status()
            result = r.json()

            if "result" in result:
                bundle_id = result["result"]
                log.info(f"✅ Jito bundle submitted | ID: {bundle_id[:16]}...")
                return bundle_id
            else:
                log.error(f"Jito bundle rejected: {result.get('error')}")
                return None

        except Exception as e:
            log.error(f"Jito bundle error: {e}")
            return None

    async def close(self):
        await self._http.aclose()