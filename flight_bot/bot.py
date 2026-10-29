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
from itertools import product
from pathlib import Path

import yaml

from .duffel import DuffelClient, DuffelError, Offer
from .telegram import send_message


@dataclass
class Route:
    origin: str
    destination: str
    departure: date
    return_date: date | None

    @property
    def key(self) -> str:
        ret = self.return_date.isoformat() if self.return_date else "oneway"
        return f"{self.origin}-{self.destination}-{self.departure.isoformat()}-{ret}"

    def label(self) -> str:
        text = f"{self.origin} → {self.destination}, {self.departure:%a %d %b}"
        if self.return_date:
            text += f" ↩ {self.return_date:%a %d %b}"
        return text


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def build_routes(cfg: dict, today: date) -> list[Route]:
    search = cfg["search"]
    dates_cfg = search["departure_dates"]
    if isinstance(dates_cfg, dict):
        dates = date_range(to_date(dates_cfg["from"]), to_date(dates_cfg["to"]))
    else:
        dates = [to_date(d) for d in dates_cfg]
    dates = [d for d in dates if d > today]

    stays = search.get("return_after_days")
    if stays is None:
        stays = [None]
    elif isinstance(stays, int):
        stays = [stays]

    routes = []
    for origin, dest, dep, stay in product(search["origins"], search["destinations"], dates, stays):
        if origin == dest:
            continue
        ret = dep + timedelta(days=stay) if stay is not None else None
        routes.append(Route(origin.upper(), dest.upper(), dep, ret))
    return routes


def to_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_state(path: Path, state: dict, today: date) -> None:
    # Drop routes whose departure date has passed.
    state = {k: v for k, v in state.items() if date.fromisoformat(v["departure"]) > today}
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


def format_alert(route: Route, offer: Offer, prev: dict | None) -> str:
    lines = [f"✈️ <b>{escape(route.label())}</b>", f"<b>{offer.price} {offer.currency}</b> · {escape(offer.airline)}"]
    if prev and prev.get("last_alerted_price"):
        lines[-1] += f" (was {prev['last_alerted_price']})"
    lines += [escape(leg) for leg in offer.itinerary]
    return "\n".join(lines)


def run(cfg: dict, client: DuffelClient, state: dict, today: date, notify, pause: float = 1.0) -> dict:
    search, alert_cfg = cfg["search"], cfg["alert"]
    routes = build_routes(cfg, today)
    print(f"Checking {len(routes)} route/date combinations…")

    alerts = []
    for i, route in enumerate(routes):
        if i:
            time.sleep(pause)
        try:
            offers = client.search(
                route.origin,
                route.destination,
                route.departure,
                route.return_date,
                adults=search.get("adults", 1),
                cabin_class=search.get("cabin_class", "economy"),
                max_connections=search.get("max_connections"),
            )
        except (DuffelError, OSError) as e:
            print(f"  {route.key}: error: {e}", file=sys.stderr)
            continue

        if not offers:
            print(f"  {route.key}: no offers")
            continue

        cheapest = min(offers, key=lambda o: o.price)
        prev = state.get(route.key)
        print(f"  {route.key}: {cheapest.price} {cheapest.currency} ({cheapest.airline})")
        if cheapest.currency not in price_limits(alert_cfg):
            print(f"    note: no max_price set for {cheapest.currency}; not compared")

        entry = dict(prev or {}, departure=route.departure.isoformat())
        entry["latest_price"] = str(cheapest.price)
        entry["currency"] = cheapest.currency
        if "best_price" not in entry or cheapest.price < Decimal(entry["best_price"]):
            entry["best_price"] = str(cheapest.price)

        if should_alert(cheapest.price, cheapest.currency, alert_cfg, prev):
            alerts.append(format_alert(route, cheapest, prev))
            entry["last_alerted_price"] = str(cheapest.price)
        state[route.key] = entry

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument("--state", default="state.json", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending, don't save state")
    args = parser.parse_args()

    cfg = load_config(args.config)
    client = DuffelClient(os.environ["DUFFEL_TOKEN"])
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
