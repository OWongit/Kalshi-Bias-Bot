"""Load bot configuration from environment (and optional .env file)."""
import os
from pathlib import Path

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default


def _int(key: str, default: int) -> int:
    v = os.environ.get(key)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v.strip())
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes")


def load_config() -> dict:
    """Load configuration from environment. All keys are present with defaults."""
    base_url = _str(
        "KALSHI_BASE_URL",
        "https://demo-api.kalshi.co/trade-api/v2",
    )
    # Ensure no trailing slash for consistent path building
    if base_url.endswith("/"):
        base_url = base_url.rstrip("/")

    return {
        "base_url": base_url,
        "api_key_id": _str("KALSHI_API_KEY_ID", ""),
        "private_key_path": _str("KALSHI_PRIVATE_KEY_PATH", ""),
        "private_key_pem": _str("KALSHI_PRIVATE_KEY", ""),
        "entry_min_price_cents": _int("ENTRY_MIN_PRICE_CENTS", 95),
        "max_spread_cents": _int("MAX_SPREAD_CENTS", 1),
        "stop_loss_cents": _int("STOP_LOSS_CENTS", 70),
        "max_pct_per_market": _int("MAX_PCT_PER_MARKET", 10),
        "max_open_positions": _int("MAX_OPEN_POSITIONS", 10),
        "stop_loss_poll_seconds": _int("STOP_LOSS_POLL_SECONDS", 30),
        "dry_run": _bool("DRY_RUN", False),
        "discovery_categories_file": _str("DISCOVERY_CATEGORIES_FILE", "discovery_categories.txt"),
        "min_contracts_per_order": 1,
        "max_contracts_per_order": 10_000,
    }


def get_private_key(config: dict) -> str:
    """Resolve private key PEM: from file path or from config string."""
    path = (config.get("private_key_path") or "").strip()
    if path and Path(path).expanduser().is_file():
        return Path(path).expanduser().read_text()
    pem = (config.get("private_key_pem") or "").strip()
    if pem:
        # Allow literal \n in env
        return pem.replace("\\n", "\n")
    return ""
