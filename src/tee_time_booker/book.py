"""Booking- and cancellation-flow orchestration.

Booking pipeline: search → pick slot → claim (GET) → player selection (POST) →
advance to cart → load checkout form → (optional) finalize POST.

Cancellation pipeline: cancel-search (POST) → add-cancellation-to-cart (GET) →
advance to cart → load checkout form → (optional) finalize POST. The two
pipelines merge at the cart step — the final POST is identical (Action=
ProcessSale), and the platform determines book-vs-cancel from cart contents.

In dry-run mode, both flows stop after loading the checkout form. The slot
is held in the cart during the 15-minute inactivity timeout, then released
automatically. No binding POST is ever sent unless `dry_run=False`.

All HTTP goes through Playwright's real browser network stack (page.goto and
in-page fetch) so every request shares the TLS/session fingerprint of the
browser that logged in.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import time as dtime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, TypeVar
from urllib.parse import parse_qs, urlencode, urlparse

import structlog
from bs4 import BeautifulSoup

from tee_time_booker.constants import COURSES, MODULE, RESERVATION_TYPE
from tee_time_booker.search import TeeTimeSlot, _scrape_csrf, search
from tee_time_booker.session import BookingSession

log = structlog.get_logger()

T = TypeVar("T")


async def with_retry(
    func: Callable[[], Awaitable[T]],
    *,
    label: str,
    attempts: int = 3,
    initial_delay_ms: int = 300,
    max_delay_ms: int = 1500,
) -> T:
    """Retry an async call with exponential backoff.

    Every exception is retried up to `attempts` times — WebTrac's errors
    mostly come back as HTTP status mismatches wrapped in RuntimeError, not
    fine-grained types, so we don't try to filter by exception class. The
    caller controls retry policy by not wrapping genuinely non-retriable
    operations (notably `finalize_booking`).
    """
    delay_ms = initial_delay_ms
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except Exception as e:
            last_error = e
            if attempt < attempts:
                log.warning(
                    "retry",
                    label=label,
                    attempt=attempt,
                    error=str(e),
                    next_delay_ms=delay_ms,
                )
                await asyncio.sleep(delay_ms / 1000)
                delay_ms = min(delay_ms * 2, max_delay_ms)
    assert last_error is not None
    raise last_error


@dataclass(frozen=True)
class CheckoutForm:
    """Scraped state of the checkout page's billing form, ready to POST."""

    action_url: str
    hidden_fields: dict[str, str]
    csrf_token: str


class BookingRunError(Exception):
    """Wraps any exception raised during a booking run, attaching the
    partial BookingResult so the caller can serialize failure diagnostics
    (failed_step, failed_url, queue stats, etc.) the same way it does for
    successful runs.

    Usage in callers:
        try:
            result = await run_scheduled_booking(...)
        except BookingRunError as e:
            result = e.partial_result   # has full diagnostic context
            error = e.__cause__         # the original exception
    """

    def __init__(self, message: str, partial_result: "BookingResult"):
        super().__init__(message)
        self.partial_result = partial_result


@dataclass
class BookingResult:
    slot: TeeTimeSlot | None = None
    dry_run: bool = True
    steps_completed: list[str] = field(default_factory=list)
    checkout_form: CheckoutForm | None = None
    confirmation_url: str | None = None
    # Per-player confirmation numbers (one per player). Populated after
    # finalize by scraping the My History account page — the only place
    # WebTrac exposes per-player numbers in HTML (the confirmation page
    # itself shows only the transaction receipt number). Needed for the
    # cancel flow. Empty list if scrape failed or dry-run.
    confirmation_numbers: list[str] = field(default_factory=list)

    # --- Diagnostic context (populated as the run progresses) ---
    # Search context — counts before / after time-window filter, and the top of
    # the ranked list. Useful for understanding why a booking did/didn't
    # find what it wanted.
    slots_total_found: int | None = None
    slots_in_window: int | None = None
    ranked_top: list[tuple[str, str]] = field(default_factory=list)  # [(course, "12:00 PM"), ...]

    # Queue context — populated by wait_for_queue_release (whether triggered
    # from enter_site or mid-keepalive). None if the run never touched the queue.
    queue_encountered: bool = False
    queue_wait_sec: float | None = None
    queue_id: str | None = None
    queue_headline_at_release: str | None = None
    # Queue-it spa-api status diagnostics. Phase 1 = pre-queue holding,
    # Phase 2 = release-in-progress. `phase2_start_utc` minus the official
    # 8:00 PM gate-open moment gives the server's Phase-1→2 transition lag
    # (observed ~30 s on 2026-05-25). `phase2_initial_progress` and
    # `phase2_last_progress` track our position-indicator at the start and
    # end of Phase 2. Full per-poll samples live on the structlog timeline.
    queue_phase1_sample_count: int | None = None
    queue_phase2_sample_count: int | None = None
    queue_phase2_start_utc: str | None = None
    queue_phase2_initial_progress: float | None = None
    queue_phase2_last_progress: float | None = None

    # Failure context — populated only on exception, captured before the
    # browser context closes so we know the bot's last-known state.
    failed_step: str | None = None
    failed_url: str | None = None
    failed_page_title: str | None = None

    # Timing breakdown — split out so we can see where the time went.
    time_in_keepalive_sec: float | None = None
    time_in_pipeline_sec: float | None = None


def _course_slug(display_name: str) -> str:
    for slug, name in COURSES.items():
        if name == display_name:
            return slug
    return display_name.lower().replace(" ", "_")


