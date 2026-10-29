"""Minimal client for the Duffel flight search API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import requests

API_URL = "https://api.duffel.com/air/offer_requests"
API_VERSION = "v2"


class DuffelError(RuntimeError):
    pass


@dataclass
class Offer:
    id: str
    price: Decimal
    currency: str
    airline: str
    # One line per slice, e.g. "LHR 08:10 -> JFK 11:05 (direct)"
    itinerary: list[str]


class DuffelClient:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Duffel-Version": API_VERSION,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def search(
        self,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: date | None = None,
        adults: int = 1,
        cabin_class: str = "economy",
        max_connections: int | None = None,
    ) -> list[Offer]:
        slices = [
            {"origin": origin, "destination": destination, "departure_date": departure_date.isoformat()}
        ]
        if return_date:
            slices.append(
                {"origin": destination, "destination": origin, "departure_date": return_date.isoformat()}
            )

        data = {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(adults)],
            "cabin_class": cabin_class,
        }
        if max_connections is not None:
            data["max_connections"] = max_connections

        resp = self.session.post(
            API_URL,
            params={"return_offers": "true", "supplier_timeout": 20000},
            json={"data": data},
            timeout=60,
        )
        if not resp.ok:
            try:
                errors = resp.json().get("errors", [])
                detail = "; ".join(e.get("message", str(e)) for e in errors)
            except ValueError:
                detail = resp.text[:300]
            raise DuffelError(f"Duffel {resp.status_code}: {detail}")

        return [parse_offer(o) for o in resp.json()["data"].get("offers", [])]


def parse_offer(raw: dict) -> Offer:
    itinerary = []
    for sl in raw.get("slices", []):
        segments = sl["segments"]
        first, last = segments[0], segments[-1]
        stops = len(segments) - 1
        stops_text = "direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"
        itinerary.append(
            f"{first['origin']['iata_code']} {first['departing_at'][:16].replace('T', ' ')}"
            f" -> {last['destination']['iata_code']} {last['arriving_at'][11:16]}"
            f" ({stops_text})"
        )
    return Offer(
        id=raw["id"],
        price=Decimal(raw["total_amount"]),
        currency=raw["total_currency"],
        airline=raw.get("owner", {}).get("name", "?"),
        itinerary=itinerary,
    )
