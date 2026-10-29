# Flight fare bot

Searches [Duffel](https://duffel.com) for every combination of origin airport, destination airport and date in `config.yaml`. When the cheapest fare is at or below your limit, it sends you a Telegram message. It doesn't resend the same price: you get a new message only when a fare drops below the last price it told you about.

## Setup

1. **Duffel token**: sign up at duffel.com and create an access token under *Developers → Access tokens*.
   Test tokens (`duffel_test_…`) only return fake "Duffel Airways" flights. You need a live token for real fares.
2. **Telegram bot**:
   - Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
   - Send any message to your new bot.
   - Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`.
3. **GitHub**: push this folder to a new repo (a private one is fine). Then go to *Settings → Secrets and variables → Actions* and add these secrets:
   `DUFFEL_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Edit `config.yaml`. Then open *Actions → Check flights → Run workflow* to test it. After that it runs every 6 hours.

## Running locally

```bash
pip install -r requirements.txt
export DUFFEL_TOKEN=duffel_test_...
python -m flight_bot.bot --dry-run   # prints alerts, sends nothing, saves no state
```

## Notes

- Searches per run = origins × destinations × dates × stay lengths. Each search takes a few seconds.
- `state.json` remembers the latest, best and last-alerted price for each route. The workflow commits it back to the repo after each run.
- Duffel returns prices in the airline's selling currency (NOK, EUR, …). `alert.max_price` sets a limit per currency. Prices in a currency with no limit are logged but never trigger an alert.
