"""
Kalshi Trading Bot Configuration

Edit the values below to match your Kalshi account and trading preferences.
"""

import os
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# ---------------------------------------------------------------------------
# API Credentials
# ---------------------------------------------------------------------------
API_KEY_ID = "05a2e7ab-90c5-44f5-97fa-ba5af9af7e55"

# Path to your .key / .pem private key file (preferred over PRIVATE_KEY_STRING)
PRIVATE_KEY_PATH = "private_key.pem"

# Fallback: PEM string (supports literal \n).  Only used when PRIVATE_KEY_PATH
# does not point to an existing file.
PRIVATE_KEY_STRING = ""

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
BASE_URL = "https://api.elections.kalshi.com"

# ---------------------------------------------------------------------------
# Trading Parameters (global fallbacks; per-category overrides in categories.json)
# ---------------------------------------------------------------------------
MAX_OPEN_POSITIONS = 100        # Max concurrent markets with an open NO position
MAX_PCT_PER_MARKET = 0.02       # Fraction of balance allocated per new market
MIN_CONTRACTS = 1               # Minimum contracts per order
MAX_CONTRACTS = 10_000          # Maximum contracts per order

STOP_LOSS_POLL_SECONDS =   0.10     # Sleep between main-loop iterations
DRY_RUN = False                  # True = log orders without sending them

# ----------------------------------------------------------------  -----------
# Discovery
# ---------------------------------------------------------------------------
CATEGORIES_FILE = "categories.json"  # JSON with categories and per-category params


# ---------------------------------------------------------------------------
# Categories config loader
# ---------------------------------------------------------------------------
def load_categories_config(filepath: str) -> dict:
    """Load categories.json and return config with merged defaults per category.

    Returns dict with:
      - "defaults": dict of default params
      - "categories": list of category dicts, each with slug + merged params
    """
    import json
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)
    defaults = data.get("defaults", {})
    _normalize_entry_price(defaults)
    categories = []
    for cat in data.get("categories", []):
        merged = {**defaults, **{k: v for k, v in cat.items() if k != "slug"}}
        merged["slug"] = cat.get("slug", "")
        _normalize_entry_price(merged)
        categories.append(merged)
    return {"defaults": defaults, "categories": categories}


def _normalize_entry_price(params: dict) -> None:
    """Ensure entry_price is always a list of ints (supports single int or list in JSON)."""
    ep = params.get("entry_price")
    if ep is None:
        return
    if isinstance(ep, (int, float)):
        params["entry_price"] = [int(ep)]
    elif isinstance(ep, list):
        params["entry_price"] = [int(v) for v in ep]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def load_private_key():
    """Load RSA private key from file or string."""
    if os.path.isfile(PRIVATE_KEY_PATH):
        with open(PRIVATE_KEY_PATH, "rb") as f:
            return serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    pem_text = PRIVATE_KEY_STRING
    if not pem_text:
        raise FileNotFoundError(
            f"No private key found: '{PRIVATE_KEY_PATH}' does not exist and "
            "PRIVATE_KEY_STRING is empty."
        )
    pem_text = pem_text.replace("\\n", "\n")
    return serialization.load_pem_private_key(
        pem_text.encode("utf-8"), password=None, backend=default_backend()
    )
