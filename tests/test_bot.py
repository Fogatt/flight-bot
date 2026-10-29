from datetime import date, datetime, timezone
from pathlib import Path
from decimal import Decimal

from flight_bot.bot import (
    build_searches,
    describe_cron,
    load_config,
    next_cron_run,
    pick_searches,
    run,
    save_state,
    should_alert,
)
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
    "status_message": False,
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


def test_status_message_after_run():
    sent = []
    cfg = dict(CFG, status_message=True, name="fast-flights")
    client = FakeClient({"GRU": ["9100"], "GIG": ["8700"]})
    next_run = datetime(2026, 10, 29, 16, 17, tzinfo=timezone.utc)
    run(cfg, client, {}, TODAY, sent.append, pause=0, next_run=next_run)
    assert len(sent) == 1  # no fare under the limit, so only the status message
    assert "Flight bot ran</b> · <i>fast-flights</i>" in sent[0]
    assert "Tue 15 Dec: <b>8700 NOK</b> BGO → GIG (Test Air)" in sent[0]
    assert "Wed 16 Dec: <b>8700 NOK</b> BGO → GIG" in sent[0]
    assert "Next check: Thu 29 Oct 17:17" in sent[0]  # Oslo time (UTC+1)


def test_next_cron_run():
    now = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)
    assert next_cron_run("17 */8 * * *", now) == datetime(2026, 10, 29, 16, 17, tzinfo=timezone.utc)
    assert next_cron_run("17 */8 * * *", datetime(2026, 10, 29, 16, 20, tzinfo=timezone.utc)) == datetime(
        2026, 10, 30, 0, 17, tzinfo=timezone.utc
    )
    assert next_cron_run("0 6,18 * * *", now) == datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
    assert next_cron_run("0 9 * * 1-5", now) is None


def test_describe_cron():
    assert describe_cron("47 * * * *") == "every hour"
    assert describe_cron("17 */8 * * *") == "every 8 hours"
    assert describe_cron("0 9 * * 1-5") is None


def test_configs_are_valid():
    import yaml

    root = Path(__file__).parent.parent
    configs = {p.stem: load_config(p, root / "trip.yaml") for p in sorted((root / "configs").glob("*.yaml"))}
    assert {c["provider"] for c in configs.values()} == {"serpapi", "fastflights"}
    assert len({c["state_file"] for c in configs.values()}) == len(configs)  # separate state per bot
    for c in configs.values():
        assert next_cron_run(c["schedule"], datetime(2026, 10, 29, tzinfo=timezone.utc)) is not None
        # Trip settings come from trip.yaml and are the same for every bot.
        assert c["search"]["origins"] == ["BGO"] and len(c["search"]["destinations"]) == 9
        assert c["alert"]["max_price"] == 8000
    assert configs["serpapi"]["search"]["max_searches_per_run"] == 2
    assert "max_searches_per_run" not in configs["fastflights"]["search"]

    # The workflow's manual-run choices must match the config file names.
    workflow = yaml.safe_load((root / ".github/workflows/check-flights.yml").read_text())
    options = workflow[True]["workflow_dispatch"]["inputs"]["config"]["options"]  # PyYAML reads `on:` as True
    assert set(options) == {"all", *configs}


def test_parse_fastflights_result():
    from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight

    from flight_bot.fastflights import parse_result

    def seg(a, b, dep, arr):
        return SingleFlight(
            from_airport=Airport(a, a), to_airport=Airport(b, b),
            departure=SimpleDatetime((2026, 12, 15), dep), arrival=SimpleDatetime((2026, 12, 15), arr),
            duration=100, plane_type="",
        )

    result = Flights(
        type="BGO", price=7420, airlines=["KLM"],
        flights=[seg("BGO", "AMS", (6,), (8, 5)), seg("AMS", "GIG", (10, 30), (19,))],
        carbon=CarbonEmission(0, 0),
    )
    offer = parse_result(result, "NOK", "https://example")
    assert (offer.origin, offer.destination, offer.price) == ("BGO", "GIG", Decimal("7420"))
    assert offer.itinerary == ["BGO 2026-12-15 06:00 -> GIG 19:00 (1 stop, via AMS)"]


def test_dates_per_message_splits_status_into_parts():
    sent = []
    cfg = dict(
        CFG,
        status_message=True,
        dates_per_message=5,
        search=dict(CFG["search"], departure_dates={"from": "2026-12-10", "to": "2026-12-21"}),  # 12 dates
    )
    client = FakeClient({"GRU": ["7900"]})  # under the limit: one alert per date
    next_run = datetime(2026, 10, 29, 16, 17, tzinfo=timezone.utc)
    run(cfg, client, {}, TODAY, sent.append, pause=0, next_run=next_run)

    status = [m for m in sent if "Flight bot ran" in m]
    assert [m.split("\n")[0].split(" · ")[-1] for m in status] == ["part 1/3", "part 2/3", "part 3/3"]
    assert [m.count(" Dec: ") for m in status] == [5, 5, 2]
    assert ["Next check" in m for m in status] == [False, False, True]
    # Each part's alerts are sent just before its status message.
    assert [m.count("✈️") for m in sent if "✈️" in m] == [5, 5, 2]


def test_bot_config_overrides_shared_trip(tmp_path):
    (tmp_path / "trip.yaml").write_text("search: {origins: [BGO], adults: 1}\nalert: {max_price: 8000}\n")
    (tmp_path / "bot.yaml").write_text("search: {adults: 2}\nname: x\n")
    cfg = load_config(tmp_path / "bot.yaml", tmp_path / "trip.yaml")
    assert cfg == {"search": {"origins": ["BGO"], "adults": 2}, "alert": {"max_price": 8000}, "name": "x"}