def _minutes_from_range(t: dtime, lo: dtime, hi: dtime) -> int:
    """Minutes between `t` and the [lo, hi] range. 0 if inside."""
    if lo <= t <= hi:
        return 0
    t_m = t.hour * 60 + t.minute
    if t < lo:
        return (lo.hour * 60 + lo.minute) - t_m
    return t_m - (hi.hour * 60 + hi.minute)


def rank_slots(
    slots: list[TeeTimeSlot],
    course_order: list[str],
    preferred_earliest: dtime | None = None,
    preferred_latest: dtime | None = None,
    course_downgrade_minutes: int = 25,
) -> list[TeeTimeSlot]:
    """Rank eligible slots by a single score; lower is better.

    With a preferred range set, each slot's score is:

        score = minutes_from_preferred_range
              + course_rank * course_downgrade_minutes

    Where `course_rank` is the slot's course position in `course_order`
    (top = 0). `course_downgrade_minutes` controls the tradeoff: it's the
    number of "off-preferred minutes" a slot at a lower-priority course
    is penalized per step down the list. Setting it to 0 makes ranking
    pure time-first; setting it large reproduces the old course-first
    behavior. Tiebreak: earliest tee_time first.

    Without a preferred range, falls back to (course_rank, tee_time).

    Slots whose course isn't in `course_order` are excluded.
    """
    ranked = {slug: i for i, slug in enumerate(course_order)}
    eligible = [s for s in slots if _course_slug(s.course) in ranked]

    if preferred_earliest is None or preferred_latest is None:
        return sorted(eligible, key=lambda s: (ranked[_course_slug(s.course)], s.tee_time))

    def score(s: TeeTimeSlot):
        distance = _minutes_from_range(s.tee_time.time(), preferred_earliest, preferred_latest)
        course_rank = ranked[_course_slug(s.course)]
        return (distance + course_rank * course_downgrade_minutes, s.tee_time)

    return sorted(eligible, key=score)


def rank_slots_multiday(
    slots: list[TeeTimeSlot],
    course_order: list[str],
    day_order: list,
    *,
    preferred_earliest: dtime | None = None,
    preferred_latest: dtime | None = None,
    course_downgrade_minutes: int = 25,
    day_fallback_trigger: str = "preferred",
) -> list[TeeTimeSlot]:
    """Rank slots that may span multiple days.

    Within a single day, slots are ranked by the existing `rank_slots`
    scorer (so the course-vs-time `course_downgrade_minutes` calibration is
    preserved exactly). Days are then ordered as a hard tier:

      - A day "qualifies" if it has at least one slot in the GATE window.
      - Gate window is the PREFERRED window when `day_fallback_trigger ==
        "preferred"` (and a preferred range is set); otherwise the gate is
        the whole result set, so any day with slots qualifies.
      - Qualifying days come first, in `day_order` order; non-qualifying
        days follow, in `day_order` order.

    The returned list concatenates each day's score-ranked slots in that day
    order. So: preferred day's morning slots first; if the preferred day has
    no morning slot, the other day's morning slots; and if no day has a
    morning slot, the preferred day's outer-window slots, then the other's.
    The downstream claim loop walks this list, so a fully-sniped preferred
    day naturally falls through to the next.

    A single-day `day_order` reduces exactly to `rank_slots` (one day, one
    group), leaving single-day behavior unchanged.
    """
    from collections import defaultdict

    by_day: dict = defaultdict(list)
    for s in slots:
        by_day[s.tee_time.date()].append(s)

    use_preferred_gate = (
        day_fallback_trigger == "preferred"
        and preferred_earliest is not None
        and preferred_latest is not None
    )

    def in_gate(s: TeeTimeSlot) -> bool:
        if not use_preferred_gate:
            return True
        return preferred_earliest <= s.tee_time.time() <= preferred_latest

    per_day_ranked: dict = {}
    qualifies: dict = {}
    for d in day_order:
        ranked_day = rank_slots(
            by_day.get(d, []),
            course_order,
            preferred_earliest=preferred_earliest,
            preferred_latest=preferred_latest,
            course_downgrade_minutes=course_downgrade_minutes,
        )
        per_day_ranked[d] = ranked_day
        qualifies[d] = any(in_gate(s) for s in ranked_day)

    ordered_days = (
        [d for d in day_order if qualifies[d]]
        + [d for d in day_order if not qualifies[d]]
    )
    out: list[TeeTimeSlot] = []
    for d in ordered_days:
        out.extend(per_day_ranked[d])
    return out


def pick_best_slot(
    slots: list[TeeTimeSlot], course_order: list[str]
) -> TeeTimeSlot | None:
    """Convenience: top-ranked slot, or None if none match."""
    ranked = rank_slots(slots, course_order)
    return ranked[0] if ranked else None


def _claim_succeeded(claim_html: str) -> bool:
    """True if the claim response shows the player-selection form — i.e., the
    slot was actually added to the cart. If the server rejected the claim
    (slot gone, session error, etc.), the form is absent."""
    return 'name="golfmemberselection_player1"' in claim_html


