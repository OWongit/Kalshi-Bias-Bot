"""
Market discovery: resolve category slugs / series tickers into a list of
tradable (ticker, event_ticker, params) tuples.
"""

import logging
import re
from urllib.parse import urlparse

import config

log = logging.getLogger(__name__)


def _normalize_slug_input(slug: str) -> str:
    """Normalize raw user input, including Kalshi category URLs."""
    raw = slug.strip()
    if raw.lower().startswith(("http://", "https://")):
        parsed = urlparse(raw)
        path = parsed.path.strip("/")
        if path.lower().startswith("category/"):
            remainder = path[len("category/"):].strip("/")
            if remainder:
                return remainder
    return raw


def _slug_candidates(slug: str) -> list[str]:
    """Return likely series/category query variants for *slug*."""
    normalized = _normalize_slug_input(slug)
    if not normalized:
        return []

    parts = [p for p in re.split(r"[\\/]+", normalized.strip("/")) if p]
    candidates: list[str] = []
    if parts:
        leaf = parts[-1]
        joined_dash = "-".join(parts)
        joined_slash = "/".join(parts)
        candidates.extend([leaf, joined_dash, joined_slash, normalized])
    else:
        candidates.append(normalized)

    seen: set[str] = set()
    out: list[str] = []
    for cand in candidates:
        key = cand.lower()
        if key not in seen:
            seen.add(key)
            out.append(cand)
    return out


def _normalize_search_text(value: str) -> str:
    return re.sub(r"[-_/]+", " ", value).strip().lower()


def is_series_ticker(slug: str) -> bool:
    """Heuristic for Kalshi series tickers like ``KXETH15M``."""
    normalized = _normalize_slug_input(slug)
    return (
        bool(normalized)
        and "/" not in normalized
        and "-" not in normalized
        and normalized.isalnum()
        and normalized.upper().startswith("KX")
    )


def discover_series_for_slug(client, slug: str) -> list[str]:
    """Return a list of series tickers that match *slug*.

    If the slug looks like a series ticker itself, return it directly.
    Otherwise try the category filter on the /series endpoint, falling back
    to client-side filtering over all series.
    """
    normalized = _normalize_slug_input(slug)
    if is_series_ticker(normalized):
        ticker = normalized.upper()
        log.info("Slug '%s' treated as series ticker directly", slug)
        return [ticker]

    candidates = _slug_candidates(normalized)

    for cand in candidates:
        series = client.get_series_list(category=cand)
        if series:
            tickers = [s["ticker"] for s in series]
            log.info("Category '%s' matched %d series via API query '%s'", slug, len(tickers), cand)
            return tickers

        title_cand = cand.replace("-", " ").replace("/", " ").title().replace(" ", "-")
        if title_cand != cand:
            series = client.get_series_list(category=title_cand)
            if series:
                tickers = [s["ticker"] for s in series]
                log.info("Category '%s' matched %d series via title query '%s'", slug, len(tickers), title_cand)
                return tickers

    # Fall back: fetch all series and filter client-side
    log.info("No API match for '%s'; scanning all series client-side", slug)
    all_series = client.get_series_list()
    needles = [_normalize_search_text(c) for c in candidates]
    matched: list[str] = []
    for s in all_series:
        tags = s.get("tags", [])
        if isinstance(tags, str):
            tags_text = tags
        elif isinstance(tags, (list, tuple, set)):
            tags_text = " ".join(str(t) for t in tags if t is not None)
        else:
            tags_text = ""

        haystack = " ".join([
            str(s.get("category", "") or ""),
            str(s.get("title", "") or ""),
            tags_text,
        ])
        haystack_norm = _normalize_search_text(haystack)
        if any(n and n in haystack_norm for n in needles):
            matched.append(s["ticker"])
    log.info("Client-side scan matched %d series for '%s'", len(matched), slug)
    return matched


def discover_markets(
    client,
    series_ticker: str,
) -> list[tuple[str, str]]:
    """Paginate open markets for a series ticker and return
    (market_ticker, event_ticker) pairs."""
    markets: list[tuple[str, str]] = []
    cursor = ""
    while True:
        page, cursor = client.get_markets(
            series_ticker=series_ticker, status="open", cursor=cursor
        )
        for m in page:
            if m.get("status") not in ("active", "open", "initialized"):
                continue
            markets.append((m["ticker"], m["event_ticker"]))
        if not cursor:
            break
    return markets


def discover_all(
    client,
    categories_file: str,
) -> tuple[list[tuple[str, str, dict]], dict[str, dict]]:
    """Full discovery pipeline: load JSON config -> resolve series -> collect markets.

    Returns:
      - candidates: list of (ticker, event_ticker, params) with merged per-category params
      - series_to_params: dict mapping series_ticker -> params (for position lookup)
    """
    cfg = config.load_categories_config(categories_file)
    categories = cfg["categories"]
    defaults = cfg["defaults"]
    log.info("Loaded %d categories from %s", len(categories), categories_file)

    seen: set[str] = set()
    candidates: list[tuple[str, str, dict]] = []
    series_to_params: dict[str, dict] = {}

    for cat in categories:
        slug = cat.get("slug", "").strip()
        if not slug:
            continue
        slug_upper = slug.upper()
        series_tickers = discover_series_for_slug(client, slug_upper)
        for st in series_tickers:
            series_to_params[st] = cat
            pairs = discover_markets(client, st)
            for ticker, event_ticker in pairs:
                if ticker not in seen:
                    seen.add(ticker)
                    candidates.append((ticker, event_ticker, cat))

    log.info("Discovery complete: %d unique markets", len(candidates))
    return candidates, series_to_params
