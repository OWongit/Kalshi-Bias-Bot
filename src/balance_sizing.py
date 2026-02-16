"""Balance fetch and bet sizing: contract counts per market from available balance."""
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)


def get_balance(client) -> int:
    """Return available balance in cents. Raises on API error."""
    r = client.get("/portfolio/balance")
    r.raise_for_status()
    data = r.json()
    return int(data.get("balance", 0))


def compute_bet_sizes(
    balance_cents: int,
    candidate_markets: List[Tuple[str, str]],
    current_position_tickers: List[str],
    entry_price_cents: int,
    max_pct_per_market: int,
    max_open_positions: int,
    min_contracts: int = 1,
    max_contracts: int = 10_000,
) -> List[Tuple[str, str, int]]:
    """
    Given balance and candidate (market_ticker, event_ticker), return list of
    (market_ticker, event_ticker, contracts) for markets we should place orders in.
    Skips markets we already have a position in; caps at max_open_positions.
    """
    # Markets we already have a position in
    already_in = set(current_position_tickers or [])

    # How many new positions we can open
    current_count = len(already_in)
    slots = max(0, max_open_positions - current_count)
    if slots == 0:
        return []

    # Per-market contract count from balance
    # contracts = floor((balance_cents * max_pct_per_market / 100) / entry_price_cents)
    if entry_price_cents <= 0:
        return []
    notional_per_contract_cents = entry_price_cents
    allocation_cents = balance_cents * max_pct_per_market // 100
    contracts_per_market = max(0, allocation_cents // notional_per_contract_cents)
    contracts_per_market = max(min_contracts, min(max_contracts, contracts_per_market))

    if contracts_per_market < min_contracts:
        return []

    out: List[Tuple[str, str, int]] = []
    for ticker, event_ticker in candidate_markets:
        if ticker in already_in:
            continue
        if len(out) >= slots:
            break
        out.append((ticker, event_ticker, contracts_per_market))
        already_in.add(ticker)

    return out
