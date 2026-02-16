"""Authenticated HTTP client for Kalshi API (requests + RSA-PSS signature)."""
import base64
import datetime
from typing import Any, Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend


# Path prefix used for signing (API expects path including this)
SIGNING_PATH_PREFIX = "/trade-api/v2"


def load_private_key(pem: str):
    """Load an RSA private key from PEM string."""
    return serialization.load_pem_private_key(
        pem.encode("utf-8") if isinstance(pem, str) else pem,
        password=None,
        backend=default_backend(),
    )


def create_signature(private_key, timestamp: str, method: str, path: str) -> str:
    """Create KALSHI-ACCESS-SIGNATURE: sign timestamp+method+path (path without query), RSA-PSS SHA256, base64."""
    path_without_query = path.split("?")[0]
    # API expects path to include /trade-api/v2 when we use base_url that ends with /trade-api/v2
    signing_path = path_without_query if path_without_query.startswith(SIGNING_PATH_PREFIX) else (SIGNING_PATH_PREFIX + path_without_query)
    message = f"{timestamp}{method}{signing_path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


class KalshiClient:
    """Minimal authenticated client for Kalshi REST API."""

    def __init__(self, base_url: str, api_key_id: str, private_key_pem: str):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self._private_key = load_private_key(private_key_pem)

    def _request(self, method: str, path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> requests.Response:
        # Path for URL: ensure leading slash
        request_path = path if path.startswith("/") else "/" + path
        url = self.base_url + request_path
        # Path for signing: must be /trade-api/v2 + request_path
        signing_path = SIGNING_PATH_PREFIX + request_path
        timestamp = str(int(datetime.datetime.now().timestamp() * 1000))
        signature = create_signature(self._private_key, timestamp, method, signing_path)

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }
        if json is not None:
            headers["Content-Type"] = "application/json"

        if method == "GET":
            return requests.get(url, headers=headers, params=params, timeout=30)
        if method == "POST":
            return requests.post(url, headers=headers, json=json, params=params, timeout=30)
        if method == "DELETE":
            return requests.delete(url, headers=headers, timeout=30)
        raise ValueError(f"Unsupported method: {method}")

    def get(self, path: str, params: Optional[dict] = None) -> requests.Response:
        return self._request("GET", path, params=params)

    def post(self, path: str, data: dict) -> requests.Response:
        return self._request("POST", path, json=data)

    def delete(self, path: str) -> requests.Response:
        return self._request("DELETE", path)


def build_client(config: dict) -> Optional["KalshiClient"]:
    """Build KalshiClient from config dict (from config.load_config()). Returns None if credentials missing."""
    from .config import get_private_key

    api_key_id = (config.get("api_key_id") or "").strip()
    private_key_pem = get_private_key(config)
    if not api_key_id or not private_key_pem:
        return None
    return KalshiClient(
        base_url=config["base_url"],
        api_key_id=api_key_id,
        private_key_pem=private_key_pem,
    )
