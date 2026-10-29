from datetime import date
from decimal import Decimal

from flight_bot.bot import build_routes, run, should_alert
from flight_bot.duffel import Offer, parse_offer

CFG = {
    "search": {
        "origins": ["LHR", "LGW"],
        "destinations": ["JFK", "BOS"],
        "departure_dates": {"from": "2026-12-15", "to": "2026-12-16"},
        "return_after_days": 10,
    },
    "alert": {"max_price": 600, "currency": "GBP"},
}
TODAY = date(2026, 10, 29)


def test_build_routes_covers_every_combination():
    routes = build_routes(CFG, TODAY)
    assert len(routes) == 2 * 2 * 2
    assert routes[0].return_date == date(2026, 12, 25)


def test_past_dates_are_skipped():
    assert build_routes(CFG, date(2026, 12, 15)) != []
    assert all(r.departure == date(2026, 12, 16) for r in build_routes(CFG, date(2026, 12, 15)))


def test_should_alert():
    a = CFG["alert"]
    assert should_alert(Decimal("500"), "GBP", a, None)
    assert not should_alert(Decimal("700"), "GBP", a, None)
    assert not should_alert(Decimal("500"), "USD", a, None)
    assert not should_alert(Decimal("500"), "GBP", a, {"last_alerted_price": "500"})
    assert should_alert(Decimal("450"), "GBP", a, {"last_alerted_price": "500"})


def test_per_currency_limits():
    a = {"max_price": {"NOK": 8000, "EUR": 690}}
    assert should_alert(Decimal("7999"), "NOK", a, None)
    assert should_alert(Decimal("650"), "EUR", a, None)
    assert not should_alert(Decimal("700"), "EUR", a, None)
    assert not should_alert(Decimal("500"), "USD", a, None)


def test_parse_offer():
    raw = {
        "id": "off_1",
        "total_amount": "512.30",
        "total_currency": "GBP",
        "owner": {"name": "British Airways"},
        "slices": [
            {
                "segments": [
                    {
                        "origin": {"iata_code": "LHR"},
                        "destination": {"iata_code": "DUB"},
                        "departing_at": "2026-12-15T08:10:00",
                        "arriving_at": "2026-12-15T09:30:00",
                    },
                    {
                        "origin": {"iata_code": "DUB"},
                        "destination": {"iata_code": "JFK"},
                        "departing_at": "2026-12-15T11:00:00",
                        "arriving_at": "2026-12-15T13:05:00",
                    },
                ]
            }
        ],
    }
    offer = parse_offer(raw)
    assert offer.price == Decimal("512.30")
    assert offer.itinerary == ["LHR 2026-12-15 08:10 -> JFK 13:05 (1 stop)"]


class FakeClient:
    def __init__(self, prices):
        self.prices = prices

    def search(self, origin, destination, *args, **kwargs):
        p = self.prices.get((origin, destination))
        return [Offer("x", Decimal(p), "GBP", "Test Air", ["leg"])] if p else []


def test_run_alerts_once_then_only_on_drop():
    sent = []
    client = FakeClient({("LHR", "JFK"): "550", ("LGW", "BOS"): "900"})
    state = run(CFG, client, {}, TODAY, sent.append, pause=0)
    assert len(sent) == 1 and sent[0].count("✈️") == 2  # LHR-JFK on both dates

    sent.clear()
    state = run(CFG, client, state, TODAY, sent.append, pause=0)
    assert sent == []  # same price, no repeat alert

    client.prices[("LHR", "JFK")] = "520"
    run(CFG, client, state, TODAY, sent.append, pause=0)
    assert "was 550" in sent[0]
