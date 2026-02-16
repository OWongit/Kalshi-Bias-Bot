"""
Kalshi Trading Bot: discover markets, buy YES and NO at the ask when bid >= entry_min and spread <= max_spread, stop loss for both sides.
Configure via env (see .env.example). Run with: python main.py
"""
import logging
import sys
import time

import requests

from src.config import load_config, get_private_key
from src.auth import build_client
from src.discovery import (
    get_discovery_categories_from_file,
    get_markets_by_discovery_categories,
)
from src.balance_sizing import get_balance, compute_bet_sizes
from src.orders import place_buy_yes, place_buy_no
from src.stop_loss import get_market_bid_ask_batch, get_positions, run_stop_loss

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    config = load_config()
    if not config.get("api_key_id") or not get_private_key(config):
        logger.error("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PATH / KALSHI_PRIVATE_KEY. See .env.example.")
        sys.exit(1)

    client = build_client(config)
    if not client:
        logger.error("Could not build API client. Check credentials.")
        sys.exit(1)

    base_url = config["base_url"]
    entry_min_cents = config["entry_min_price_cents"]
    max_spread_cents = config.get("max_spread_cents", 1)
    stop_cents = config["stop_loss_cents"]
    max_pct = config["max_pct_per_market"]
    max_positions = config["max_open_positions"]
    poll_seconds = config["stop_loss_poll_seconds"]
    dry_run = config["dry_run"]
    min_contracts = config.get("min_contracts_per_order", 1)
    max_contracts = config.get("max_contracts_per_order", 10_000)

    logger.info(
        "Bot started: entry_min=%sc max_spread=%sc stop=%sc max_pct=%s max_positions=%s dry_run=%s (YES and NO)",
        entry_min_cents, max_spread_cents, stop_cents, max_pct, max_positions, dry_run,
    )

    try:
        get_balance(client)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            is_demo = "demo-api.kalshi.co" in base_url
            env_name = "DEMO (demo.kalshi.com)" if is_demo else "PRODUCTION (kalshi.com)"
            logger.error("401 Unauthorized: Kalshi rejected your API key. You are using %s.", env_name)
            sys.exit(1)
        raise

    while True:
        try:
            categories_file = (config.get("discovery_categories_file") or "").strip()
            slugs = get_discovery_categories_from_file(categories_file) if categories_file else []
            candidates: list = []
            if slugs:
                candidates = get_markets_by_discovery_categories(base_url, slugs)
                if candidates:
                    logger.info("Discovered %s market(s) from categories: %s", len(candidates), ", ".join(slugs))
            if not candidates:
                logger.info("No markets found this cycle.")

            try:
                yes_positions, no_positions = get_positions(client)
            except Exception as e:
                logger.warning("Could not fetch positions: %s", e)
                yes_positions = []
                no_positions = []

            position_tickers_yes = [t for t, _ in yes_positions]
            position_tickers_no = [t for t, _ in no_positions]

            try:
                balance_cents = get_balance(client)
            except Exception as e:
                logger.warning("Could not fetch balance: %s", e)
                balance_cents = 0

            to_open_yes: list = []
            to_open_no: list = []
            bid_ask: dict = {}

            if candidates and balance_cents > 0:
                # Fetch bid/ask for ALL candidates first (like test.py), then filter by bid >= 95 and spread
                all_tickers = [t for t, _ in candidates]
                bid_ask = get_market_bid_ask_batch(client, all_tickers)

                yes_qualified = []
                no_qualified = []
                for (ticker, event_ticker) in candidates:
                    yb, ya, nb, na = bid_ask.get(ticker, (0, 0, 0, 0))
                    if yb >= entry_min_cents and ya and (ya - yb) <= max_spread_cents:
                        yes_qualified.append((ticker, event_ticker))
                    if nb >= entry_min_cents and na and (na - nb) <= max_spread_cents:
                        no_qualified.append((ticker, event_ticker))

                to_open_yes = compute_bet_sizes(
                    balance_cents=balance_cents,
                    candidate_markets=yes_qualified,
                    current_position_tickers=position_tickers_yes,
                    entry_price_cents=entry_min_cents,
                    max_pct_per_market=max_pct,
                    max_open_positions=max_positions,
                    min_contracts=min_contracts,
                    max_contracts=max_contracts,
                )
                to_open_no = compute_bet_sizes(
                    balance_cents=balance_cents,
                    candidate_markets=no_qualified,
                    current_position_tickers=position_tickers_no,
                    entry_price_cents=entry_min_cents,
                    max_pct_per_market=max_pct,
                    max_open_positions=max_positions,
                    min_contracts=min_contracts,
                    max_contracts=max_contracts,
                )

            for ticker, event_ticker, count in to_open_yes:
                yb, ya, nb, na = bid_ask.get(ticker, (0, 0, 0, 0))
                spread = (ya - yb) if (yb and ya) else 999
                if yb < entry_min_cents:
                    continue
                if spread > max_spread_cents:
                    continue
                if not ya:
                    continue
                if ya >= 100:
                    continue  # skip: exchange rejects limit price 100; don't place at capped 99
                print(f"Market above {entry_min_cents}c (YES): {ticker} bid={yb}c ask={ya}c spread={ya - yb}c")
                try:
                    place_buy_yes(client, ticker, count, min(ya, 99), dry_run=dry_run)
                except Exception as e:
                    logger.exception("Place YES order failed ticker=%s: %s", ticker, e)
                if not dry_run:
                    time.sleep(0.5)

            for ticker, event_ticker, count in to_open_no:
                yb, ya, nb, na = bid_ask.get(ticker, (0, 0, 0, 0))
                spread = (na - nb) if (nb and na) else 999
                if nb < entry_min_cents:
                    continue
                if spread > max_spread_cents:
                    continue
                if not na:
                    continue
                if na >= 100:
                    continue  # skip: exchange rejects limit price 100; don't place at capped 99
                print(f"Market above {entry_min_cents}c (NO): {ticker} bid={nb}c ask={na}c spread={na - nb}c")
                try:
                    place_buy_no(client, ticker, count, min(na, 99), dry_run=dry_run)
                except Exception as e:
                    logger.exception("Place NO order failed ticker=%s: %s", ticker, e)
                if not dry_run:
                    time.sleep(0.5)

            try:
                run_stop_loss(client, stop_loss_cents=stop_cents, dry_run=dry_run)
            except Exception as e:
                logger.exception("Stop-loss monitor failed: %s", e)

        except KeyboardInterrupt:
            logger.info("Shutting down")
            break
        except Exception as e:
            logger.exception("Loop error: %s", e)

        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
