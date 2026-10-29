"""Check fares for every origin/destination/date combination and alert on good prices."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .offers import Offer, ProviderError
from .telegram import send_message


@dataclass
class Search:
    departure: date
    return_date: date | None


def route_key(offer: Offer, search: Search) -> str:
    ret = search.return_date.isoformat() if search.return_date else "oneway"
    return f"{offer.origin}-{offer.destination}-{search.departure.isoformat()}-{ret}"


def route_label(offer: Offer, search: Search) -> str:
    text = f"{offer.origin} → {offer.destination}, {search.departure:%a %d %b}"
    if search.return_date:
        text += f" ↩ {search.return_date:%a %d %b}"
    return text


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def to_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def build_searches(cfg: dict, today: date) -> list[Search]:
    search = cfg["search"]
    dates_cfg = search["departure_dates"]
    if isinstance(dates_cfg, dict):
        start, end = to_date(dates_cfg["from"]), to_date(dates_cfg["to"])
        dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        dates = [to_date(d) for d in dates_cfg]
    dates = [d for d in dates if d > today]

    stays = search.get("return_after_days")
    if stays is None:
        stays = [None]
    elif isinstance(stays, int):
        stays = [stays]

    return [
        Search(dep, dep + timedelta(days=stay) if stay is not None else None)
        for dep in dates
        for stay in stays
    ]


def pick_searches(searches: list[Search], state: dict, max_calls: int | None, calls_per_search: int) -> list[Search]:
    """Take the next batch of searches in round-robin order, so a small API quota
    still covers every date over several runs."""
    if not searches or max_calls is None:
        return searches
    count = min(len(searches), max(1, max_calls // calls_per_search))
    start = state.get("cursor", 0) % len(searches)
    state["cursor"] = (start + count) % len(searches)
    return [searches[(start + i) % len(searches)] for i in range(count)]


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict, today: date) -> None:
    # Drop routes whose departure date has passed.
    routes = {k: v for k, v in state.get("routes", {}).items() if date.fromisoformat(v["departure"]) > today}
    state = dict(state, routes=routes)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def price_limits(alert_cfg: dict) -> dict[str, Decimal]:
    """`max_price` is either a number (in `currency`) or a {currency: limit} mapping."""
    limits = alert_cfg["max_price"]
    if not isinstance(limits, dict):
        limits = {alert_cfg["currency"]: limits}
    return {cur.upper(): Decimal(str(v)) for cur, v in limits.items()}


def should_alert(price: Decimal, currency: str, alert_cfg: dict, prev: dict | None) -> bool:
    """Alert when the price is under the limit and lower than the last price we alerted on."""
    limit = price_limits(alert_cfg).get(currency)
    if limit is None or price > limit:
        return False
    last_alerted = prev.get("last_alerted_price") if prev else None
    return last_alerted is None or price < Decimal(last_alerted)


def format_alert(offer: Offer, search: Search, prev: dict | None, source: str) -> str:
    lines = [
        f"✈️ <b>{escape(route_label(offer, search))}</b> · <i>{escape(source)}</i>",
        f"<b>{offer.price} {offer.currency}</b> · {escape(offer.airline)}",
    ]
    if prev and prev.get("last_alerted_price"):
        lines[-1] += f" (was {prev['last_alerted_price']})"
    lines += [escape(leg) for leg in offer.itinerary]
    if offer.link:
        lines.append(f'<a href="{escape(offer.link)}">Open in Google Flights</a>')
    return "\n".join(lines)


def run(
    cfg: dict, client, state: dict, today: date, notify, pause: float = 1.0, next_run: datetime | None = None
) -> dict:
    search_cfg, alert_cfg = cfg["search"], cfg["alert"]
    origins = [a.upper() for a in search_cfg["origins"]]
    destinations = [a.upper() for a in search_cfg["destinations"]]
    calls_per_search = 1 if client.multi_airport else len(origins) * len(destinations)

    all_searches = build_searches(cfg, today)
    searches = pick_searches(all_searches, state, search_cfg.get("max_searches_per_run"), calls_per_search)
    print(f"Running {len(searches)} of {len(all_searches)} date searches "
          f"({', '.join(origins)} → {', '.join(destinations)})…")

    # Optionally split the run into groups of dates, with one Telegram message per group.
    per_message = cfg.get("dates_per_message") or len(searches) or 1
    batches = [searches[i : i + per_message] for i in range(0, len(searches), per_message)] or [[]]

    routes = state.setdefault("routes", {})
    total_alerts = searched = 0
    for part, batch in enumerate(batches, 1):
        alerts = []
        found: list[tuple[Offer, Search]] = []
        errors = 0
        for search in batch:
            if searched:
                time.sleep(pause)
            searched += 1
            try:
                offers = client.search(
                    origins,
                    destinations,
                    search.departure,
                    search.return_date,
                    adults=search_cfg.get("adults", 1),
                    cabin_class=search_cfg.get("cabin_class", "economy"),
                    max_connections=search_cfg.get("max_connections"),
                )
            except (ProviderError, OSError) as e:
                print(f"  {search.departure}: error: {e}", file=sys.stderr)
                errors += 1
                continue

            # Cheapest offer per origin/destination pair.
            cheapest: dict[tuple[str, str], Offer] = {}
            for offer in offers:
                pair = (offer.origin, offer.destination)
                if pair not in cheapest or offer.price < cheapest[pair].price:
                    cheapest[pair] = offer
            if not cheapest:
                print(f"  {search.departure}: no offers")

            for offer in sorted(cheapest.values(), key=lambda o: o.price):
                found.append((offer, search))
                key = route_key(offer, search)
                prev = routes.get(key)
                print(f"  {key}: {offer.price} {offer.currency} ({offer.airline})")
                if offer.currency not in price_limits(alert_cfg):
                    print(f"    note: no max_price set for {offer.currency}; not compared")

                entry = dict(prev or {}, departure=search.departure.isoformat())
                entry["latest_price"] = str(offer.price)
                entry["currency"] = offer.currency
                if "best_price" not in entry or offer.price < Decimal(entry["best_price"]):
                    entry["best_price"] = str(offer.price)

                if should_alert(offer.price, offer.currency, alert_cfg, prev):
                    alerts.append(format_alert(offer, search, prev, source_name(cfg)))
                    entry["last_alerted_price"] = str(offer.price)
                routes[key] = entry

        if alerts:
            # Telegram messages are capped at 4096 chars, so send in chunks.
            chunk = ""
            for a in alerts:
                if len(chunk) + len(a) > 3800:
                    notify(chunk)
                    chunk = ""
                chunk += a + "\n\n"
            notify(chunk)
        if cfg.get("status_message", True):
            label = f"{part}/{len(batches)}" if len(batches) > 1 else None
            last = part == len(batches)
            notify(format_status(cfg, batch, found, len(alerts), errors, next_run if last else None, label))
        total_alerts += len(alerts)

    print(f"Done. {total_alerts} alert(s).")
    return state


def format_status(
    cfg: dict,
    searches: list[Search],
    found: list[tuple[Offer, Search]],
    alerts: int,
    errors: int,
    next_run: datetime | None,
    part: str | None = None,
) -> str:
    """Short summary sent after every run so you know the bot is alive,
    with the cheapest fare found for each date checked."""
    title = f"✅ <b>Flight bot ran</b> · <i>{escape(source_name(cfg))}</i>"
    if part:
        title += f" · part {part}"
    lines = [title]
    for search in searches:
        fares = [o for o, s in found if s is search]
        if fares:
            o = min(fares, key=lambda o: o.price)
            lines.append(
                f"{search.departure:%a %d %b}: <b>{o.price} {o.currency}</b> {o.origin} → {o.destination}"
                f" ({escape(o.airline)})"
            )
        else:
            lines.append(f"{search.departure:%a %d %b}: no fares")
    if not searches:
        lines.append("No dates left to check.")
    lines.append(f"Alerts sent: {alerts}" + (f" · Failed searches: {errors}" if errors else ""))
    if next_run:
        tz = ZoneInfo(cfg.get("timezone", "Europe/Oslo"))
        minutes = (next_run - datetime.now(timezone.utc)).total_seconds() / 60
        wait = f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.0f} h"
        line = f"Next check: {next_run.astimezone(tz):%a %d %b %H:%M} (in ~{wait})"
        every = describe_cron(cfg.get("schedule", ""))
        if every:
            line += f" · runs {every}"
        lines.append(line)
    return "\n".join(lines)


def describe_cron(cron: str) -> str | None:
    """"every hour" / "every 8 hours" for the simple crons next_cron_run understands."""
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        return None
    if fields[1] == "*":
        return "every hour"
    if fields[1].startswith("*/") and fields[1][2:].isdigit():
        return f"every {fields[1][2:]} hours"
    return None


def next_cron_run(cron: str, now: datetime) -> datetime | None:
    """Next UTC time for a simple cron like "17 */8 * * *" (fixed minute; hour as *, */N, N or N,M).
    Returns None for anything more complex."""
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"] or not fields[0].isdigit():
        return None
    minute, hour_field = int(fields[0]), fields[1]
    if hour_field == "*":
        hours = set(range(24))
    elif hour_field.startswith("*/") and hour_field[2:].isdigit():
        hours = set(range(0, 24, int(hour_field[2:])))
    elif all(h.isdigit() for h in hour_field.split(",")):
        hours = {int(h) for h in hour_field.split(",")}
    else:
        return None
    t = now.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    for _ in range(48):
        if t > now and t.hour in hours:
            return t
        t += timedelta(hours=1)
    return None


def source_name(cfg: dict) -> str:
    return cfg.get("name") or cfg.get("provider", "serpapi")


def make_client(cfg: dict):
    provider = cfg.get("provider", "serpapi")
    if provider == "serpapi":
        from .serpapi import SerpApiClient

        return SerpApiClient(os.environ["SERPAPI_KEY"], currency=cfg["alert"].get("currency", "NOK"))
    if provider == "fastflights":
        from .fastflights import FastFlightsClient

        return FastFlightsClient(currency=cfg["alert"].get("currency", "NOK"))
    if provider == "duffel":
        from .duffel import DuffelClient

        return DuffelClient(os.environ["DUFFEL_TOKEN"])
    raise SystemExit(f"Unknown provider: {provider}")


def run_config(path: Path, dry_run: bool) -> None:
    cfg = load_config(path)
    state_path = Path(cfg.get("state_file", "state.json"))
    print(f"=== {source_name(cfg)} ({path}) ===")
    client = make_client(cfg)
    today = date.today()

    if dry_run:
        notify = lambda text: print("\n--- telegram ---\n" + text)
    else:
        token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        notify = lambda text: send_message(token, chat_id, text)

    cron = cfg.get("schedule")
    next_run = next_cron_run(cron, datetime.now(timezone.utc)) if cron else None
    state = run(cfg, client, load_state(state_path), today, notify, next_run=next_run)
    if not dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        save_state(state_path, state, today)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", action="append", type=Path, help="config file to run (repeatable; default: every file in configs/)"
    )
    parser.add_argument(
        "--schedule", default="", help="only run configs whose `schedule` equals this cron (used by GitHub Actions)"
    )
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of sending, don't save state")
    args = parser.parse_args()

    paths = args.config or sorted(Path("configs").glob("*.yaml"))
    if args.schedule:
        paths = [p for p in paths if load_config(p).get("schedule") == args.schedule]
        if not paths:
            raise SystemExit(f"No config in configs/ has schedule {args.schedule!r}")

    failed = []
    for path in paths:
        try:
            run_config(path, args.dry_run)
        except Exception as e:
            # Keep going so one broken source doesn't stop the others.
            print(f"{path}: {type(e).__name__}: {e}", file=sys.stderr)
            failed.append(path)
    if failed:
        raise SystemExit(f"Failed: {', '.join(map(str, failed))}")


if __name__ == "__main__":
    main()
