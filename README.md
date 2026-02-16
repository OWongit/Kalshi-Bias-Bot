# Kalshi Trading Bot

Bot that discovers markets on Kalshi and applies the same entry and stop-loss logic to **both YES and NO**: places limit buy **YES** at the YES ask when YES bid ≥ 95¢ and YES spread ≤ 1¢, and limit buy **NO** at the NO ask when NO bid ≥ 95¢ and NO spread ≤ 1¢. Runs a 70¢ stop loss on both YES and NO positions. Bet sizing is based on account balance (configurable max % per market and max open positions per side).

## Setup

1. **Environment**: Copy `.env.example` to `.env` and set:

   - `KALSHI_API_KEY_ID` – your API key ID from Kalshi (Account & security → API Keys)
   - `KALSHI_PRIVATE_KEY_PATH` – path to your private key `.pem`/`.key` file, **or**
   - `KALSHI_PRIVATE_KEY` – PEM string (e.g. `"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"`)
   - `KALSHI_BASE_URL` – optional; default is demo: `https://demo-api.kalshi.co/trade-api/v2`

2. **Optional overrides** in `.env`:

   - `ENTRY_MIN_PRICE_CENTS=95` – only buy YES/NO when that side’s bid is at or above this; order is placed at that side’s ask (default 95)
   - `MAX_SPREAD_CENTS=1` – only buy when that side’s bid-ask spread is at most this many cents (default 1)
   - `STOP_LOSS_CENTS=70` – sell YES when YES bid ≤ this, sell NO when NO bid ≤ this (default 70)
   - `MAX_PCT_PER_MARKET=10` – max % of balance per market order (default 10)
   - `MAX_OPEN_POSITIONS=10` – max number of markets per side (YES and NO each get up to this many; default 10)
   - `STOP_LOSS_POLL_SECONDS=30` – seconds between main loop cycles (default 30)
   - `DRY_RUN=true` – log only, no real orders (default false)
   - `DISCOVERY_CATEGORIES_FILE=discovery_categories.txt` – path to a file with one **category slug** per line (e.g. `mens-college-basketball-mens-game` from the Kalshi URL path)

## Discovery

The bot discovers candidate markets only from the **discovery categories file** (`DISCOVERY_CATEGORIES_FILE`, default `discovery_categories.txt`). Each line is a **category slug** (the URL path segment, e.g. `mens-college-basketball-mens-game`). For each slug it finds series in that category and collects all open, active markets. If the file is empty or yields no markets, the bot logs “No markets found this cycle.”

## Discovery: categories file

Add category slugs to **`discovery_categories.txt`** (or set `DISCOVERY_CATEGORIES_FILE` to another path):

```text
# Category slugs from Kalshi URLs (path segment between event and market ticker)
mens-college-basketball-mens-game
```

The bot discovers series for each slug (via the API or client-side filter), then all open, active markets in those series, and applies entry/stop-loss to each (subject to max positions and bet sizing).