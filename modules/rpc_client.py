"""
Helius WebSocket RPC engine.
Handles subscriptions and raw JSON-RPC over persistent WS connections.
Uses connection pooling and auto-reconnect with exponential backoff.
"""

import asyncio
import json
import websockets
from websockets.exceptions import ConnectionClosedError
from typing import Callable, Awaitable
from config import cfg
from utils.logger import get_logger

log = get_logger("rpc_client")

class SolanaRPCClient:
    def __init__(self):
        self._ws = None
        self._sub_id_map: dict[int, Callable] = {}
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def connect(self):
        """Establish WebSocket connection with auto-reconnect."""
        backoff = 1
        while True:
            try:
                log.info(f"Connecting to Helius WS: {cfg.RPC_WS[:50]}...")
                self._ws = await websockets.connect(
                    cfg.RPC_WS,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=10 * 1024 * 1024  # 10MB for large account data
                )
                log.info("WebSocket connected.")
                backoff = 1
                await self._listen()
            except (ConnectionClosedError, OSError) as e:
                log.warning(f"WS disconnected: {e}. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _listen(self):
        """Main message pump — dispatches to pending futures or subscription handlers."""
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
                # Response to an RPC call
                if "id" in msg and msg["id"] in self._pending:
                    self._pending.pop(msg["id"]).set_result(msg)
                # Subscription notification
                elif "method" in msg and msg["method"].endswith("Notification"):
                    sub_id = msg["params"]["subscription"]
                    handler = self._sub_id_map.get(sub_id)
                    if handler:
                        asyncio.create_task(handler(msg["params"]["result"]))
            except Exception as e:
                log.error(f"Message parse error: {e}")

    async def _send(self, method: str, params: list) -> dict:
        """Send a JSON-RPC request and await its response."""
        req_id = self._next_id()
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params
        })
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        await self._ws.send(payload)
        return await asyncio.wait_for(future, timeout=15.0)

    async def subscribe_logs(
        self,
        address: str,
        handler: Callable[[dict], Awaitable[None]]
    ) -> int:
        """Subscribe to logs mentioning a specific address."""
        resp = await self._send("logsSubscribe", [
            {"mentions": [address]},
            {"commitment": "processed"}  # fastest commitment level
        ])
        sub_id = resp["result"]
        self._sub_id_map[sub_id] = handler
        log.info(f"Subscribed to logs for {address[:8]}... (sub_id={sub_id})")
        return sub_id

    async def subscribe_account(
        self,
        address: str,
        handler: Callable[[dict], Awaitable[None]]
    ) -> int:
        """Subscribe to account data changes."""
        resp = await self._send("accountSubscribe", [
            address,
            {"encoding": "jsonParsed", "commitment": "processed"}
        ])
        sub_id = resp["result"]
        self._sub_id_map[sub_id] = handler
        return sub_id

    async def get_account_info(self, address: str) -> dict | None:
        """HTTP-equivalent account fetch over WS."""
        resp = await self._send("getAccountInfo", [
            address,
            {"encoding": "jsonParsed", "commitment": "confirmed"}
        ])
        return resp.get("result", {}).get("value")

    async def get_token_accounts_by_owner(self, owner: str, mint: str) -> list:
        resp = await self._send("getTokenAccountsByOwner", [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed", "commitment": "confirmed"}
        ])
        return resp.get("result", {}).get("value", [])

    async def unsubscribe(self, sub_id: int, kind: str = "logs"):
        method = f"{kind}Unsubscribe"
        await self._send(method, [sub_id])
        self._sub_id_map.pop(sub_id, None)