"""Google Flights search through SerpApi (https://serpapi.com/google-flights-api)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import requests

from .offers import Offer, ProviderError, stops_text

API_URL = "https://serpapi.com/search.json"
TRAVEL_CLASS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
# SerpApi "stops": 0 = any, 1 = nonstop, 2 = up to 1 stop, 3 = up to 2 stops
STOPS = {0: 1, 1: 2, 2: 3}


class SerpApiClient:
    # One search covers every origin/destination pair, since Google Flights
    # accepts comma-separated airport lists.
    multi_airport = True

    def __init__(self, api_key: str, currency: str = "NOK", country: str = "no"):
        self.api_key = api_key
        self.currency = currency
        self.country = country

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
        params = {
            "engine": "google_flights",
            "departure_id": ",".join(origins),
            "arrival_id": ",".join(destinations),
            "outbound_date": departure_date.isoformat(),
            "type": 1 if return_date else 2,
            "adults": adults,
            "travel_class": TRAVEL_CLASS[cabin_class],
            "stops": STOPS.get(max_connections, 0),
            "currency": self.currency,
            "gl": self.country,
            "hl": "en",
            "api_key": self.api_key,
        }
        if return_date:
            params["return_date"] = return_date.isoformat()

        resp = requests.get(API_URL, params=params, timeout=90)
        try:
            body = resp.json()
        except ValueError:
            raise ProviderError(f"SerpApi {resp.status_code}: {resp.text[:300]}")
        if "error" in body:
            # SerpApi reports "no results" as an error; that's not a failure for us.
            if "hasn't returned any results" in body["error"]:
                return []
            raise ProviderError(f"SerpApi: {body['error']}")
        resp.raise_for_status()

        link = body.get("search_metadata", {}).get("google_flights_url")
        options = body.get("best_flights", []) + body.get("other_flights", [])
        return [o for o in (parse_option(opt, self.currency, link) for opt in options) if o]


def parse_option(opt: dict, currency: str, link: str | None) -> Offer | None:
    flights = opt.get("flights") or []
    if "price" not in opt or not flights:
        return None
    first, last = flights[0], flights[-1]
    dep, arr = first["departure_airport"], last["arrival_airport"]
    airlines = list(dict.fromkeys(f.get("airline", "?") for f in flights))
    via = [f["arrival_airport"]["id"] for f in flights[:-1]]
    leg = f"{dep['id']} {dep.get('time', '')} -> {arr['id']} {arr.get('time', '')[-5:]} ({stops_text(len(flights))}"
    leg += f", via {', '.join(via)})" if via else ")"
    return Offer(
        origin=dep["id"],
        destination=arr["id"],
        price=Decimal(str(opt["price"])),
        currency=currency,
        airline=" + ".join(airlines),
        itinerary=[leg],
        link=link,
    )
