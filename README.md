# Flight fare bot

Checks Google Flights for fares from your origin airports to all your destination airports, and sends a Telegram message when the cheapest fare to an airport on a date is at or below your limit. You're only alerted again for the same route and date if the price drops further.

Two bots run side by side from the same code and workflow, each with its own config in `configs/` and its own saved state in `state/`:

| Bot | Config | Source | Schedule |
|---|---|---|---|
| SerpApi | `configs/serpapi.yaml` | [SerpApi Google Flights API](https://serpapi.com/google-flights-api) (official, free plan 250 searches/month) | every 8 hours |
| fast-flights | `configs/fastflights.yaml` | [faster-flights](https://pypi.org/project/faster-flights/) library (unofficial scraper, no key, no quota, may break or get blocked) | every hour |

Both send to the same Telegram chat; each message says which bot it came from.

## Setup

1. **SerpApi key**: sign up at [serpapi.com](https://serpapi.com/users/sign_up) and copy your API key.
2. **Telegram bot**:
   - Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
   - Send any message to your new bot.
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`.
3. **GitHub secrets**: in this repo go to *Settings → Secrets and variables → Actions* and add
   `SERPAPI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Open *Actions → Check flights → Run workflow*, pick which bot to run (or `all`), and start it.

## Changing a schedule

Each config has a `schedule` cron, and `.github/workflows/check-flights.yml` must list the same cron under `on.schedule`. GitHub runs whichever configs match the cron that fired. Change both together (a test checks they match). Times are UTC.

## Search quota

`max_searches_per_run` limits how many API calls/requests one run makes; dates are checked in rotation so all of them get covered over a few runs.

- SerpApi: one search covers every airport for one date. 2 per run x 3 runs a day = ~186 searches/month.
- fast-flights: no limit set, so every date is checked on every run (one request per airport per date, 9 per date here). `dates_per_message: 5` sends one status message per 5 dates.

## Running locally

```bash
pip install -r requirements.txt
export SERPAPI_KEY=...
python -m flight_bot.bot --dry-run                                  # all bots; prints messages, saves nothing
python -m flight_bot.bot --config configs/fastflights.yaml --dry-run  # one bot
```

## Notes

- After every run each bot sends a short status message (dates checked, cheapest fare, time of the next check). Set `status_message: false` in its config to turn it off.
- A [Duffel](https://duffel.com) provider is also included (`provider: duffel`, secret `DUFFEL_TOKEN`), but Duffel only gives live tokens to verified businesses.
