"""NTP-synced clock for precise booking-moment scheduling.

Local system clocks drift. For a scheduled run that must fire within
milliseconds of a known instant, we sync against an NTP server once at
startup to measure the local offset, then use that offset to produce a
corrected UTC time throughout the run.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone

import ntplib
import structlog

from tee_time_booker.constants import (
    CENTRAL,
    WEEKDAY_ADVANCE_DAYS,
    WEEKDAY_OPEN_HOUR,
    WEEKEND_OPEN_HOUR,
)

log = structlog.get_logger()

DEFAULT_NTP_SERVER = "time.google.com"


@dataclass(frozen=True)
class Clock:
    """A clock with an offset between the local system time and NTP-measured UTC.

    `offset_seconds` is added to local time to get the corrected time.
    """

    offset_seconds: float
    ntp_server: str

    def now_utc(self) -> datetime:
        return datetime.now(tz=timezone.utc) + timedelta(seconds=self.offset_seconds)

    def now_central(self) -> datetime:
        return self.now_utc().astimezone(CENTRAL)

    async def sleep_until(self, target_utc: datetime, *, spin_ms: int = 20) -> None:
        """Sleep until the corrected wall-clock reaches `target_utc`.

        Uses `asyncio.sleep` for the bulk of the wait, then a tight loop for
        the final `spin_ms` milliseconds to mitigate event-loop jitter. The
        tight loop briefly pins one core but is bounded at ~20 ms.
        """
        spin_seconds = spin_ms / 1000.0
        while True:
            remaining = (target_utc - self.now_utc()).total_seconds()
            if remaining <= spin_seconds:
                break
            await asyncio.sleep(remaining - spin_seconds)

        while self.now_utc() < target_utc:
            pass


async def sync_clock(server: str = DEFAULT_NTP_SERVER, *, attempts: int = 3) -> Clock:
    """Query NTP and return a Clock with the measured offset.

    `ntplib.NTPClient` is synchronous — run it in a thread so we don't block
    the event loop.
    """
    client = ntplib.NTPClient()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = await asyncio.to_thread(
                client.request, server, version=3, timeout=5
            )
            log.info(
                "ntp sync ok",
                server=server,
                offset_ms=response.offset * 1000,
                root_delay_ms=response.root_delay * 1000,
                attempt=attempt,
            )
            return Clock(offset_seconds=response.offset, ntp_server=server)
        except Exception as e:
            last_error = e
            log.warning("ntp sync attempt failed", server=server, attempt=attempt, error=str(e))
            if attempt < attempts:
                await asyncio.sleep(1)
    raise RuntimeError(f"NTP sync failed after {attempts} attempts: {last_error}")


def compute_booking_opens_at(target_date: date) -> datetime:
    """Return the UTC datetime at which the Add-to-Cart endpoint starts
    accepting claims for `target_date`.

    (The tee-time rows are always visible in the UI — what actually changes
    at this moment is that the Add-to-Cart button activates and the server
    starts accepting claim requests.)

    Rules (Austin muni, encoded here as the app's business logic):
      - Sat/Sun targets open on the preceding Monday at 8:00 PM Central.
      - Mon-Thu targets open 7 days in advance at 9:00 AM Central.
      - Fri targets: not formally documented by the platform; assume
        weekday rules (7 days in advance at 9 AM Central).
    """
    weekday = target_date.weekday()  # Monday=0, Sunday=6

    if weekday >= 5:  # Saturday or Sunday
        monday = target_date - timedelta(days=weekday)
        wall_clock = datetime.combine(
            monday, dtime(WEEKEND_OPEN_HOUR, 0), tzinfo=CENTRAL
        )
    else:
        open_date = target_date - timedelta(days=WEEKDAY_ADVANCE_DAYS)
        wall_clock = datetime.combine(
            open_date, dtime(WEEKDAY_OPEN_HOUR, 0), tzinfo=CENTRAL
        )

    return wall_clock.astimezone(timezone.utc)


async def _smoke_test() -> None:
    """Sync clock, report offset, demonstrate sleep_until precision, show booking-open moments."""
    clock = await sync_clock()

    print(f"\nClock synced against {clock.ntp_server}")
    print(f"  Offset:       {clock.offset_seconds * 1000:+.2f} ms from local")
    print(f"  Local time:   {datetime.now(tz=CENTRAL).isoformat(timespec='milliseconds')}")
    print(f"  Corrected:    {clock.now_central().isoformat(timespec='milliseconds')}")

    target = clock.now_utc() + timedelta(seconds=2)
    print(f"\nScheduling a wake at T+2s ({target.isoformat(timespec='milliseconds')})")
    t0 = time.perf_counter()
    await clock.sleep_until(target)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    overshoot_ms = (clock.now_utc() - target).total_seconds() * 1000
    print(f"  Elapsed:      {elapsed_ms:.2f} ms")
    print(f"  Overshoot:    {overshoot_ms:+.3f} ms (positive = fired late)")

    for label, d in [
        ("Saturday", date(2026, 5, 2)),
        ("Sunday", date(2026, 5, 3)),
        ("Wednesday", date(2026, 5, 6)),
        ("Friday", date(2026, 5, 8)),
    ]:
        rel = compute_booking_opens_at(d)
        print(f"\nTarget {d.isoformat()} ({label}) booking opens at:")
        print(f"  UTC:      {rel.isoformat(timespec='seconds')}")
        print(f"  Central:  {rel.astimezone(CENTRAL).isoformat(timespec='seconds')}")


if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    asyncio.run(_smoke_test())
