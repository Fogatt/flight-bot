"""Minimal client for the Duffel flight search API.

Note: Duffel only issues live (real fare) tokens to verified businesses.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from itertools import product

import requests

from .offers import Offer, ProviderError, stops_text

API_URL = "https://api.duffel.com/air/offer_requests"
API_VERSION = "v2"


class DuffelClient:
    # Duffel needs one request per origin/destination pair.
    multi_airport = False

    def __init__(self, token: str, session: requests.Session | None = None, pause: float = 1.0):
        self.pause = pause
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
        origins: list[str],
        destinations: list[str],
        departure_date: date,
        return_date: date | None = None,
        adults: int = 1,
        cabin_class: str = "economy",
        max_connections: int | None = None,
    ) -> list[Offer]:
        offers = []
        for i, (origin, destination) in enumerate(product(origins, destinations)):
            if i:
                time.sleep(self.pause)
            offers += self._search_pair(
                origin, destination, departure_date, return_date, adults, cabin_class, max_connections
            )
        return offers

    def _search_pair(self, origin, destination, departure_date, return_date, adults, cabin_class, max_connections):
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
            raise ProviderError(f"Duffel {resp.status_code}: {detail}")

        return [parse_offer(o, origin, destination) for o in resp.json()["data"].get("offers", [])]


def parse_offer(raw: dict, origin: str, destination: str) -> Offer:
    itinerary = []
    for sl in raw.get("slices", []):
        segments = sl["segments"]
        first, last = segments[0], segments[-1]
        itinerary.append(
            f"{first['origin']['iata_code']} {first['departing_at'][:16].replace('T', ' ')}"
            f" -> {last['destination']['iata_code']} {last['arriving_at'][11:16]}"
            f" ({stops_text(len(segments))})"
        )
    return Offer(
        origin=origin,
        destination=destination,
        price=Decimal(raw["total_amount"]),
        currency=raw["total_currency"],
        airline=raw.get("owner", {}).get("name", "?"),
        itinerary=itinerary,
    )
