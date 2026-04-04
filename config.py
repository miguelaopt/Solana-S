import os
from dotenv import load_dotenv
from solders.keypair import Keypair
import base58

load_dotenv()

def load_keypair() -> Keypair:
    """Load Phantom wallet keypair from base58 private key."""
    raw = os.getenv("WALLET_PRIVATE_KEY")
    if not raw:
        raise ValueError("WALLET_PRIVATE_KEY not set in .env")
    decoded = base58.b58decode(raw)
    return Keypair.from_bytes(decoded)

class Config:
    # RPC
    RPC_HTTP: str = os.getenv("HELIUS_RPC_HTTP")
    RPC_WS: str = os.getenv("HELIUS_RPC_WS")

    # Jito MEV Protection
    JITO_URL: str = os.getenv("JITO_BLOCK_ENGINE_URL")
    JITO_TIP_LAMPORTS: int = int(os.getenv("JITO_TIP_LAMPORTS", 100000))

    # Trade Parameters
    BUY_AMOUNT_SOL: float = float(os.getenv("BUY_AMOUNT_SOL", 0.1))
    BUY_AMOUNT_LAMPORTS: int = int(BUY_AMOUNT_SOL * 1_000_000_000)
    SLIPPAGE_BPS_ENTRY: int = int(os.getenv("SLIPPAGE_BPS_ENTRY", 1000))  # 10%
    SLIPPAGE_BPS_EXIT: int = int(os.getenv("SLIPPAGE_BPS_EXIT", 2000))    # 20%

    # Risk Management
    TAKE_PROFIT_MULTIPLIER: float = float(os.getenv("TAKE_PROFIT_MULTIPLIER", 2.0))
    TRAILING_STOP_PCT: float = float(os.getenv("TRAILING_STOP_PCT", 0.25))

    # Smart Money
    SMART_MONEY_WALLETS: list[str] = os.getenv("SMART_MONEY_WALLETS", "").split(",")

    # Known Program IDs
    RAYDIUM_AMM_V4: str = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
    PUMP_FUN_PROGRAM: str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    TOKEN_PROGRAM: str = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
    SOL_MINT: str = "So11111111111111111111111111111111111111112"
    WSOL_MINT: str = "So11111111111111111111111111111111111111112"

cfg = Config()
keypair = load_keypair()