async def _claim_first_available(
    session: BookingSession,
    csrf_token: str,
    ranked_slots: list[TeeTimeSlot],
    num_players: int,
    *,
    max_attempts: int,
) -> tuple[TeeTimeSlot, str]:
    """Iterate preference-ranked slots, firing claim GETs until one succeeds.

    Returns (slot, claim_response_html) for the first slot whose claim lands a
    player-selection form. Falls through rejections fast — a "claim didn't
    add to cart" outcome is non-state-changing on the server, so it's safe to
    immediately attempt the next slot.

    Raises RuntimeError if all `max_attempts` candidates are rejected.
    """
    last_error: Exception | None = None
    for rank, slot in enumerate(ranked_slots[:max_attempts]):
        log.info(
            "attempting claim",
            rank=rank,
            course=slot.course,
            tee_time=slot.tee_time.isoformat(),
            grfmid=slot.grfmid,
        )
        try:
            claim_html = await with_retry(
                lambda s=slot: claim_slot(session, csrf_token, s, num_players),
                label=f"claim_slot[rank={rank}]",
                attempts=2,
                initial_delay_ms=50,
                max_delay_ms=100,
            )
        except Exception as e:
            log.warning("claim errored, trying next", rank=rank, error=str(e))
            last_error = e
            continue

        if _claim_succeeded(claim_html):
            log.info("claim accepted", rank=rank, course=slot.course)
            return slot, claim_html

        log.warning(
            "claim rejected (no player form in response), trying next",
            rank=rank,
            course=slot.course,
        )

    raise RuntimeError(
        f"all {max_attempts} ranked slots failed to claim"
        + (f"; last error: {last_error}" if last_error else "")
    )


def build_claim_url(
    base_url: str, csrf_token: str, slot: TeeTimeSlot, num_players: int
) -> str:
    params = {
        "Module": MODULE,
        "GRFMIDList": slot.grfmid,
        "FromProgram": "search",
        "GlobalSalesArea_GRNumSlots": str(num_players),
        "GlobalSalesArea_GRReservationType": RESERVATION_TYPE,
        "GlobalSalesArea_Reservee": "",
        "_csrf_token": csrf_token,
    }
    return f"{base_url}/addtocart.html?{urlencode(params)}"


async def claim_slot(
    session: BookingSession,
    csrf_token: str,
    slot: TeeTimeSlot,
    num_players: int,
) -> str:
    """Fire the addtocart GET — the request that claims a slot server-side."""
    url = build_claim_url(session.base_url, csrf_token, slot, num_players)
    log.info(
        "claim_slot: GET",
        grfmid=slot.grfmid,
        course=slot.course,
        tee_time=slot.tee_time.isoformat(),
    )
    resp = await session.get(url)
    log.info("claim_slot: response", status=resp.status, url=resp.url)
    if not resp.ok:
        raise RuntimeError(f"claim_slot: HTTP {resp.status}")
    return resp.text


async def submit_players(
    session: BookingSession, csrf_token: str, member_id: str, num_players: int
) -> str:
    """POST player selection as multipart/form-data.

    Playwright follows redirects by default, so the response here is the final
    page after the 302. We scrape a fresh CSRF from its HTML.
    """
    fields = {
        "Action": "Process",
        "SubAction": "",
        "_csrf_token": csrf_token,
    }
    for i in range(1, 6):
        fields[f"golfmemberselection_player{i}"] = (
            member_id if i <= num_players else "Skip"
        )
    fields["golfmemberselection_buttoncontinue"] = "yes"

    log.info("submit_players: POST", num_players=num_players)
    resp = await session.post_multipart(
        f"{session.base_url}/addtocart.html", fields
    )
    log.info("submit_players: response", status=resp.status, url=resp.url)
    if not resp.ok:
        raise RuntimeError(f"submit_players: HTTP {resp.status}")

    url_csrf = parse_qs(urlparse(resp.url).query).get("_csrf_token", [""])[0]
    html_csrf = _scrape_csrf(resp.text)
    return url_csrf or html_csrf or csrf_token


async def advance_to_cart(session: BookingSession, csrf_token: str) -> str:
    """Walk through the post-player-selection intermediate step and cart.html."""
    step2_url = (
        f"{session.base_url}/addtocart.html"
        f"?action=addtocart&subaction=start2&_csrf_token={csrf_token}"
    )
    resp = await session.get(step2_url)
    log.info("advance_to_cart: step2", status=resp.status)

    cart_url = f"{session.base_url}/cart.html?_csrf_token={csrf_token}"
    resp = await session.get(cart_url)
    log.info("advance_to_cart: cart", status=resp.status)
    if not resp.ok:
        raise RuntimeError(f"advance_to_cart: cart HTTP {resp.status}")

    return _scrape_csrf(resp.text) or csrf_token


async def load_checkout_form(session: BookingSession, csrf_token: str) -> CheckoutForm:
    """GET checkout.html, parse the billing form, return fields + fresh CSRF."""
    url = f"{session.base_url}/checkout.html?_csrf_token={csrf_token}"
    log.info("load_checkout_form: GET", url=url)
    resp = await session.get(url)
    if not resp.ok:
        raise RuntimeError(f"load_checkout_form: HTTP {resp.status}")

    soup = BeautifulSoup(resp.text, "lxml")
    form = soup.find("form", method=lambda v: v and v.lower() == "post")
    if form is None:
        raise RuntimeError("load_checkout_form: no POST <form> on checkout page")

    action = form.get("action", "")
    if not action:
        action = f"{session.base_url}/checkout.html"
    elif not action.startswith("http"):
        action = f"{session.base_url}/{action.lstrip('/')}"

    hidden: dict[str, str] = {}
    for inp in form.find_all("input", type="hidden"):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            hidden[name] = value

    form_csrf = hidden.get("_csrf_token", csrf_token)
    log.info(
        "load_checkout_form: parsed",
        action_url=action,
        hidden_count=len(hidden),
        csrf_differs=(form_csrf != csrf_token),
    )
    return CheckoutForm(action_url=action, hidden_fields=hidden, csrf_token=form_csrf)


