"""Booking-flow orchestration.

Pipeline: search → pick slot → claim (GET) → player selection (POST) →
advance to cart → load checkout form → (optional) finalize POST.

A caller in dry-run mode stops after loading the checkout form. The slot is
held in the cart during the 15-minute inactivity timeout, then released
automatically. No binding POST is ever sent unless `dry_run=False`.

All HTTP uses Playwright's `context.request` via BookingSession so every
request shares the exact network fingerprint of the browser that logged in.
"""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import structlog
from bs4 import BeautifulSoup

from tee_time_booker.constants import COURSES, MODULE, RESERVATION_TYPE
from tee_time_booker.search import TeeTimeSlot, _scrape_csrf, search
from tee_time_booker.session import BookingSession

log = structlog.get_logger()


@dataclass(frozen=True)
class CheckoutForm:
    """Scraped state of the checkout page's billing form, ready to POST."""

    action_url: str
    hidden_fields: dict[str, str]
    csrf_token: str


@dataclass
class BookingResult:
    slot: TeeTimeSlot | None = None
    dry_run: bool = True
    steps_completed: list[str] = field(default_factory=list)
    checkout_form: CheckoutForm | None = None
    confirmation_url: str | None = None


def _course_slug(display_name: str) -> str:
    for slug, name in COURSES.items():
        if name == display_name:
            return slug
    return display_name.lower().replace(" ", "_")


def pick_best_slot(
    slots: list[TeeTimeSlot], course_order: list[str]
) -> TeeTimeSlot | None:
    """Most-preferred course wins; within a course, the earliest tee time wins."""
    ranked = {slug: i for i, slug in enumerate(course_order)}
    eligible = [s for s in slots if _course_slug(s.course) in ranked]
    if not eligible:
        return None
    return min(eligible, key=lambda s: (ranked[_course_slug(s.course)], s.tee_time))


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


async def finalize_booking(
    session: BookingSession,
    checkout_form: CheckoutForm,
    *,
    bill_firstname: str,
    bill_lastname: str,
    bill_phone: str,
    bill_email: str,
    summary_hash: str = "0_0_0_0_0_0_0_0_0_0_0_0_0",
) -> str:
    """BINDING POST. Only call when the caller has explicitly opted in."""
    body = dict(checkout_form.hidden_fields)
    body["Action"] = "ProcessSale"
    body["SubAction"] = ""
    body["webcheckout_billfirstname"] = bill_firstname
    body["webcheckout_billlastname"] = bill_lastname
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


async def run_booking(session: BookingSession, plan, secrets, *, dry_run: bool = True) -> BookingResult:
    """Full pipeline. Stops before the binding POST when `dry_run=True`."""
    result = BookingResult(dry_run=dry_run)

    slots, csrf = await search(
        session,
        target_date=plan.target_date,
        earliest_time=plan.earliest_time,
        latest_time=plan.latest_time,
        num_players=plan.num_players,
        num_holes=plan.holes,
    )
    result.steps_completed.append("search")
    if not slots:
        raise RuntimeError("run_booking: no slots in window")

    chosen = pick_best_slot(slots, plan.courses_ranked())
    if chosen is None:
        raise RuntimeError("run_booking: no slots match preferred courses")
    result.slot = chosen
    result.steps_completed.append("pick")
    log.info(
        "run_booking: picked",
        course=chosen.course,
        tee_time=chosen.tee_time.isoformat(),
        grfmid=chosen.grfmid,
    )

    claim_html = await claim_slot(session, csrf, chosen, plan.num_players)
    result.steps_completed.append("claim")
    csrf = _scrape_csrf(claim_html) or csrf

    csrf = await submit_players(session, csrf, secrets.member_id, plan.num_players)
    result.steps_completed.append("players")

    csrf = await advance_to_cart(session, csrf)
    result.steps_completed.append("cart")

    result.checkout_form = await load_checkout_form(session, csrf)
    result.steps_completed.append("checkout")

    if dry_run:
        log.warning("run_booking: DRY RUN stop — slot held in cart; no binding POST")
        return result

    result.confirmation_url = await finalize_booking(
        session,
        result.checkout_form,
        bill_firstname=secrets.bill_firstname,
        bill_lastname=secrets.bill_lastname,
        bill_phone=secrets.bill_phone,
        bill_email=secrets.bill_email,
    )
    result.steps_completed.append("finalize")

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
