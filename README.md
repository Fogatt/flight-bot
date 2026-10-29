# Flight fare bot

Checks Google Flights (via [SerpApi](https://serpapi.com/google-flights-api)) for fares from your origin airports to all your destination airports, and sends a Telegram message when the cheapest fare to an airport on a date is at or below your limit. You're only alerted again for the same route and date if the price drops further.

## Setup

1. **SerpApi key**: sign up at [serpapi.com](https://serpapi.com/users/sign_up) (free plan: 250 searches/month) and copy your API key from the dashboard.
2. **Telegram bot**:
   - Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
   - Send any message to your new bot.
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`.
3. **GitHub secrets**: in this repo go to *Settings → Secrets and variables → Actions* and add
   `SERPAPI_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Open *Actions → Check flights → Run workflow* to test it. After that it runs every 8 hours.

## Search quota

One SerpApi search covers every origin and destination airport for one date. `max_searches_per_run` in `config.yaml` limits how many dates each run checks; dates are checked in rotation, so all of them get covered over a few runs.

With 13 dates, 3 runs a day and 2 searches per run, that's about 186 searches a month (each date checked about every 2 days). On a paid SerpApi plan, raise `max_searches_per_run` or remove it to check every date on every run.

## Running locally

```bash
pip install -r requirements.txt
export SERPAPI_KEY=...
python -m flight_bot.bot --dry-run   # prints alerts, sends nothing, saves no state
```

## Notes

- After every run the bot sends a short status message (dates checked, cheapest fare, time of the next check). Set `status_message: false` in `config.yaml` to turn it off.
- `state.json` remembers the latest, best and last-alerted price for each route, plus which date to check next. The workflow commits it back to the repo after each run.
- A [Duffel](https://duffel.com) provider is also included (`provider: duffel`, secret `DUFFEL_TOKEN`), but Duffel only gives live tokens to verified businesses.