async def scrape_confirmation_numbers(
    session: "BookingSession",
    slot: TeeTimeSlot,
) -> list[str]:
    """Fetch the My History account page and extract per-player confirmation
    numbers for the just-booked slot.

    Why this exists: WebTrac's `/confirmation.html` page (which `finalize_booking`
    lands on) only displays the transaction receipt number (e.g. 7316641),
    NOT the per-player confirmation numbers (e.g. 318107910..318107916 for
    a 4-player booking). The per-player numbers are required for cancellation
    via `webteetimecancel_confirmationnumber`. They live in two places:
    (a) the emailed PDF receipt, and (b) the My History account page where
    each booking row has a "Questions" button linking to
    `questioninfo.html?fmid=<per-player-confirmation-number>`. We scrape (b).

    Best-effort: returns [] on any failure. The booking has already committed
    by the time this is called, so scrape failure isn't catastrophic — just
    means Andrew has to dig through the email PDF if he wants to cancel.

    Verified against 5/11 (313093472,...479,...485,...489) and 5/25
    (318107910,...912,...914,...916) — both matched email PDFs exactly.
    """
    import re as _re

    from bs4 import BeautifulSoup

    url = (
        f"{session.base_url}/history.html"
        f"?historyoption=inquiry&_csrf_token={session.csrf_token}"
    )
    log.info("scrape_confirmation_numbers: GET", url=url)
    try:
        resp = await session.get(url)
    except Exception as e:
        log.warning("scrape_confirmation_numbers: GET failed", error=str(e))
        return []
    if not resp.ok:
        log.warning(
            "scrape_confirmation_numbers: non-OK response",
            status=resp.status,
            url=resp.url,
        )
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    rows = soup.select("table#webhistory_resultsoutput tbody tr")
    if not rows:
        log.warning("scrape_confirmation_numbers: no result rows in history table")
        return []

    # Build matchers tolerant to WebTrac's display quirks:
    # - Time format: "11:41 am" for 2-digit hours, " 7:10 am" (extra leading
    #   space) for 1-digit hours. Match with a regex anchored on "at\s+".
    # - Date: zero-padded mm/dd/yyyy, appears verbatim.
    # - Course: full display name appears as "on <Course> Golf Course".
    course_name = slot.course  # e.g. "Roy Kizer"
    date_str = slot.tee_time.strftime("%m/%d/%Y")
    time_str = slot.tee_time.strftime("%I:%M %p").lstrip("0").lower()
    time_re = _re.compile(r"at\s+" + _re.escape(time_str) + r",", _re.IGNORECASE)

    numbers: list[str] = []
    for tr in rows:
        desc_cell = tr.find("td", {"data-title": "Description"})
        status_cell = tr.find("td", {"data-title": "Status"})
        questions_cell = tr.find("td", {"data-title": "Question(s)"})
        if not (desc_cell and status_cell and questions_cell):
            continue

        desc = desc_cell.get_text(strip=False)
        status = status_cell.get_text(strip=True)

        if status != "Reserved":
            continue
        if course_name not in desc:
            continue
        if date_str not in desc:
            continue
        if not time_re.search(desc):
            continue

        a = questions_cell.find("a", href=True)
        if not a:
            continue
        m = _re.search(r"fmid=(\d+)", a["href"])
        if m:
            numbers.append(m.group(1))

    log.info(
        "scrape_confirmation_numbers: extracted",
        count=len(numbers),
        numbers=numbers,
        slot_course=slot.course,
        slot_tee_time=slot.tee_time.isoformat(),
    )
    return numbers


async def finalize_booking(
    session: BookingSession,
    checkout_form: CheckoutForm,
    *,
    bill_firstname: str,
    bill_lastname: str,
    bill_address1: str,
    bill_address2: str,
    bill_city: str,
    bill_state: str,
    bill_zip: str,
    bill_phone: str,
    bill_email: str,
    summary_hash: str = "0_0_0_0_0_0_0_0_0_0_0_0_0",
) -> str:
    """BINDING POST. Only call when the caller has explicitly opted in.

    Always submits the full billing-address set. The booking flow's server
    silently auto-fills missing fields for logged-in members, but the
    cancellation flow does not — sending the full set always is simpler and
    works for both.
    """
    body = dict(checkout_form.hidden_fields)
    body["Action"] = "ProcessSale"
    body["SubAction"] = ""
    body["webcheckout_billfirstname"] = bill_firstname
    body["webcheckout_billlastname"] = bill_lastname
    body["webcheckout_billaddress1"] = bill_address1
    body["webcheckout_billaddress2"] = bill_address2
    body["webcheckout_billcity"] = bill_city
    body["webcheckout_billstate"] = bill_state
    body["webcheckout_billzip"] = bill_zip
    body["webcheckout_billphone"] = bill_phone
    body["webcheckout_billemail"] = bill_email
    body["webcheckout_billemail_2"] = bill_email
    body["webcheckout_summaryvalueshash"] = summary_hash

    log.warning("finalize_booking: BINDING POST", action=body["Action"])
    resp = await session.post_form(checkout_form.action_url, body)
    log.info("finalize_booking: response", status=resp.status, url=resp.url)
    if not resp.ok:
        raise RuntimeError(f"finalize_booking: HTTP {resp.status}")
    return resp.url


