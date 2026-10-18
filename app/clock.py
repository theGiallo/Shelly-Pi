"""Decides whether the Pi's wall clock can be trusted for timestamps.

The Pi 4 has no battery-backed clock, and this box usually has no internet
(so no NTP). With the DS3231 RTC the time survives power loss, but if the
RTC battery dies or the RTC was never set, the clock can be badly wrong.

Rule: the clock is trusted when it is
  * not earlier than MIN_VALID_EPOCH (the project didn't exist before that), and
  * not earlier than the newest timestamp we ever stored, minus a tolerance.
Until it is trusted, samples stay in RAM (stamped on a monotonic clock) and are
written with corrected timestamps once the time is set.
"""

from __future__ import annotations

import time
from typing import Callable

from .config import Settings
from .db import Database


class ClockGuard:
    def __init__(
        self, settings: Settings, db: Database, time_fn: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.db = db
        self._time = time_fn
        self.last_good = db.get_meta_int("last_good_epoch", 0)

    def now(self) -> float:
        return self._time()

    @property
    def floor(self) -> int:
        return max(
            self.settings.min_valid_epoch,
            self.last_good - self.settings.clock_tolerance_s,
        )

    def status(self) -> dict:
        now = self.now()
        if now < self.settings.min_valid_epoch:
            reason = "before_minimum"
        elif now < self.last_good - self.settings.clock_tolerance_s:
            reason = "went_backwards"
        else:
            reason = "ok"
        return {
            "trusted": reason == "ok",
            "reason": reason,
            "now": int(now),
            "last_good": self.last_good,
        }

    @property
    def trusted(self) -> bool:
        return self.now() >= self.floor

    def note_good(self, epoch: float) -> None:
        """Remember the newest time we know was valid (only ever moves forward)."""
        epoch = int(epoch)
        if epoch > self.last_good:
            self.last_good = epoch
            self.db.set_meta("last_good_epoch", epoch)

    def confirm_now(self) -> None:
        """The user explicitly set/confirmed the time: trust it, even if earlier."""
        epoch = int(self.now())
        self.last_good = epoch
        self.db.set_meta("last_good_epoch", epoch)
