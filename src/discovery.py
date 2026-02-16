"""Discover open markets on Kalshi by category slugs or series tickers."""
import logging
from pathlib import Path
from typing import List, Tuple

import requests

logger = logging.getLogger(__name__)


def get_discovery_categories_from_file(file_path: str) -> List[str]:
    """
    Read category slugs from a text file (one slug per line).
    Blank lines and lines starting with # are ignored.
    Returns list of slugs (e.g. mens-college-basketball-mens-game).
    """
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    out: List[str] = []
    for line in lines:
        slug = line.split("#")[0].strip()
        if slug:
            out.append(slug)
    return out


def _get_json(url: str, params: dict = None, timeout: int = 30) -> dict:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_markets_by_series(base_url: str, series_ticker: str) -> List[Tuple[str, str]]:
    """
    Return all open, active markets for a given series (e.g. KXNCAAMBGAME).
    Uses GET /markets?series_ticker=...&status=open, paginated by cursor.
    """
    base_url = base_url.rstrip("/")
    series_ticker = (series_ticker or "").strip().upper()
    if not series_ticker:
        return []

    results: List[Tuple[str, str]] = []
    cursor = None
    for _ in range(50):
        params = {
            "series_ticker": series_ticker,
            "status": "open",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = _get_json(f"{base_url}/markets", params=params)
        except Exception as e:
            logger.warning("Discovery for series %s failed: %s", series_ticker, e)
            break
        for m in data.get("markets") or []:
            if m.get("status") and m.get("status") != "active":
                continue
            ticker = m.get("ticker")
            if ticker:
                event_ticker = m.get("event_ticker") or ""
                results.append((ticker, event_ticker))
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    return results


def _series_matches_slug(series: dict, slug: str) -> bool:
    """True if series category, tags, or title match the slug (with dashes as spaces)."""
    slug_norm = (slug or "").strip().lower().replace("-", " ")
    if not slug_norm:
        return False
    cat = (series.get("category") or "").strip().lower().replace("-", " ")
    # Exact match or slug contained in category
    if slug_norm in cat or slug.strip().lower() == (series.get("category") or "").strip().lower():
        return True
    # Category (e.g. "Mens College Basketball") contained in slug ("mens college basketball mens game")
    if cat and cat in slug_norm:
        return True
    tags = [str(t).lower().replace("-", " ") for t in (series.get("tags") or [])]
    if slug_norm in tags or slug.strip().lower() in [str(t).lower() for t in (series.get("tags") or [])]:
        return True
    title = (series.get("title") or "").lower().replace("-", " ")
    if slug_norm in title or (cat and cat in title):
        return True
    return False


def _slug_to_title_case(slug: str) -> str:
    """Convert mens-college-basketball-mens-game -> Mens College Basketball Mens Game."""
    return (slug or "").replace("-", " ").title()


def get_markets_by_category_slug(base_url: str, slug: str) -> List[Tuple[str, str]]:
    """
    Return all open, active markets for a slug (category slug or series ticker).
    If slug is a series ticker (e.g. kxncaambgame), uses GET /markets?series_ticker=... directly.
    Else treats as category slug: GET /series (filter by category/title), then GET /markets per series.
    """
    base_url = base_url.rstrip("/")
    slug = (slug or "").strip()
    if not slug:
        return []

    # Fast path: slug may be a series ticker (e.g. kxncaambgame). GET /markets?series_ticker=... returns markets directly.
    direct = get_markets_by_series(base_url, slug)
    if direct:
        return direct

    series_tickers: List[str] = []

    # (1) GET /series?category=<slug> — try URL slug then title-case (e.g. "Mens College Basketball Mens Game")
    for category_value in (slug, _slug_to_title_case(slug)):
        try:
            data = _get_json(f"{base_url}/series", params={"category": category_value, "limit": 200})
            for s in data.get("series") or []:
                t = s.get("ticker")
                if t and t not in series_tickers:
                    series_tickers.append(t)
            if series_tickers:
                break
        except Exception as e:
            logger.debug("GET /series?category=%s failed: %s", category_value, e)

    # (2) If no series from (1), GET /series paginated and filter client-side
    if not series_tickers:
        logger.debug(
            "Discovery: GET /series?category=<slug> returned no series for %r; fetching all series and filtering by slug.",
            slug,
        )
        cursor = None
        for _ in range(50):
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                data = _get_json(f"{base_url}/series", params=params)
            except Exception as e:
                logger.warning("Discovery for category slug %s (series list) failed: %s", slug, e)
                break
            raw_series = data.get("series") or []
            for s in raw_series:
                if _series_matches_slug(s, slug):
                    t = s.get("ticker")
                    if t and t not in series_tickers:
                        series_tickers.append(t)
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        if not series_tickers:
            logger.debug(
                "Discovery: no series matched slug %r after client-side filter. If using demo API, try production base_url for more markets.",
                slug,
            )

    results: List[Tuple[str, str]] = []
    for st in series_tickers:
        results.extend(get_markets_by_series(base_url, st))

    # Dedupe by market ticker
    seen = set()
    unique: List[Tuple[str, str]] = []
    for ticker, ev in results:
        if ticker not in seen:
            seen.add(ticker)
            unique.append((ticker, ev))
    return unique


def get_markets_by_discovery_categories(base_url: str, slugs: List[str]) -> List[Tuple[str, str]]:
    """For each category slug, discover markets; merge and dedupe by market ticker."""
    seen = set()
    results: List[Tuple[str, str]] = []
    for slug in slugs:
        for ticker, ev in get_markets_by_category_slug(base_url, slug):
            if ticker not in seen:
                seen.add(ticker)
                results.append((ticker, ev))
    return results