async def run_booking(
    session: BookingSession,
    plan,
    secrets,
    *,
    dry_run: bool = True,
    result: BookingResult | None = None,
) -> BookingResult:
    """Full pipeline. Stops before the binding POST when `dry_run=True`.

    Non-binding steps retry with exponential backoff on transient failures.
    The finalize POST is never retried — the server may have committed even
    on a "failed" response, and a retry would risk a double-booking.

    A pre-existing `result` may be passed in (lets the caller pre-populate
    diagnostic context like timing). Otherwise a fresh one is created.
    """
    if result is None:
        result = BookingResult(dry_run=dry_run)

    # Search each target day (preferred first), pooling the results. Both
    # days of a weekend open at the same moment, so one run can chase both.
    # Each search is one extra round-trip on the critical path; accepted as
    # the cost of not being blind to the other day's morning inventory.
    day_order = plan.day_order()
    slots: list[TeeTimeSlot] = []
    total_found = 0
    csrf = session.csrf_token
    for d in day_order:
        # Thread the freshest CSRF into each search — the platform
        # occasionally rotates it per response (seen live 2026-06-08),
        # so day 2 must not reuse the login-era token.
        day_slots, day_total, csrf = await with_retry(
            lambda d=d: search(
                session,
                target_date=d,
                earliest_time=plan.earliest_time,
                latest_time=plan.latest_time,
                num_players=plan.num_players,
                num_holes=plan.holes,
                csrf_token=csrf,
            ),
            label=f"search[{d.isoformat()}]",
        )
        slots.extend(day_slots)
        total_found += day_total
    result.steps_completed.append("search")
    result.slots_total_found = total_found
    result.slots_in_window = len(slots)
    if not slots:
        raise RuntimeError("run_booking: no slots in window")

    ranked = rank_slots_multiday(
        slots,
        plan.courses_ranked(),
        day_order,
        preferred_earliest=plan.preferred_earliest,
        preferred_latest=plan.preferred_latest,
        course_downgrade_minutes=plan.course_downgrade_minutes,
        day_fallback_trigger=plan.day_fallback_trigger,
    )
    result.ranked_top = [
        (s.course, s.tee_time.strftime("%a %m/%d %I:%M %p")) for s in ranked[:5]
    ]
    if not ranked:
        raise RuntimeError("run_booking: no slots match preferred courses")
    log.info(
        "run_booking: ranked candidates",
        count=len(ranked),
        top=result.ranked_top,
    )

    # Try slots in preference order. First one whose claim lands a player-
    # selection form wins. If top pick is grabbed by a competing bot, fall
    # through to the next — the search already filtered by time window, so
    # every option here is acceptable.
    chosen, claim_html = await _claim_first_available(
        session, csrf, ranked, plan.num_players, max_attempts=min(5, len(ranked))
    )
    result.slot = chosen
    result.steps_completed.append("claim")
    csrf = _scrape_csrf(claim_html) or csrf

    csrf = await with_retry(
        lambda: submit_players(session, csrf, secrets.member_id, plan.num_players),
        label="submit_players",
    )
    result.steps_completed.append("players")

    csrf = await with_retry(
        lambda: advance_to_cart(session, csrf),
        label="advance_to_cart",
    )
    result.steps_completed.append("cart")

    result.checkout_form = await with_retry(
        lambda: load_checkout_form(session, csrf),
        label="load_checkout_form",
    )
    result.steps_completed.append("checkout")

    if dry_run:
        log.warning("run_booking: DRY RUN stop — slot held in cart; no binding POST")
        return result

    result.confirmation_url = await finalize_booking(
        session,
        result.checkout_form,
        bill_firstname=secrets.bill_firstname,
        bill_lastname=secrets.bill_lastname,
        bill_address1=secrets.bill_address1,
        bill_address2=secrets.bill_address2,
        bill_city=secrets.bill_city,
        bill_state=secrets.bill_state,
        bill_zip=secrets.bill_zip,
        bill_phone=secrets.bill_phone,
        bill_email=secrets.bill_email,
    )
    result.steps_completed.append("finalize")

    # Navigate the visible browser tab to the receipt page. The finalize POST
    # used an in-page fetch (not a navigation) so the tab is still rendering
    # the checkout form — without this nav, --keep-browser-open-sec leaves
    # the user staring at the previous step instead of the actual confirmation.
    if result.confirmation_url:
        try:
            log.info("navigating browser to confirmation page", url=result.confirmation_url)
            await session.get(result.confirmation_url)
        except Exception as e:
            # Booking is already committed server-side — failure to load the
            # receipt page is cosmetic, not catastrophic.
            log.warning(
                "could not navigate to confirmation page (booking still succeeded)",
                error=str(e),
            )

    # Scrape per-player confirmation numbers from the My History account
    # page. Best-effort: booking already committed; failure here just means
    # Andrew has to look in the email PDF if he wants to cancel later.
    if result.slot:
        try:
            result.confirmation_numbers = await scrape_confirmation_numbers(
                session, result.slot
            )
        except Exception as e:
            log.warning(
                "scrape_confirmation_numbers failed (booking still succeeded)",
                error=str(e),
            )

    return result


# ---------------------------------------------------------------------------
# Scheduled (booking-open-aware) entry point
# ---------------------------------------------------------------------------


