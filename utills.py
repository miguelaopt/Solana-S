"""
utils.py — Shared utilities for the entire syndicate.
Handles: config I/O (thread-safe), color logging, SQLite wallet DB schema.
"""

import asyncio
import json
import logging
import os
import sqlite3
import aiofiles
import aiosqlite
import colorlog
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

# ─── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "data" / "wallet_history.db"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
(ROOT / "data").mkdir(exist_ok=True)


# ─── Color Logger ─────────────────────────────────────────────────────────────
def get_logger(name: str, log_file: str | None = None) -> logging.Logger:
    """
    Structured color logger.
    Terminal: color-coded by level.
    File: plain text with timestamps.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    # Color handler for terminal
    color_handler = colorlog.StreamHandler()
    color_handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s.%(msecs)03d%(reset)s "
        "%(bold_white)s|%(reset)s %(log_color)s%(levelname)-8s%(reset)s "
        "%(bold_white)s|%(reset)s %(cyan)s%(name)-18s%(reset)s "
        "%(bold_white)s|%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "white",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))
    logger.addHandler(color_handler)

    # File handler
    if log_file:
        fh = logging.FileHandler(LOG_DIR / log_file)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        ))
        logger.addHandler(fh)

    return logger


log = get_logger("utils")


# ─── Config I/O (Async, File-Locked) ──────────────────────────────────────────
_config_lock = asyncio.Lock()

async def read_config() -> dict:
    """Read config.json atomically."""
    async with _config_lock:
        async with aiofiles.open(CONFIG_PATH, "r") as f:
            return json.loads(await f.read())

async def write_config(data: dict) -> None:
    """Write config.json atomically (write to .tmp, then rename)."""
    async with _config_lock:
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        async with aiofiles.open(tmp, "w") as f:
            await f.write(json.dumps(data, indent=2))
        os.replace(tmp, CONFIG_PATH)

async def add_wallet_to_config(wallet: str) -> bool:
    """
    Append a wallet to TARGET_WALLETS if not already present.
    Returns True if added, False if already existed.
    """
    cfg = await read_config()
    if wallet in cfg["TARGET_WALLETS"] or wallet in cfg["PRUNED_WALLETS"]:
        return False
    cfg["TARGET_WALLETS"].append(wallet)
    cfg["meta"]["last_scraper_run"] = utc_now_iso()
    await write_config(cfg)
    log.info(f"✅ Added wallet to config: {wallet[:12]}...")
    return True

async def prune_wallet_from_config(wallet: str, reason: str = "") -> bool:
    """
    Move a wallet from TARGET_WALLETS to PRUNED_WALLETS.
    Returns True if pruned.
    """
    cfg = await read_config()
    if wallet not in cfg["TARGET_WALLETS"]:
        return False
    cfg["TARGET_WALLETS"].remove(wallet)
    if wallet not in cfg["PRUNED_WALLETS"]:
        cfg["PRUNED_WALLETS"].append(wallet)
    cfg["meta"]["last_prune_run"] = utc_now_iso()
    await write_config(cfg)
    log.warning(f"🗑️  Pruned wallet: {wallet[:12]}... | Reason: {reason}")
    return True

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def utc_now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


# ─── SQLite Wallet Performance DB ─────────────────────────────────────────────
async def init_db():
    """Create wallet history schema if it doesn't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS wallet_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet          TEXT NOT NULL,
                token_mint      TEXT NOT NULL,
                entry_price_usd REAL,
                exit_price_usd  REAL,
                roi_pct         REAL,
                is_win          INTEGER,  -- 1=win, 0=loss
                trade_ts        REAL,     -- unix timestamp
                source          TEXT      -- 'birdeye' | 'rpc'
            );

            CREATE INDEX IF NOT EXISTS idx_wallet ON wallet_trades(wallet);
            CREATE INDEX IF NOT EXISTS idx_ts     ON wallet_trades(trade_ts);

            CREATE TABLE IF NOT EXISTS wallet_meta (
                wallet          TEXT PRIMARY KEY,
                first_seen_ts   REAL,
                last_checked_ts REAL,
                tx_per_day_avg  REAL,
                unique_tokens   INTEGER,
                total_trades    INTEGER,
                win_rate_7d     REAL,
                win_rate_all    REAL,
                status          TEXT  -- 'active' | 'pruned' | 'candidate'
            );
        """)
        await db.commit()

async def upsert_wallet_meta(wallet: str, **kwargs):
    """Insert or update wallet metadata."""
    async with aiosqlite.connect(DB_PATH) as db:
        existing = await db.execute(
            "SELECT wallet FROM wallet_meta WHERE wallet=?", (wallet,)
        )
        row = await existing.fetchone()
        if row:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            vals = list(kwargs.values()) + [wallet]
            await db.execute(f"UPDATE wallet_meta SET {sets} WHERE wallet=?", vals)
        else:
            kwargs["wallet"] = wallet
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" * len(kwargs))
            await db.execute(
                f"INSERT INTO wallet_meta ({cols}) VALUES ({placeholders})",
                list(kwargs.values())
            )
        await db.commit()

async def insert_trade(wallet: str, token_mint: str, roi_pct: float,
                       entry_price: float, exit_price: float, trade_ts: float,
                       source: str = "birdeye"):
    """Record a completed trade for win-rate calculation."""
    is_win = 1 if roi_pct > 0 else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO wallet_trades
               (wallet, token_mint, roi_pct, entry_price_usd, exit_price_usd,
                is_win, trade_ts, source)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wallet, token_mint, roi_pct, entry_price, exit_price,
             is_win, trade_ts, source)
        )
        await db.commit()