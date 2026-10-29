from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class ProviderError(RuntimeError):
    pass


@dataclass
class Offer:
    origin: str
    destination: str
    price: Decimal
    currency: str
    airline: str
    # One line per slice, e.g. "BGO 2026-12-10 06:00 -> GRU 21:05 (1 stop)"
    itinerary: list[str]
    link: str | None = None


def stops_text(segments: int) -> str:
    stops = segments - 1
    return "direct" if stops == 0 else f"{stops} stop{'s' if stops > 1 else ''}"