async def run_scheduled_booking(
    plan,
    secrets,
    *,
    dry_run: bool = True,
    lead_time_sec: int = 30,
    headless: bool = False,
    keepalive_interval_sec: int = 90,
    keepalive_tight_interval_sec: int = 5,
    keepalive_tight_window_sec: int = 900,
    auth_lead_sec: int = 60,
    final_quiet_sec: int = 5,
    keep_browser_open_sec: int = 0,
    pre_auth: bool = False,
) -> BookingResult:
    """Run a booking, waiting until the moment booking opens before firing.

    Timeline (for long lead windows — traffic may route through a waiting room):
      1. Sync clock against NTP (~200 ms)
      2. Sleep until T - lead_time_sec
      3. Enter site (wait out waiting room if present) — acquires the 24h
         queue pass cookie; no login yet
      4. Keepalive loop until T - auth_lead_sec (splash refresh every
         keepalive_interval_sec)
      5. Authenticate at T - auth_lead_sec
      6. Quiet sleep until T - final_quiet_sec, then tight wait to T = 0
      7. Fire the booking pipeline

    For short lead windows (weekday opens with no waiting room, default
    lead_time_sec=30), steps 3-5 collapse into a single combined login
    (equivalent to the previous behavior).

    Deferring login until after the waiting-room wait — rather than logging
    in at T-60min and sitting idle for an hour — avoids an edge case where
    a mid-session redirect through the waiting room invalidates the auth
    session. Only the queue pass cookie needs to persist through the wait,
    and that's a 24h signed token scoped to the target date.

    If booking has already opened, fires immediately.
    """
    from tee_time_booker.clock import compute_booking_opens_at, sync_clock
    from tee_time_booker.session import enter_site, login

    opens_at = compute_booking_opens_at(plan.target_date)

    # Multi-day guard: every target day must open at the same moment, or a
    # single timed run can't serve them. (Sat+Sun of a weekend do; pairing a
    # Saturday with a weekday would not.)
    opens_by_day = {d: compute_booking_opens_at(d) for d in plan.day_order()}
    distinct = set(opens_by_day.values())
    if len(distinct) > 1:
        detail = ", ".join(
            f"{d.isoformat()}→{o.isoformat()}" for d, o in opens_by_day.items()
        )
        raise ValueError(
            "all target dates must open at the same booking moment for one run; "
            f"got differing moments: {detail}"
        )

    clock = await sync_clock()
    now = clock.now_utc()

    log.info(
        "scheduled run",
        target_date=plan.target_date.isoformat(),
        search_dates=[d.isoformat() for d in plan.day_order()],
        opens_at_utc=opens_at.isoformat(),
        now_utc=now.isoformat(),
        offset_ms=clock.offset_seconds * 1000,
        lead_time_sec=lead_time_sec,
    )

    result = BookingResult(dry_run=dry_run)

    if now >= opens_at:
        log.warning(
            "booking has already opened, firing immediately",
            past_by_seconds=(now - opens_at).total_seconds(),
        )
        async with await login(
            secrets.username,
            secrets.password.get_secret_value(),
            secrets.base_url,
            headless=headless,
        ) as session:
            result = await _run_booking_with_diagnostics(
                session, plan, secrets, dry_run=dry_run, result=result,
            )
            await _linger(keep_browser_open_sec)
            return result

    enter_at = opens_at - timedelta(seconds=lead_time_sec)
    now = clock.now_utc()
    if now < enter_at:
        wait_s = (enter_at - now).total_seconds()
        log.info("waiting until site-entry moment", wait_seconds=wait_s)
        await clock.sleep_until(enter_at)

    # Short-lead path: lead_time_sec is inside the auth_lead_sec window.
    # Combined login in one shot (preserves the weekday default behavior).
    if lead_time_sec <= auth_lead_sec:
        async with await login(
            secrets.username,
            secrets.password.get_secret_value(),
            secrets.base_url,
            headless=headless,
        ) as session:
            return await _wait_and_book(
                session, plan, secrets, clock, opens_at,
                final_quiet_sec=final_quiet_sec,
                dry_run=dry_run,
                keep_browser_open_sec=keep_browser_open_sec,
                result=result,
            )

    # Long-lead path: enter site, keepalive through activation/queue, then book.
    #
    # Two login strategies:
    #   - Deferred (default): stay anonymous through the keepalive + queue,
    #     authenticate at T-auth_lead_sec. Original design — minimizes the
    #     window in which a queue redirect could disturb an authed session.
    #   - Pre-auth (pre_auth=True): log in immediately at site entry, hold the
    #     authenticated session through keepalive + the queue, verify it
    #     survived afterwards (re-auth if not). Removes the login step from the
    #     post-queue critical path. Experimental — testing whether being
    #     authenticated before activation changes anything.
    import time as _time
    async with await enter_site(secrets.base_url, headless=headless) as session:
        auth_at = opens_at - timedelta(seconds=auth_lead_sec)
        t_keepalive_start = _time.monotonic()

        if pre_auth:
            log.info("pre-auth: authenticating early, before keepalive/queue")
            await session.authenticate(
                secrets.username, secrets.password.get_secret_value()
            )

        now = clock.now_utc()
        if now < auth_at and (auth_at - now).total_seconds() > keepalive_interval_sec:
            wait_s = (auth_at - now).total_seconds()
            log.info(
                "keepalive until auth/booking moment",
                wait_seconds=round(wait_s, 1),
                interval_sec=keepalive_interval_sec,
                auth_lead_sec=auth_lead_sec,
                pre_auth=pre_auth,
            )
            await session.keepalive(
                clock, auth_at,
                interval_sec=keepalive_interval_sec,
                tight_interval_sec=keepalive_tight_interval_sec,
                tight_window_sec=keepalive_tight_window_sec,
            )
        result.time_in_keepalive_sec = round(_time.monotonic() - t_keepalive_start, 1)

        if pre_auth:
            # Verify the early login survived the queue passage; recover if not.
            await session.ensure_authenticated(
                secrets.username, secrets.password.get_secret_value()
            )
        else:
            # Deferred login: authenticate now, just before booking opens.
            remaining = (auth_at - clock.now_utc()).total_seconds()
            if remaining > 0:
                await clock.sleep_until(auth_at)
            log.info("auth moment reached; logging in")
            await session.authenticate(
                secrets.username, secrets.password.get_secret_value()
            )

        return await _wait_and_book(
            session, plan, secrets, clock, opens_at,
            final_quiet_sec=final_quiet_sec,
            dry_run=dry_run,
            keep_browser_open_sec=keep_browser_open_sec,
            result=result,
        )


