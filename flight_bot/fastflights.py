"""Google Flights search through the unofficial faster-flights library (no API key).

It reads Google Flights pages directly, so it can break when Google changes them
or start failing if Google blocks the requests.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from itertools import product

from .offers import Offer, ProviderError, stops_text

SEAT = {"economy": "economy", "premium_economy": "premium-economy", "business": "business", "first": "first"}


class FastFlightsClient:
    # Google Flights takes a single airport per leg here, so each pair is a request.
    multi_airport = False

    def __init__(self, currency: str = "NOK", pause: float = 2.0):
        self.currency = currency
        self.pause = pause

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
        offers, failures = [], []
        pairs = list(product(origins, destinations))
        for i, (origin, destination) in enumerate(pairs):
            if i:
                time.sleep(self.pause)
            try:
                offers += self._search_pair(
                    origin, destination, departure_date, return_date, adults, cabin_class, max_connections
                )
            except Exception as e:  # the library raises assorted errors when Google changes or blocks
                failures.append(f"{origin}-{destination}: {type(e).__name__}: {e}")
        if failures and len(failures) == len(pairs):
            raise ProviderError("fast-flights: every search failed; " + failures[0])
        for f in failures:
            print(f"    fast-flights {departure_date}: {f}")
        return offers

    def _search_pair(self, origin, destination, departure_date, return_date, adults, cabin_class, max_connections):
        from fast_flights import FlightQuery, Passengers, create_query, get_flights

        legs = [FlightQuery(date=departure_date.isoformat(), from_airport=origin, to_airport=destination)]
        if return_date:
            legs.append(FlightQuery(date=return_date.isoformat(), from_airport=destination, to_airport=origin))
        query = create_query(
            flights=legs,
            seat=SEAT[cabin_class],
            trip="round-trip" if return_date else "one-way",
            passengers=Passengers(adults=adults),
            language="en-US",
            currency=self.currency,
            max_stops=max_connections,
        )
        results = get_flights(query)
        return [o for o in (parse_result(r, self.currency, query.url()) for r in results) if o]


def parse_result(result, currency: str, link: str | None) -> Offer | None:
    flights = result.flights
    if not flights or not result.price:
        return None
    first, last = flights[0], flights[-1]
    dep_y, dep_m, dep_d = first.departure.date
    via = [f.to_airport.code for f in flights[:-1]]
    leg = (
        f"{first.from_airport.code} {dep_y:04d}-{dep_m:02d}-{dep_d:02d} {_hhmm(first.departure.time)}"
        f" -> {last.to_airport.code} {_hhmm(last.arrival.time)} ({stops_text(len(flights))}"
    )
    leg += f", via {', '.join(via)})" if via else ")"
    return Offer(
        origin=first.from_airport.code,
        destination=last.to_airport.code,
        price=Decimal(result.price),
        currency=currency,
        airline=" + ".join(result.airlines) or "?",
        itinerary=[leg],
        link=link,
    )


def _hhmm(t) -> str:
    # Google sometimes leaves out the minutes for times on the hour.
    hour, minute = (list(t) + [0, 0])[:2]
    return f"{hour or 0:02d}:{minute or 0:02d}"
