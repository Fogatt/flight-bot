from datetime import date
from decimal import Decimal

from flight_bot.bot import build_searches, pick_searches, run, save_state, should_alert
from flight_bot.duffel import parse_offer
from flight_bot.offers import Offer
from flight_bot.serpapi import parse_option

CFG = {
    "search": {
        "origins": ["BGO"],
        "destinations": ["GRU", "GIG"],
        "departure_dates": {"from": "2026-12-15", "to": "2026-12-16"},
    },
    "alert": {"max_price": 8000, "currency": "NOK"},
}
TODAY = date(2026, 10, 29)


def test_build_searches_one_way_and_round_trip():
    assert [s.departure for s in build_searches(CFG, TODAY)] == [date(2026, 12, 15), date(2026, 12, 16)]
    rt = dict(CFG, search=dict(CFG["search"], return_after_days=[7, 14]))
    searches = build_searches(rt, TODAY)
    assert len(searches) == 4
    assert searches[1].return_date == date(2026, 12, 29)


def test_past_dates_are_skipped():
    assert [s.departure for s in build_searches(CFG, date(2026, 12, 15))] == [date(2026, 12, 16)]


def test_pick_searches_rotates_through_all_dates():
    searches = list(range(5))
    state = {}
    seen = []
    for _ in range(3):
        seen += pick_searches(searches, state, max_calls=2, calls_per_search=1)
    assert seen == [0, 1, 2, 3, 4, 0]
    assert pick_searches(searches, {}, max_calls=None, calls_per_search=1) == searches
    # Duffel needs one call per airport pair; still do at least one search.
    assert len(pick_searches(searches, {}, max_calls=2, calls_per_search=9)) == 1


def test_should_alert():
    a = CFG["alert"]
    assert should_alert(Decimal("7500"), "NOK", a, None)
    assert not should_alert(Decimal("9000"), "NOK", a, None)
    assert not should_alert(Decimal("500"), "EUR", a, None)
    assert not should_alert(Decimal("7500"), "NOK", a, {"last_alerted_price": "7500"})
    assert should_alert(Decimal("7000"), "NOK", a, {"last_alerted_price": "7500"})


def test_per_currency_limits():
    a = {"max_price": {"NOK": 8000, "EUR": 690}}
    assert should_alert(Decimal("7999"), "NOK", a, None)
    assert should_alert(Decimal("650"), "EUR", a, None)
    assert not should_alert(Decimal("700"), "EUR", a, None)
    assert not should_alert(Decimal("500"), "USD", a, None)


def test_parse_serpapi_option():
    opt = {
        "price": 6450,
        "flights": [
            {
                "departure_airport": {"id": "BGO", "time": "2026-12-15 06:00"},
                "arrival_airport": {"id": "AMS", "time": "2026-12-15 08:00"},
                "airline": "KLM",
            },
            {
                "departure_airport": {"id": "AMS", "time": "2026-12-15 10:00"},
                "arrival_airport": {"id": "GRU", "time": "2026-12-15 19:10"},
                "airline": "KLM",
            },
        ],
    }
    offer = parse_option(opt, "NOK", "https://example")
    assert (offer.origin, offer.destination, offer.price, offer.airline) == ("BGO", "GRU", Decimal("6450"), "KLM")
    assert offer.itinerary == ["BGO 2026-12-15 06:00 -> GRU 19:10 (1 stop, via AMS)"]
    assert parse_option({"flights": opt["flights"]}, "NOK", None) is None  # no price


def test_parse_duffel_offer():
    raw = {
        "total_amount": "512.30",
        "total_currency": "EUR",
        "owner": {"name": "KLM"},
        "slices": [
            {
                "segments": [
                    {
                        "origin": {"iata_code": "BGO"},
                        "destination": {"iata_code": "AMS"},
                        "departing_at": "2026-12-15T06:00:00",
                        "arriving_at": "2026-12-15T08:00:00",
                    },
                    {
                        "origin": {"iata_code": "AMS"},
                        "destination": {"iata_code": "GRU"},
                        "departing_at": "2026-12-15T10:00:00",
                        "arriving_at": "2026-12-15T19:10:00",
                    },
                ]
            }
        ],
    }
    offer = parse_offer(raw, "BGO", "GRU")
    assert offer.price == Decimal("512.30")
    assert offer.itinerary == ["BGO 2026-12-15 06:00 -> GRU 19:10 (1 stop)"]


class FakeClient:
    multi_airport = True

    def __init__(self, prices):
        self.prices = prices  # {destination: [prices]}

    def search(self, origins, destinations, *args, **kwargs):
        return [
            Offer("BGO", dest, Decimal(p), "NOK", "Test Air", ["leg"])
            for dest in destinations
            for p in self.prices.get(dest, [])
        ]


def test_run_alerts_cheapest_once_then_only_on_drop(tmp_path):
    sent = []
    client = FakeClient({"GRU": ["7900", "7600"], "GIG": ["9500"]})
    state = run(CFG, client, {}, TODAY, sent.append, pause=0)
    assert len(sent) == 1 and sent[0].count("✈️") == 2  # GRU on both dates, cheapest only
    assert "7600 NOK" in sent[0] and "7900" not in sent[0]
    assert state["routes"]["BGO-GIG-2026-12-15-oneway"]["latest_price"] == "9500"

    sent.clear()
    state = run(CFG, client, state, TODAY, sent.append, pause=0)
    assert sent == []  # same price, no repeat alert

    client.prices["GRU"] = ["7000"]
    run(CFG, client, state, TODAY, sent.append, pause=0)
    assert "was 7600" in sent[0]

    save_state(tmp_path / "s.json", state, date(2026, 12, 15))
    assert "BGO-GRU-2026-12-15-oneway" not in (tmp_path / "s.json").read_text()
