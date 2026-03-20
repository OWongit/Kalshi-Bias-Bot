"""
Market discovery: resolve category slugs / series tickers into a list of
tradable (ticker, event_ticker, params) tuples.
"""

import logging

import config

log = logging.getLogger(__name__)


def is_series_ticker(slug: str) -> bool:
    """Heuristic: series tickers are alphanumeric (no dashes), while category
    slugs use dashes (e.g. 'mens-college-basketball-mens-game')."""
    return "-" not in slug


def discover_series_for_slug(client, slug: str) -> list[str]:
    """Return a list of series tickers that match *slug*.

    If the slug looks like a series ticker itself, return it directly.
    Otherwise try the category filter on the /series endpoint, falling back
    to client-side filtering over all series.
    """
    if is_series_ticker(slug):
        log.info("Slug '%s' treated as series ticker directly", slug)
        return [slug]

    # Try exact category match
    series = client.get_series_list(category=slug)
    if series:
        tickers = [s["ticker"] for s in series]
        log.info("Category '%s' matched %d series via API", slug, len(tickers))
        return tickers

    # Try title-cased variant (e.g. "Mens-College-Basketball-Mens-Game")
    title_slug = slug.replace("-", " ").title().replace(" ", "-")
    series = client.get_series_list(category=title_slug)
    if series:
        tickers = [s["ticker"] for s in series]
        log.info("Category '%s' (title-cased) matched %d series", slug, len(tickers))
        return tickers

    # Fall back: fetch all series and filter client-side
    log.info("No API match for '%s'; scanning all series client-side", slug)
    all_series = client.get_series_list()
    needle = slug.replace("-", " ").lower()
    matched: list[str] = []
    for s in all_series:
        haystack = " ".join([
            s.get("category", ""),
            s.get("title", ""),
            " ".join(s.get("tags", [])),
        ]).lower()
        if needle in haystack:
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
