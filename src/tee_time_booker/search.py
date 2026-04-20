"""Build, fetch, and parse tee-time search results.

One search returns slots across all courses at the configured host for a given
date + time window + party size. Each slot has a unique id that identifies it
for the add-to-cart request.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse

import structlog
from bs4 import BeautifulSoup, Tag

from tee_time_booker.constants import MODULE, RESERVATION_TYPE

if TYPE_CHECKING:
    from tee_time_booker.session import BookingSession

log = structlog.get_logger()


@dataclass(frozen=True)
class TeeTimeSlot:
    course: str
    tee_time: datetime
    grfmid: str
    holes: int
    num_players_allowed: int


def build_search_url(
    base_url: str,
    csrf_token: str,
    *,
    target_date: date,
    earliest_time: time,
    num_players: int,
    num_holes: int,
) -> str:
    params = {
        "Action": "Start",
        "SubAction": "",
        "_csrf_token": csrf_token,
        "secondarycode": "",
        "begintime": earliest_time.strftime("%I:%M %p").lower().lstrip("0"),
        "begindate": target_date.strftime("%m/%d/%Y"),
        "numberofplayers": str(num_players),
        "numberofholes": str(num_holes),
        "search": "yes",
        "page": "1",
        "module": MODULE,
        "multiselectlist_value": "",
        "grwebsearch_buttonsearch": "yes",
    }
    return f"{base_url}/search.html?{urlencode(params)}"


async def search(
    session: "BookingSession",
    *,
    target_date: date,
    earliest_time: time,
    latest_time: time,
    num_players: int,
    num_holes: int,
    debug_dump: Path | None = None,
) -> tuple[list[TeeTimeSlot], str]:
    """Fetch + parse search results. Returns (slots, fresh_csrf_token).

    Filters client-side to slots whose tee_time is <= latest_time.
    The returned csrf_token is scraped from the response (may have rotated).
    """
    url = build_search_url(
        session.base_url,
        session.csrf_token,
        target_date=target_date,
        earliest_time=earliest_time,
        num_players=num_players,
        num_holes=num_holes,
    )
    log.info("fetching search results", url=url)
    resp = await session.get(url)
    if not resp.ok:
        raise RuntimeError(f"search: HTTP {resp.status}")

    html = resp.text
    if debug_dump:
        debug_dump.write_text(html)
        log.info("dumped search response", path=str(debug_dump))

    all_slots = _parse_results(html, target_date=target_date, num_holes=num_holes)
    in_window = [s for s in all_slots if s.tee_time.time() <= latest_time]
    fresh_csrf = _scrape_csrf(html) or session.csrf_token

    log.info(
        "search parsed",
        total_found=len(all_slots),
        in_time_window=len(in_window),
        fresh_csrf_differs=(fresh_csrf != session.csrf_token),
    )
    return in_window, fresh_csrf


def _parse_results(
    html: str, *, target_date: date, num_holes: int
) -> list[TeeTimeSlot]:
    """Extract every Add-to-Cart anchor and its surrounding course/time context."""
    soup = BeautifulSoup(html, "lxml")
    slots: list[TeeTimeSlot] = []

    for anchor in soup.select('a[href*="addtocart.html"][href*="GRFMIDList="]'):
        href = anchor.get("href", "")
        assert isinstance(href, str)
        qs = parse_qs(urlparse(href).query)
        grfmid = qs.get("GRFMIDList", [""])[0]
        num_slots = int(qs.get("GlobalSalesArea_GRNumSlots", ["0"])[0])
        res_type = qs.get("GlobalSalesArea_GRReservationType", [""])[0]

        if not grfmid or res_type != RESERVATION_TYPE:
            continue

        course, tee_time = _extract_row_context(anchor, target_date=target_date)
        if course is None or tee_time is None:
            log.warning(
                "could not extract course/time for slot",
                grfmid=grfmid,
                anchor_parent=str(anchor.parent)[:200] if anchor.parent else None,
            )
            continue

        slots.append(
            TeeTimeSlot(
                course=course,
                tee_time=tee_time,
                grfmid=grfmid,
                holes=num_holes,
                num_players_allowed=num_slots,
            )
        )

    return slots


def _extract_row_context(
    anchor: Tag, *, target_date: date
) -> tuple[str | None, datetime | None]:
    """Walk up from an Add-to-Cart anchor to its result row, pull course + time."""
    row = anchor
    for _ in range(8):
        parent = row.parent
        if parent is None:
            break
        row = parent
        if row.name in ("tr", "li", "article") or (
            row.name == "div"
            and any("result" in c or "row" in c or "slot" in c for c in row.get("class", []))
        ):
            break

    text = " ".join(row.get_text(" ", strip=True).split())

    course = _find_course(text)
    tee_time = _find_tee_time(text, target_date=target_date)
    return course, tee_time


def _find_course(text: str) -> str | None:
    from tee_time_booker.constants import COURSES

    for display_name in COURSES.values():
        if display_name.lower() in text.lower():
            return display_name
    return None


def _find_tee_time(text: str, *, target_date: date) -> datetime | None:
    import re

    match = re.search(r"\b(1[0-2]|0?[1-9]):([0-5]\d)\s*(am|pm|AM|PM)\b", text)
    if not match:
        return None
    try:
        t = datetime.strptime(match.group(0).upper().replace(" ", ""), "%I:%M%p").time()
        return datetime.combine(target_date, t)
    except ValueError:
        return None


def _scrape_csrf(html: str) -> str | None:
    """Find a fresh _csrf_token in the response HTML (prefer form hidden input)."""
    soup = BeautifulSoup(html, "lxml")

    hidden = soup.select_one('form input[type="hidden"][name="_csrf_token"]')
    if hidden and (val := hidden.get("value")):
        return val if isinstance(val, str) else None

    for a in soup.select("a[href*='_csrf_token=']"):
        href = a.get("href", "")
        if isinstance(href, str):
            tok = parse_qs(urlparse(href).query).get("_csrf_token", [""])[0]
            if tok:
                return tok
    return None


async def _smoke_test() -> None:
    """Log in, run one search against a plan file, print results."""
    import asyncio
    import sys
    from dotenv import load_dotenv

    from tee_time_booker.config import Secrets, load_plan
    from tee_time_booker.session import login

    load_dotenv()
    secrets = Secrets()  # type: ignore[call-arg]

    plan_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("plans/example.yaml")
    plan = load_plan(plan_path)
    log.info(
        "plan loaded",
        target=str(plan.target_date),
        window=f"{plan.earliest_time}-{plan.latest_time}",
        players=plan.num_players,
        holes=plan.holes,
    )

    async with await login(
        secrets.username,
        secrets.password.get_secret_value(),
        secrets.base_url,
    ) as sess:
        slots, fresh_csrf = await search(
            sess,
            target_date=plan.target_date,
            earliest_time=plan.earliest_time,
            latest_time=plan.latest_time,
            num_players=plan.num_players,
            num_holes=plan.holes,
            debug_dump=Path("/tmp/tee_time_booker-search-debug.html"),
        )

    print(f"\nFound {len(slots)} slots in window:")
    for s in sorted(slots, key=lambda s: (s.course, s.tee_time)):
        print(
            f"  {s.tee_time.strftime('%a %m/%d %I:%M %p')}  "
            f"{s.course:<18}  GRFMID={s.grfmid}  "
            f"holes={s.holes}  max_players={s.num_players_allowed}"
        )

    wanted = set(plan.courses_ranked())
    preferred = [s for s in slots if _slug(s.course) in wanted]
    print(f"\nOf which, {len(preferred)} match your preferred courses.")


def _slug(course_display: str) -> str:
    from tee_time_booker.constants import COURSES

    for slug, name in COURSES.items():
        if name == course_display:
            return slug
    return course_display.lower().replace(" ", "_")


if __name__ == "__main__":
    import asyncio

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    asyncio.run(_smoke_test())
