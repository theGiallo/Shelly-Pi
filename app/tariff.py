"""Electricity price: one base price per kWh plus optional time slots.

A slot has a name, a price, the weekdays it applies to and a daily time window
on a 15-minute grid (the same grid the 15-minute rollup uses, so the cost of a
bucket is exact). If slots overlap, the one higher in the list wins. Times not
covered by any slot use the base price. A window whose end is not after its
start runs over midnight, and start == end means the whole day: a slot "night"
from 23:00 to 07:00 on Monday covers Monday 23:00-24:00 and Tuesday 00:00-07:00.

Prices can change over time. The prices at the start are the base price and
slots above; each later change is a date plus a complete new set of prices,
valid from midnight (in the tariff's time zone) at the start of that date until
the next change. Readings are always priced with what applied on their own day,
so a change leaves earlier costs as they were.

The tariff lives in the database (table meta, key "tariff"), so it changes
from the Settings page without restarting anything. Costs are computed when
they're asked for, from the stored readings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database

GRID = 15  # minutes
META_KEY = "tariff"
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
MAX_SLOTS = 12
MAX_CHANGES = 60
MAX_PRICE = 1000.0  # per kWh, in whatever currency; just a typo guard


class TariffError(ValueError):
    """The submitted tariff isn't usable; the message is meant for the person."""


def parse_hhmm(text: Any) -> int:
    """'07:30' -> minutes since midnight. '24:00' is accepted and means 00:00."""
    m = TIME_RE.match(str(text).strip())
    if not m:
        raise TariffError(f"'{text}' isn't a time. Use HH:MM, for example 07:30.")
    h, mi = int(m.group(1)), int(m.group(2))
    if (h, mi) == (24, 0):
        return 0
    if h > 23 or mi > 59:
        raise TariffError(f"'{text}' isn't a time of day.")
    if mi % GRID:
        raise TariffError(f"Times must be on a {GRID}-minute step (00, 15, 30, 45). '{text}' isn't.")
    return h * 60 + mi


def fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Slot:
    name: str
    price: float
    days: tuple[int, ...]  # 0 = Monday ... 6 = Sunday
    start: int  # minutes since midnight
    end: int

    def covers(self, weekday: int, minute: int) -> bool:
        if self.start == self.end:  # whole day
            return weekday in self.days
        if self.start < self.end:
            return weekday in self.days and self.start <= minute < self.end
        # runs over midnight: the evening belongs to the listed day, the morning to the day after
        return (weekday in self.days and minute >= self.start) or (
            (weekday - 1) % 7 in self.days and minute < self.end
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "price": self.price, "days": list(self.days),
                "start": fmt_hhmm(self.start), "end": fmt_hhmm(self.end)}