async def _wait_and_book(
    session,
    plan,
    secrets,
    clock,
    opens_at,
    *,
    final_quiet_sec: int,
    dry_run: bool,
    keep_browser_open_sec: int = 0,
    result: BookingResult | None = None,
) -> BookingResult:
    """Quiet wait until booking opens, then fire the booking pipeline."""
    wait_s = (opens_at - clock.now_utc()).total_seconds()
    log.info("waiting until booking opens", wait_seconds=round(wait_s, 3))
    await clock.sleep_until(opens_at)

    log.info("booking open; firing pipeline")
    result = await _run_booking_with_diagnostics(
        session, plan, secrets, dry_run=dry_run, result=result,
    )
    await _linger(keep_browser_open_sec)
    return result


async def _run_booking_with_diagnostics(
    session,
    plan,
    secrets,
    *,
    dry_run: bool,
    result: BookingResult | None = None,
) -> BookingResult:
    """Run the booking pipeline, decorating the result with diagnostic context
    on both success and failure.

    Must be called inside the BookingSession's `async with` so that on failure
    we can read `session._page` (URL + title) before the browser closes.

    On any internal exception, wraps it as `BookingRunError` carrying the
    partial result — caller can introspect `e.partial_result` for full
    failure context (failed_step, failed_url, queue stats, etc.).
    """
    import time

    if result is None:
        result = BookingResult(dry_run=dry_run)

    t_start = time.monotonic()
    try:
        await run_booking(session, plan, secrets, dry_run=dry_run, result=result)
    except Exception as e:
        # Capture failure context BEFORE the BookingSession context closes
        # the browser. Once we re-raise, the browser is gone.
        result.failed_step = (
            result.steps_completed[-1] if result.steps_completed else "before search"
        )
        try:
            result.failed_url = session._page.url
            result.failed_page_title = await session._page.title()
        except Exception:
            # Browser may already be in a bad state; don't mask the original error.
            pass
        # Promote queue stats + timing onto the result so they're available
        # to the caller via e.partial_result.
        _promote_session_diagnostics(session, result, t_start)
        raise BookingRunError(str(e), result) from e

    _promote_session_diagnostics(session, result, t_start)
    return result


def _promote_session_diagnostics(session, result: BookingResult, t_start: float) -> None:
    """Copy queue stats from the session and pipeline timing onto the result.
    Idempotent — safe to call from both success and failure paths."""
    import time

    result.time_in_pipeline_sec = round(time.monotonic() - t_start, 1)
    if session.queue_stats:
        stats = session.queue_stats
        result.queue_encountered = True
        result.queue_wait_sec = stats.get("wait_sec")
        result.queue_id = stats.get("queue_id")
        result.queue_headline_at_release = stats.get("headline_at_release")
        result.queue_phase1_sample_count = stats.get("phase1_sample_count")
        result.queue_phase2_sample_count = stats.get("phase2_sample_count")
        result.queue_phase2_start_utc = stats.get("phase2_start_utc")
        result.queue_phase2_initial_progress = stats.get("phase2_initial_progress")
        result.queue_phase2_last_progress = stats.get("phase2_last_progress")


async def _linger(seconds: int) -> None:
    """Pause before letting the browser context manager close.

    Useful for inspecting the confirmation page / DevTools after a successful
    real run. Set via `--keep-browser-open-sec`. Default 0 = close immediately.
    """
    if seconds <= 0:
        return
    log.warning(
        "keeping browser open before exit (inspect DevTools / receipt now)",
        duration_sec=seconds,
    )
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Cancellation flow
# ---------------------------------------------------------------------------


async def cancel_search(
    session: BookingSession,
    csrf_token: str,
    confirmation_numbers: list[str],
    tee_time: dtime,
) -> str:
    """POST teetimecancel.html with confirmation numbers and tee-time.

    The platform expects a single comma-separated `confirmationnumber` field
    and the tee-time split across three fields: hour (1-12), minute, AM/PM.
    """
    hour = tee_time.strftime("%I").lstrip("0") or "12"
    minute = tee_time.strftime("%M")
    ampm = tee_time.strftime("%p")

    fields = {
        "Action": "Process",
        "SubAction": "",
        "_csrf_token": csrf_token,
        "webteetimecancel_confirmationnumber": ",".join(confirmation_numbers),
        "webteetimecancel_teetimeslot1": hour,
        "webteetimecancel_teetimeslot2": minute,
        "webteetimecancel_teetimeslot3": ampm,
        "webteetimecancel_buttonsearch": "yes",
    }
    log.info(
        "cancel_search: POST",
        confirmation_numbers=confirmation_numbers,
        tee_time=tee_time.isoformat(timespec="minutes"),
    )
    resp = await session.post_form(f"{session.base_url}/teetimecancel.html", fields)
    log.info("cancel_search: response", status=resp.status, url=resp.url)
    if not resp.ok:
        raise RuntimeError(f"cancel_search: HTTP {resp.status}")
    return resp.text


