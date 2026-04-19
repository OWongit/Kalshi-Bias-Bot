# Kalshi Automated NO Trading Bot

An automated trading bot for [Kalshi](https://kalshi.com) that discovers markets from category/series slugs, opens **NO** positions under strict entry rules, sizes bets from your balance, and manages risk with a stop-loss.

## Quick Start

### 1. Install dependencies

**Raspberry Pi:** Run the install script for a full setup (venv, dependencies, desktop autostart):

```bash
chmod +x install.sh
./install.sh
```

**Service commands:**
```
sudo systemctl status kalshi-trading-bot          # is it running?
sudo systemctl restart kalshi-trading-bot         # restart after code/config changes
sudo systemctl stop kalshi-trading-bot            # stop bot
sudo systemctl disable --now kalshi-trading-bot   # stop and disable boot start
journalctl -u kalshi-trading-bot -f               # follow logs

```

Requires Raspberry Pi OS with Desktop and Desktop Autologin enabled (`raspi-config` → Boot → Desktop Autologin).

**Other platforms:**

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

Open `config.py` and set:

- **`API_KEY_ID`** — your Kalshi API key ID.
- **`PRIVATE_KEY_PATH`** — path to the `.key` file downloaded when you created the API key. If the file is not available, set `PRIVATE_KEY_STRING` to the PEM text instead (literal `\n` is supported).

### 3. Choose environment

| Environment | `BASE_URL` |
|-------------|------------|
| **Demo** (default) | `https://demo-api.kalshi.co` |
| **Production** | `https://api.elections.kalshi.com` |

Start with Demo to test safely.

### 4. Edit the categories config

Edit `categories.json` to add categories (slugs or series tickers) and per-category trading params:

```json
{
  "defaults": {
    "entry_price": 95,
    "stop_loss": 70,
    "max_spread": 2,
    "min_open_interest": null,
    "stop_out_cooldown_seconds": 300
  },
  "categories": [
    {"slug": "kxncaambgame"},
    {"slug": "kxeth15m", "entry_price": 90, "stop_loss": 0, "max_spread": 1}
  ]
}
```

Each category can override any default. Omit fields to use defaults.

### 5. Run

```bash
python main.py
```

The bot starts in **dry-run mode** by default (`DRY_RUN = True` in `config.py`). It will log what orders it *would* place without actually sending them. Set `DRY_RUN = False` when you are ready to trade live.

## How It Works

1. **Discovery** — reads `categories.json`, resolves each slug to series tickers, then fetches open markets. Per-category params (entry_price, stop_loss, max_spread, min_open_interest, stop_out_cooldown_seconds) apply per market.
2. **Entry** — qualifies when the best bid (integer cents) matches `entry_price` rules: if `buy_if_bid_gt_entry` is **true**, enter when `bid >= entry_price`; if **false**, enter only when `bid == entry_price`. Posts a limit buy at the current best bid plus `bid_limit_offset` (cents, default 0). Only one resting buy is maintained per market/side — if the bid moves, the stale order is canceled and replaced.
3. **Sizing** — allocates `MAX_PCT_PER_MARKET` of your balance per market, clamped between `MIN_CONTRACTS` and `MAX_CONTRACTS`. Respects `MAX_OPEN_POSITIONS`.
4. **Stop-loss** — for every open YES or NO position, if the bid on that side falls to or below the category's `stop_loss`, places a limit sell at the current bid.
5. **Cooldown** — tickers sold by stop-loss are excluded from re-entry for the category's `stop_out_cooldown_seconds`.
6. **Loop** — sleeps `STOP_LOSS_POLL_SECONDS` between iterations.

## Configuration Reference

All settings live in `config.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `categories.json` | — | Per-category: entry_price, stop_loss, max_spread, min_open_interest, stop_out_cooldown_seconds, buy_if_bid_gt_entry (true: bid ≥ entry; false: bid = entry), bid_limit_offset |
| `MAX_OPEN_POSITIONS` | 5 | Max concurrent NO markets |
| `MAX_PCT_PER_MARKET` | 0.10 | Fraction of balance per market |
| `MIN_CONTRACTS` | 1 | Minimum contracts per order |
| `MAX_CONTRACTS` | 10,000 | Maximum contracts per order |
| `STOP_LOSS_POLL_SECONDS` | 30 | Sleep between loop iterations |
| `DRY_RUN` | True | Log orders without sending |

## Project Structure

```
config.py          Configuration and credential loading
api_client.py      Authenticated Kalshi HTTP client (RSA-PSS signing)
discovery.py       Market discovery from categories/series
trading.py         Entry logic, bet sizing, stop-loss
main.py            Main loop orchestrator
categories.json    Categories with per-category trading params
requirements.txt   Python dependencies
```