@dataclass
class Prices:
    """One complete set of prices: a base price and optional time slots."""
    base_price: float = 0.0
    slots: list[Slot] = field(default_factory=list)

    def slot_index(self, weekday: int, minute: int) -> int:
        """Index of the slot that applies, or -1 for the base price."""
        for i, s in enumerate(self.slots):
            if s.covers(weekday, minute):
                return i
        return -1

    def price_for(self, index: int) -> float:
        return self.base_price if index < 0 else self.slots[index].price

    def week(self) -> list[list[int]]:
        """Slot index for every quarter hour of a week: 7 rows (Monday first) of 96."""
        return [[self.slot_index(d, q * GRID) for q in range(24 * 60 // GRID)] for d in range(7)]

    def to_dict(self) -> dict:
        return {"base_price": self.base_price, "slots": [s.to_dict() for s in self.slots]}


@dataclass
class Change:
    """Prices that replace the earlier ones from the start of this (local) date."""
    start: date
    prices: Prices

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), **self.prices.to_dict()}


@dataclass
class Tariff:
    enabled: bool = False
    currency: str = "€"
    currency_after: bool = True  # "0.31 €" rather than "€ 0.31"
    decimals: int = 2  # decimals when showing an amount of money
    timezone: str = "UTC"
    base_price: float = 0.0  # per kWh
    slots: list[Slot] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)  # later prices, oldest first

    # ---- pricing -------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def initial(self) -> Prices:
        """The prices from the start, before any change."""
        return Prices(self.base_price, self.slots)

    def all_prices(self) -> list[tuple[date | None, Prices]]:
        """Every set of prices with the date it starts on (None for the first), oldest first."""
        return [(None, self.initial)] + [(c.start, c.prices) for c in self.changes]

    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz)

    def prices_at(self, ts: float) -> Prices:
        """The set of prices that applies on the (local) day of an instant."""
        day = self.local(ts).date()
        cur = self.initial
        for c in self.changes:
            if c.start > day:
                break
            cur = c.prices
        return cur

    def index_at(self, ts: float) -> int:
        """Index, in prices_at(ts).slots, of the slot that applies; -1 for the base price."""
        t = self.local(ts)
        return self.prices_at(ts).slot_index(t.weekday(), t.hour * 60 + t.minute)

    def price_at(self, ts: float) -> float:
        """Price per kWh at an instant. Handles daylight-saving changes (local time is used)."""
        p = self.prices_at(ts)
        t = self.local(ts)
        return p.price_for(p.slot_index(t.weekday(), t.hour * 60 + t.minute))

    def week(self) -> list[list[int]]:
        """Week grid of the prices from the start."""
        return self.initial.week()

    # ---- (de)serialising ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "currency": self.currency, "currency_after": self.currency_after,
            "decimals": self.decimals, "timezone": self.timezone, "base_price": self.base_price,
            "slots": [s.to_dict() for s in self.slots],
            "changes": [c.to_dict() for c in self.changes],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Tariff":
        """Validate and build. Raises TariffError with a message fit to show."""
        try:
            tz = str(d.get("timezone") or "UTC").strip()
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise TariffError(
                f"'{d.get('timezone')}' isn't a time zone name. Use a name like Europe/Rome."
            ) from None
        currency = str(d.get("currency", "€")).strip()
        if not 1 <= len(currency) <= 8:
            raise TariffError("The currency needs 1 to 8 characters, for example € or CHF.")
        if any(ord(c) < 32 for c in currency):
            raise TariffError("The currency can't contain control characters.")
        try:
            decimals = int(d.get("decimals", 2))
        except (TypeError, ValueError):
            raise TariffError("Prices and decimals must be numbers.") from None
        if not 0 <= decimals <= 4:
            raise TariffError("Show between 0 and 4 decimals.")
        first = _parse_prices(d)
        raw_changes = d.get("changes") or []
        if len(raw_changes) > MAX_CHANGES:
            raise TariffError(f"Up to {MAX_CHANGES} price changes are supported.")
        changes: list[Change] = []
        for c in raw_changes:
            try:
                start = date.fromisoformat(str(c.get("start")))
            except ValueError:
                raise TariffError("Every price change needs a date.") from None
            try:
                prices = _parse_prices(c)
            except TariffError as e:
                raise TariffError(f"Prices from {start.isoformat()}: {e}") from None
            changes.append(Change(start, prices))
        changes.sort(key=lambda c: c.start)
        for x, y in zip(changes, changes[1:]):
            if x.start == y.start:
                raise TariffError(f"There are two price changes on {x.start.isoformat()}. Keep one.")
        return cls(
            enabled=bool(d.get("enabled", False)), currency=currency,
            currency_after=bool(d.get("currency_after", True)), decimals=decimals,
            timezone=tz, base_price=first.base_price, slots=first.slots, changes=changes,
        )


def _parse_prices(d: dict) -> Prices:
    """A base price and slots from a dict, checked. Raises TariffError."""
    try:
        base = float(d.get("base_price", 0))
    except (TypeError, ValueError):
        raise TariffError("Prices and decimals must be numbers.") from None
    _check_price(base, "The base price")
    raw_slots = d.get("slots") or []
    if len(raw_slots) > MAX_SLOTS:
        raise TariffError(f"Up to {MAX_SLOTS} time slots are supported.")
    slots: list[Slot] = []
    for n, s in enumerate(raw_slots, 1):
        name = str(s.get("name") or "").strip() or f"Slot {n}"
        if len(name) > 24:
            raise TariffError(f"Slot name '{name[:24]}…' is too long (24 characters at most).")
        try:
            price = float(s.get("price"))
        except (TypeError, ValueError):
            raise TariffError(f"Slot '{name}' needs a price.") from None
        _check_price(price, f"The price of '{name}'")
        days = tuple(sorted({int(x) for x in (s.get("days") or [])}))
        if not days or any(x < 0 or x > 6 for x in days):
            raise TariffError(f"Pick at least one day for slot '{name}'.")
        slots.append(Slot(name, price, days, parse_hhmm(s.get("start", "00:00")),
                          parse_hhmm(s.get("end", "00:00"))))
    return Prices(base, slots)


def _check_price(v: float, what: str) -> None:
    if v != v or v < 0 or v > MAX_PRICE:  # NaN, negative, absurd
        raise TariffError(f"{what} must be a price per kWh between 0 and {MAX_PRICE:g}.")


def load(db: Database) -> Tariff:
    raw = db.get_meta(META_KEY)
    if raw:
        try:
            return Tariff.from_dict(json.loads(raw))
        except (ValueError, TypeError):
            pass  # a damaged value falls back to "no tariff" rather than stopping the dashboard
    return Tariff()


def save(db: Database, t: Tariff) -> None:
    db.set_meta(META_KEY, json.dumps(t.to_dict()))
