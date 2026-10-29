"""Check fares for every origin/destination/date combination and alert on good prices."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path

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


def format_alert(offer: Offer, search: Search, prev: dict | None) -> str:
    lines = [
        f"✈️ <b>{escape(route_label(offer, search))}</b>",
        f"<b>{offer.price} {offer.currency}</b> · {escape(offer.airline)}",
    ]
    if prev and prev.get("last_alerted_price"):
        lines[-1] += f" (was {prev['last_alerted_price']})"
    lines += [escape(leg) for leg in offer.itinerary]
    if offer.link:
        lines.append(f'<a href="{escape(offer.link)}">Open in Google Flights</a>')
    return "\n".join(lines)


def run(cfg: dict, client, state: dict, today: date, notify, pause: float = 1.0) -> dict:
    search_cfg, alert_cfg = cfg["search"], cfg["alert"]
    origins = [a.upper() for a in search_cfg["origins"]]
    destinations = [a.upper() for a in search_cfg["destinations"]]
    calls_per_search = 1 if client.multi_airport else len(origins) * len(destinations)

    all_searches = build_searches(cfg, today)
    searches = pick_searches(all_searches, state, search_cfg.get("max_searches_per_run"), calls_per_search)
    print(f"Running {len(searches)} of {len(all_searches)} date searches "
          f"({', '.join(origins)} → {', '.join(destinations)})…")

    routes = state.setdefault("routes", {})
    alerts = []
    for i, search in enumerate(searches):
        if i:
            time.sleep(pause)
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
                alerts.append(format_alert(offer, search, prev))
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
    print(f"Done. {len(alerts)} alert(s).")
    return state


def make_client(cfg: dict):
    provider = cfg.get("provider", "serpapi")
    if provider == "serpapi":
        from .serpapi import SerpApiClient

        return SerpApiClient(os.environ["SERPAPI_KEY"], currency=cfg["alert"].get("currency", "NOK"))
    if provider == "duffel":
        from .duffel import DuffelClient

        return DuffelClient(os.environ["DUFFEL_TOKEN"])
    raise SystemExit(f"Unknown provider: {provider}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument("--state", default="state.json", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending, don't save state")
    args = parser.parse_args()

    cfg = load_config(args.config)
    client = make_client(cfg)
    today = date.today()

    if args.dry_run:
        notify = lambda text: print("\n--- alert ---\n" + text)
    else:
        token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
        notify = lambda text: send_message(token, chat_id, text)

    state = run(cfg, client, load_state(args.state), today, notify)
    if not args.dry_run:
        save_state(args.state, state, today)


if __name__ == "__main__":
    main()