async def add_cancellation_to_cart(
    session: BookingSession, csrf_token: str, confirmation_numbers: list[str]
) -> str:
    """GET addtocart.html?action=cancellation — populates the cart with cancel items.

    The platform redirects this through addtocart?subaction=start2 and lands
    on cart.html; page.goto follows the chain and returns the final cart HTML.
    """
    params = {
        "action": "cancellation",
        "fmidlist": ",".join(f"GR{n}" for n in confirmation_numbers),
        "module": MODULE,
        "SADetailIDList": ",".join(confirmation_numbers),
        "_csrf_token": csrf_token,
    }
    url = f"{session.base_url}/addtocart.html?{urlencode(params)}"
    log.info("add_cancellation_to_cart: GET", url=url)
    resp = await session.get(url)
    log.info(
        "add_cancellation_to_cart: response", status=resp.status, final_url=resp.url
    )
    if not resp.ok:
        raise RuntimeError(f"add_cancellation_to_cart: HTTP {resp.status}")
    return resp.text


async def run_cancellation(
    session: BookingSession,
    confirmation_numbers: list[str],
    tee_time: dtime,
    secrets,
    *,
    dry_run: bool = True,
) -> BookingResult:
    """Full cancellation pipeline. Stops before the binding POST when dry_run=True.

    In dry-run, cancel items are added to the cart and held during the 15-min
    inactivity timeout, then cleared by the platform. The reservation remains
    active.
    """
    result = BookingResult(dry_run=dry_run)
    csrf = session.csrf_token

    search_html = await with_retry(
        lambda: cancel_search(session, csrf, confirmation_numbers, tee_time),
        label="cancel_search",
    )
    result.steps_completed.append("cancel_search")
    csrf = _scrape_csrf(search_html) or csrf

    cart_html = await with_retry(
        lambda: add_cancellation_to_cart(session, csrf, confirmation_numbers),
        label="add_cancellation_to_cart",
    )
    result.steps_completed.append("cancel_claim")
    csrf = _scrape_csrf(cart_html) or csrf

    result.checkout_form = await with_retry(
        lambda: load_checkout_form(session, csrf),
        label="load_checkout_form",
    )
    result.steps_completed.append("checkout")

    if dry_run:
        log.warning("run_cancellation: DRY RUN stop — cancellation in cart; no binding POST")
        return result

    result.confirmation_url = await finalize_booking(
        session,
        result.checkout_form,
        bill_firstname=secrets.bill_firstname,
        bill_lastname=secrets.bill_lastname,
        bill_address1=secrets.bill_address1,
        bill_address2=secrets.bill_address2,
        bill_city=secrets.bill_city,
        bill_state=secrets.bill_state,
        bill_zip=secrets.bill_zip,
        bill_phone=secrets.bill_phone,
        bill_email=secrets.bill_email,
    )
    result.steps_completed.append("finalize")

    if result.confirmation_url:
        try:
            log.info("navigating browser to confirmation page", url=result.confirmation_url)
            await session.get(result.confirmation_url)
        except Exception as e:
            log.warning(
                "could not navigate to confirmation page (cancellation still succeeded)",
                error=str(e),
            )

    return result


async def _smoke_test() -> None:
    """Run a booking end-to-end. Defaults to dry-run; set BOOK_FOR_REAL=1 to commit."""
    import os
    import sys

    from dotenv import load_dotenv

    from tee_time_booker.config import Secrets, load_plan
    from tee_time_booker.session import login

    load_dotenv()
    secrets = Secrets()  # type: ignore[call-arg]

    plan_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("plans/example.yaml")
    plan = load_plan(plan_path)
    dry_run = os.getenv("BOOK_FOR_REAL", "0") != "1"

    if not dry_run:
        print("!! BOOK_FOR_REAL=1 set — this will COMMIT a real reservation. !!")

    keep_open = os.getenv("KEEP_BROWSER_OPEN", "0") == "1"

    async with await login(
        secrets.username,
        secrets.password.get_secret_value(),
        secrets.base_url,
    ) as session:
        result = await run_booking(session, plan, secrets, dry_run=dry_run)

        if keep_open:
            import asyncio
            print("\n(Browser kept open — inspect the page, then press Enter here to close.)")
            await asyncio.to_thread(input)

    header = "=== DRY RUN COMPLETE ===" if dry_run else "=== BOOKING COMMITTED ==="
    print(f"\n{header}")
    print(f"Steps completed: {' → '.join(result.steps_completed)}")
    if result.slot:
        print(
            f"Slot chosen:     {result.slot.course} @ "
            f"{result.slot.tee_time.strftime('%a %m/%d %I:%M %p')}  "
            f"(GRFMID {result.slot.grfmid})"
        )
    if result.checkout_form:
        cf = result.checkout_form
        print(f"Checkout form:   action={cf.action_url}")
        print(f"                 hidden fields: {sorted(cf.hidden_fields.keys())}")
    if result.confirmation_url:
        print(f"Confirmation:    {result.confirmation_url}")
    print()
    if dry_run:
        print("The slot is now held in your cart. It will auto-release after 15 min of")
        print("inactivity, or you can sign in to the platform and remove it manually.")
    else:
        print("A real reservation was just created. You should receive a confirmation email.")


if __name__ == "__main__":
    import asyncio

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    asyncio.run(_smoke_test())
