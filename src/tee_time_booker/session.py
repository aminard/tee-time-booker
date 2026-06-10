"""Playwright-based session. Login runs interactively, then subsequent HTTP
requests use the browser's real network stack via `page.goto` (for GETs) and
in-page `fetch()` via `page.evaluate` (for POSTs).

Using the page navigation stack — rather than Playwright's separate
`context.request` API — is what actually shares TLS + cookies + any
additional state the server expects from a real browser.

Some booking-open windows run behind a virtual waiting room. When the
platform redirects us to it, we wait for the queue to release us
automatically (detected via URL leaving the queue host). Once released,
a signed pass cookie is set on the platform domain and persists for
~24 hours, so subsequent navigations in the same browser context sail
through.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date as ddate, datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Virtual waiting room handling
# ---------------------------------------------------------------------------

QUEUE_HOST = "queue-it.net"


def is_in_queue(page: Page) -> bool:
    """True if the page's current URL is a virtual waiting room."""
    return QUEUE_HOST in (page.url or "")


async def has_queue_pass(context: BrowserContext, target_date: ddate) -> bool:
    """True if the context holds a signed pass cookie for the booking-open event.

    The pass is issued by the waiting room and set on the platform domain
    when it releases us. Name format observed: `QueueITAccepted-*_txaustin{YYYYMMDD}`.
    """
    suffix = f"txaustin{target_date.strftime('%Y%m%d')}"
    for c in await context.cookies():
        name = c.get("name", "")
        if name.startswith("QueueITAccepted-") and name.endswith(suffix):
            return True
    return False


async def _scrape_queue_metadata(page: Page) -> dict[str, str]:
    """Pull diagnostic fields off the waiting-room page.

    The page exposes some human-readable state (status-updated timestamp,
    queue id, headline) that's useful for understanding how the queue is
    progressing — especially helpful when iterating on queue behavior
    between release events, since we only see one event per week.
    """
    try:
        body_text = await page.evaluate("document.body.innerText")
    except Exception as e:
        return {"scrape_error": str(e)}

    meta: dict[str, str] = {}

    m = re.search(r"Status last updated:\s*([^\r\n]+)", body_text)
    if m:
        meta["status_updated"] = m.group(1).strip()

    m = re.search(r"Queue ID:?\s*([0-9a-f-]{20,})", body_text)
    if m:
        meta["queue_id"] = m.group(1).strip()

    # First non-empty line often contains the event headline (e.g. "Tee Time
    # Launch 04/27 - You're in line..."). Useful if we want to confirm
    # we're in the right event's queue.
    for line in body_text.splitlines():
        line = line.strip()
        if line and len(line) < 200:
            meta["headline"] = line
            break

    return meta


async def wait_for_queue_release(
    page: Page,
    *,
    poll_interval_sec: float = 2.0,
    log_interval_sec: float = 30.0,
    timeout_sec: int = 14400,
) -> dict:
    """Block until the page navigates out of the virtual waiting room.

    The waiting room JS polls its own status endpoint and triggers a full
    redirect back to the target URL when our turn is up, so we just watch
    the page URL. Progress is logged every ~30 sec for observability,
    including any queue metadata we can scrape off the page (status
    timestamp, queue id) so we have a richer dataset to reason about.

    We also attach a response listener to capture Queue-it's spa-api
    status poll bodies (numeric `progress`, `QueueState`, etc.) as they
    arrive. These are the only direct measurement of our position-in-queue
    over time — the page doesn't render position info, but the JSON does.
    Captured samples flow back in the return dict for the caller to
    persist.

    The 4 h default timeout is deliberately loose. The right frame isn't
    "how long am I willing to sit in the queue" but "how long past the
    booking-open moment am I willing to keep trying to book." Arriving
    hours early for data-gathering + being willing to wait ~1-2 h past
    opening (slots can linger) adds up to ~4 h. The 24 h queue-pass
    cookie means once through, subsequent navigations are cheap. Better
    to try and fail on stale slots than to give up before the queue
    even clears.
    """
    loop = asyncio.get_event_loop()
    start = loop.time()
    last_log = start
    initial_meta = await _scrape_queue_metadata(page)
    last_meta = initial_meta
    log.warning(
        "virtual waiting room: entered",
        url=page.url,
        title=await page.title(),
        **initial_meta,
    )

    # Capture Queue-it status poll responses for the duration of the wait.
    # The page's own JS polls every ~30 s in Phase 1 (holding) and every
    # ~2 s in Phase 2 (releasing); each response body carries `progress`
    # and `QueueState` which are the only signals we get about our actual
    # position. We listen on the page event and parse bodies as they
    # arrive, appending to `samples` for later return.
    samples: list[dict] = []

    async def _capture(response):
        url = response.url
        if "spa-api/queue/" not in url or "/status" not in url:
            return
        try:
            body = await response.json()
        except Exception as e:
            log.debug("queue status: parse failed", error=str(e))
            return
        ticket = body.get("ticket") or {}
        sample = {
            "t_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_sec": round(loop.time() - start, 2),
            "phase": body.get("QueueState"),
            "is_before_or_idle": body.get("isBeforeOrIdle"),
            "forecast": body.get("forecastStatus"),
            "progress": ticket.get("progress"),
            "seconds_to_start": ticket.get("secondsToStart"),
            "update_interval_ms": body.get("updateInterval"),
        }
        samples.append(sample)
        log.info("queue status sample", **sample)

    def _on_response(response):
        # page.on() takes a sync callback; spawn a task for the async work.
        asyncio.create_task(_capture(response))

    page.on("response", _on_response)
    try:
        while True:
            if not is_in_queue(page):
                wait_seconds = round(loop.time() - start, 1)
                log.info(
                    "virtual waiting room: released",
                    wait_seconds=wait_seconds,
                    url=page.url,
                    samples_captured=len(samples),
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass

                # Derive summary stats from captured samples.
                phase2 = [s for s in samples if s["phase"] == 2]
                phase2_start = phase2[0] if phase2 else None
                phase2_end = phase2[-1] if phase2 else None
                return {
                    "wait_sec": wait_seconds,
                    "queue_id": last_meta.get("queue_id") or initial_meta.get("queue_id"),
                    "headline_at_release": last_meta.get("headline"),
                    "headline_at_entry": initial_meta.get("headline"),
                    "status_samples": samples,
                    "phase2_start_utc": phase2_start["t_utc"] if phase2_start else None,
                    "phase2_initial_progress": (
                        phase2_start["progress"] if phase2_start else None
                    ),
                    "phase2_last_progress": (
                        phase2_end["progress"] if phase2_end else None
                    ),
                    "phase2_sample_count": len(phase2),
                    "phase1_sample_count": sum(1 for s in samples if s["phase"] == 1),
                }

            elapsed = loop.time() - start
            if elapsed >= timeout_sec:
                raise RuntimeError(
                    f"virtual waiting room: timed out after {timeout_sec}s (still on {page.url})"
                )
            if loop.time() - last_log >= log_interval_sec:
                last_meta = await _scrape_queue_metadata(page)
                log.info(
                    "virtual waiting room: still waiting",
                    wait_seconds=round(elapsed, 1),
                    url=page.url,
                    **last_meta,
                )
                last_log = loop.time()
            await asyncio.sleep(poll_interval_sec)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass


@dataclass
class Response:
    """Minimal response shape returned by BookingSession's HTTP helpers."""

    status: int
    url: str
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class BookingSession:
    """A browser session for the platform. Starts unauthenticated (after
    `enter_site()`) and is populated with auth state by `authenticate()`.

    Use as an async context manager so the underlying Playwright resources
    are cleaned up deterministically.
    """

    base_url: str
    _pw_mgr: Any = field(repr=False)
    _browser: Browser = field(repr=False)
    _context: BrowserContext = field(repr=False)
    _page: Page = field(repr=False)
    # Populated by `authenticate()`. Empty until then.
    csrf_token: str = ""
    user_agent: str = ""
    # Populated by enter_site() or keepalive() if we ever pass through the
    # virtual waiting room. None means we never hit the queue this run.
    queue_stats: dict | None = None

    @property
    def authenticated(self) -> bool:
        return bool(self.csrf_token)

    async def __aenter__(self) -> "BookingSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self._browser.close()
        finally:
            await self._pw_mgr.__aexit__(None, None, None)

    async def get(self, url: str, *, wait_until: str = "domcontentloaded") -> Response:
        """Navigate the live page to `url` and return the final status + HTML.

        Default waits only for domcontentloaded: WebTrac pages are fully
        server-rendered, so the HTML we parse (forms, hidden fields, CSRF)
        is complete as soon as the document arrives. The old networkidle
        default added a ≥500 ms "no traffic for half a second" settle tax
        per navigation — ~6 navigations on the booking hot path — waiting
        on images/analytics we never read. Pass wait_until="networkidle"
        explicitly if a future call ever needs JS to finish."""
        response = await self._page.goto(url, wait_until=wait_until)
        status = response.status if response else 0
        return Response(
            status=status,
            url=self._page.url,
            text=await self._page.content(),
        )

    async def get_fetch(self, url: str) -> Response:
        """GET via in-page fetch() — same browser network stack as `get()`,
        but no page navigation. Two advantages on the hot path: no
        networkidle settle-wait, and multiple GETs can run concurrently
        (page.goto is inherently one-at-a-time). Used for searches."""
        return await self._fetch(url, body_type=None, fields=None)

    async def post_multipart(self, url: str, fields: dict[str, str]) -> Response:
        """Submit a multipart/form-data POST via in-page fetch()."""
        return await self._fetch(url, body_type="multipart", fields=fields)

    async def post_form(self, url: str, fields: dict[str, str]) -> Response:
        """Submit an application/x-www-form-urlencoded POST via in-page fetch()."""
        return await self._fetch(url, body_type="urlencoded", fields=fields)

    async def keepalive(
        self,
        clock: Any,
        deadline_utc: datetime,
        *,
        interval_sec: int = 90,
        tight_interval_sec: int | None = None,
        tight_window_sec: int = 900,
    ) -> None:
        """Refresh splash.html periodically until `deadline_utc`.

        Purpose: two-fold.
          (1) Keep short-TTL platform cookies (Cloudflare bot-management etc.)
              from expiring during long idle waits.
          (2) Detect virtual-waiting-room activation during the idle period;
              if a refresh lands us in the queue, we wait for release.

        Adaptive cadence: if `tight_interval_sec` is set, the loop uses that
        tighter interval for the last `tight_window_sec` seconds *before
        deadline_utc*, and `interval_sec` outside that window. This anchors
        the tight period to the expected activation moment regardless of
        how long the bot has been running — important when lead time is
        long (e.g. 60+ min) and a fixed "tight for the first N seconds"
        window would expire before activation happens. Pass None to use a
        flat `interval_sec` throughout.

        `clock` is a tee_time_booker.clock.Clock (offset-corrected NTP wrapper).
        """
        splash_url = f"{self.base_url}/splash.html"
        log.info(
            "keepalive: starting",
            deadline_utc=deadline_utc.isoformat(),
            interval_sec=interval_sec,
            tight_interval_sec=tight_interval_sec,
            tight_window_sec=tight_window_sec if tight_interval_sec else None,
        )
        while True:
            now = clock.now_utc()
            if now >= deadline_utc:
                log.info("keepalive: deadline reached")
                return

            # Pick the active interval based on remaining time to deadline.
            remaining_sec = (deadline_utc - now).total_seconds()
            if tight_interval_sec is not None and remaining_sec <= tight_window_sec:
                active_interval = tight_interval_sec
            else:
                active_interval = interval_sec

            next_tick = min(now + timedelta(seconds=active_interval), deadline_utc)
            await clock.sleep_until(next_tick)

            if clock.now_utc() >= deadline_utc:
                log.info("keepalive: deadline reached")
                return

            try:
                log.info("keepalive: refreshing splash", url=splash_url)
                await self._page.goto(splash_url, wait_until="networkidle")
                if is_in_queue(self._page):
                    log.warning(
                        "keepalive: virtual waiting room encountered mid-session"
                    )
                    stats = await wait_for_queue_release(self._page)
                    # Stash stats so the caller can promote them to BookingResult.
                    # If we already have stats from an earlier queue pass this
                    # run (rare), keep the latest.
                    self.queue_stats = stats
                    # Queue may have bounced us to a logged-out state. Detect
                    # and log — v1 doesn't re-login automatically.
                    login_form = self._page.locator('input[name="weblogin_username"]')
                    if await login_form.count() > 0:
                        log.error(
                            "keepalive: login form visible after queue release — "
                            "session lost; subsequent booking calls will likely fail",
                        )
                else:
                    log.info(
                        "keepalive: refresh ok",
                        title=await self._page.title(),
                        url=self._page.url,
                    )
            except Exception as e:
                log.warning("keepalive: refresh errored", error=str(e))

    async def authenticate(self, username: str, password: str) -> None:
        """Fill the login form on the currently-displayed page and log in.

        Assumes the page is already parked on splash.html (the public landing
        page, which for anonymous users displays the login form). `enter_site()`
        and `keepalive()` both leave the page there, so this is the common
        state after arriving or idling through a waiting room.

        Populates `self.csrf_token` and `self.user_agent` on success.
        """
        page = self._page

        # Edge case: if the session just bounced out of a virtual waiting room,
        # we might need a beat for the login form to render.
        username_input = page.locator('input[name="weblogin_username"]')
        try:
            await username_input.wait_for(state="visible", timeout=15_000)
        except Exception:
            log.error(
                "authenticate: login form did not appear",
                url=page.url,
                title=await page.title(),
                body_snippet=(await page.content())[:500],
            )
            raise

        log.info("authenticate: filling credentials")
        await username_input.fill(username)
        await page.fill('input[name="weblogin_password"]', password)

        async with page.expect_navigation(wait_until="networkidle"):
            await page.click(
                'button[type="submit"], input[type="submit"], '
                'button[name="weblogin_buttonlogin"], '
                'input[name="weblogin_buttonlogin"]'
            )

        # If there's an active session from another browser / device, the
        # platform interrupts with a "Login Warning - Active Session Alert"
        # page. Clicking "Continue with Login" ends the other session and
        # proceeds.
        resume_button = page.locator("#loginresumesession_buttoncontinue")
        if await resume_button.count() > 0:
            log.info("authenticate: active session alert — taking over previous session")
            async with page.expect_navigation(wait_until="networkidle"):
                await resume_button.click()

        final_url = page.url
        if "_csrf_token" not in final_url:
            raise RuntimeError(
                f"Login did not produce a _csrf_token in the URL.\n"
                f"  Final URL: {final_url}\n"
                f"  Title:     {await page.title()}\n"
            )

        csrf_token = parse_qs(urlparse(final_url).query).get("_csrf_token", [""])[0]
        if not csrf_token:
            raise RuntimeError(f"Could not parse _csrf_token from final URL: {final_url}")

        self.csrf_token = csrf_token
        self.user_agent = await page.evaluate("navigator.userAgent")
        log.info("authenticate: login succeeded", final_url=final_url)

    async def ensure_authenticated(self, username: str, password: str) -> bool:
        """Verify we're still logged in and recover if not. Returns True if a
        (re-)login was performed.

        Used by the pre-auth flow: we authenticate early and hold the session
        through keepalive + the virtual waiting room, but a redirect through
        the queue *might* invalidate it. After the queue we land back on
        splash.html — if it shows the login form, the session was lost and we
        re-authenticate; otherwise we refresh the CSRF token from the current
        URL (the early-login token may be stale after a long hold) and proceed.
        """
        page = self._page
        login_form = page.locator('input[name="weblogin_username"]')
        if await login_form.count() > 0:
            log.warning(
                "ensure_authenticated: login form present after queue — "
                "session lost; re-authenticating",
                url=page.url,
            )
            await self.authenticate(username, password)
            return True

        token = parse_qs(urlparse(page.url).query).get("_csrf_token", [""])[0]
        if token:
            self.csrf_token = token
        log.info(
            "ensure_authenticated: session survived the queue; still authenticated",
            url=page.url,
            csrf_refreshed=bool(token),
        )
        return False

    async def _fetch(
        self, url: str, *, body_type: str | None, fields: dict[str, str] | None
    ) -> Response:
        """Run fetch() from the live page's JS context so it uses the real
        browser network stack. `body_type=None` means a plain GET.
        Returns status, final URL, and body text."""
        js = r"""
        async ({url, bodyType, fields}) => {
            const opts = {redirect: 'follow', credentials: 'include'};
            if (bodyType === null) {
                opts.method = 'GET';
            } else {
                opts.method = 'POST';
                if (bodyType === 'multipart') {
                    opts.body = new FormData();
                } else {
                    opts.body = new URLSearchParams();
                }
                for (const [k, v] of Object.entries(fields)) opts.body.append(k, v);
            }
            const resp = await fetch(url, opts);
            return {
                status: resp.status,
                url: resp.url,
                text: await resp.text(),
            };
        }
        """
        result = await self._page.evaluate(
            js, {"url": url, "bodyType": body_type, "fields": fields}
        )
        return Response(status=result["status"], url=result["url"], text=result["text"])


async def enter_site(base_url: str, *, headless: bool = False) -> BookingSession:
    """Open a browser, land on splash.html, and wait out the virtual waiting
    room if we're redirected there. Returns an *unauthenticated* session.

    Purpose: separating site entry from login lets the scheduler arrive
    early for high-contention booking-open windows — acquire the queue
    pass cookie, then idle on the public splash via `keepalive()` — and
    defer the actual login to just before booking opens. That avoids the
    "logged-in session gets bounced through the waiting room and loses
    state" edge case.

    Close the returned session (or use `async with`) when done.
    """
    pw_mgr = async_playwright()
    p = await pw_mgr.__aenter__()
    try:
        browser = await p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--disable-infobars",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/Chicago",
        )
        # Standard hardening: don't advertise the automation indicator.
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()

        splash_url = f"{base_url}/splash.html"
        log.info("enter_site: loading landing page", url=splash_url)
        await page.goto(splash_url, wait_until="networkidle")

        # During high-traffic booking-open windows, the platform routes
        # traffic through a virtual waiting room. If we land there, block
        # until released — the signed pass cookie issued on release lets
        # subsequent navigations in this context sail through for ~24h.
        queue_stats: dict | None = None
        if is_in_queue(page):
            log.warning("enter_site: virtual waiting room detected on landing")
            queue_stats = await wait_for_queue_release(page)

        return BookingSession(
            base_url=base_url,
            _pw_mgr=pw_mgr,
            _browser=browser,
            _context=context,
            _page=page,
            queue_stats=queue_stats,
        )
    except BaseException:
        try:
            await pw_mgr.__aexit__(None, None, None)
        except Exception:
            pass
        raise


async def login(
    username: str, password: str, base_url: str, *, headless: bool = False
) -> BookingSession:
    """Enter the site and authenticate in one shot. Returns a fully
    authenticated BookingSession.

    Convenience wrapper over `enter_site()` + `BookingSession.authenticate()`.
    For booking-open windows with a waiting room, callers that want to
    defer login until closer to T=0 should use the two underlying steps
    directly.
    """
    session = await enter_site(base_url, headless=headless)
    try:
        await session.authenticate(username, password)
        return session
    except BaseException:
        await session.close()
        raise


async def _smoke_test() -> None:
    """Log in, then re-fetch splash.html via the browser's request API."""
    import os

    from dotenv import load_dotenv

    from tee_time_booker.config import Secrets

    load_dotenv()
    secrets = Secrets()  # type: ignore[call-arg]

    headless = os.getenv("HEADLESS", "0") == "1"
    log.info("starting login smoke test", username=secrets.username, headless=headless)

    async with await login(
        secrets.username,
        secrets.password.get_secret_value(),
        secrets.base_url,
        headless=headless,
    ) as sess:
        print(f"\nUser-Agent:   {sess.user_agent}")
        print(f"Base URL:     {sess.base_url}")
        print(f"CSRF preview: {sess.csrf_token[:30]}...")

        resp = await sess.get(f"{sess.base_url}/splash.html")
        print(f"\nGET /splash.html -> HTTP {resp.status} ({len(resp.text)} bytes)")


async def _smoke_test_keepalive() -> None:
    """Log in, then run keepalive for KEEPALIVE_DURATION_SEC (default 240s / 4 min).

    Expected behavior:
      - Login succeeds (queue wait is a no-op since queue isn't active)
      - Keepalive refreshes splash every 90 sec (so 2-3 cycles in 4 min)
      - Each refresh logs status + URL
      - Clean exit when deadline is reached
    """
    import os

    from dotenv import load_dotenv

    from tee_time_booker.clock import sync_clock
    from tee_time_booker.config import Secrets

    load_dotenv()
    secrets = Secrets()  # type: ignore[call-arg]

    duration_sec = int(os.getenv("KEEPALIVE_DURATION_SEC", "240"))
    interval_sec = int(os.getenv("KEEPALIVE_INTERVAL_SEC", "90"))
    headless = os.getenv("HEADLESS", "0") == "1"

    log.info(
        "starting keepalive smoke test",
        username=secrets.username,
        duration_sec=duration_sec,
        interval_sec=interval_sec,
    )

    clock = await sync_clock()

    async with await login(
        secrets.username,
        secrets.password.get_secret_value(),
        secrets.base_url,
        headless=headless,
    ) as sess:
        print(f"\nLogged in. User-Agent: {sess.user_agent}")
        print(f"Running keepalive for {duration_sec}s "
              f"(interval {interval_sec}s, expect {duration_sec // interval_sec} refresh cycles).")
        print("Watch the browser — splash.html should reload every ~90s.\n")

        deadline = clock.now_utc() + timedelta(seconds=duration_sec)
        await sess.keepalive(clock, deadline, interval_sec=interval_sec)

        print("\nKeepalive complete. Queue pass check:", end=" ")
        from datetime import date as _date
        # Dummy date — we just want to exercise the cookie lookup path.
        has_pass = await has_queue_pass(sess._context, _date.today())
        print(f"has_queue_pass(today) = {has_pass}")


async def _smoke_test_scheduled() -> None:
    """Rehearse the long-lead scheduled-run timeline with a fake booking-open
    moment. Exits after 'fake opens' instead of booking.

    Env vars (all seconds):
      LEAD_SEC (default 360)     — total lead from now to fake opens
      AUTH_LEAD_SEC (default 60) — login happens this far before fake opens
      KEEPALIVE_INTERVAL_SEC (default 45) — shortened from prod's 90 so we
                                            see multiple cycles in a 6-min run

    Expected sequence:
      enter_site (~few s) → keepalive loop (≥1 cycle) → authenticate (~few s)
      → quiet wait → 'fake opens hit' log → exit 0
    """
    import os

    from dotenv import load_dotenv

    from tee_time_booker.clock import sync_clock
    from tee_time_booker.config import Secrets

    load_dotenv()
    secrets = Secrets()  # type: ignore[call-arg]

    lead_sec = int(os.getenv("LEAD_SEC", "360"))
    auth_lead_sec = int(os.getenv("AUTH_LEAD_SEC", "60"))
    keepalive_interval = int(os.getenv("KEEPALIVE_INTERVAL_SEC", "45"))
    headless = os.getenv("HEADLESS", "0") == "1"

    clock = await sync_clock()
    opens_at = clock.now_utc() + timedelta(seconds=lead_sec)

    log.info(
        "smoke scheduled: starting",
        fake_opens_at_utc=opens_at.isoformat(),
        lead_sec=lead_sec,
        auth_lead_sec=auth_lead_sec,
        keepalive_interval=keepalive_interval,
    )
    print(f"\nFake booking-open moment: {opens_at.isoformat(timespec='seconds')}")
    print(f"  Lead total:         {lead_sec}s")
    print(f"  Auth lead:          {auth_lead_sec}s (login at fake T-{auth_lead_sec}s)")
    print(f"  Keepalive interval: {keepalive_interval}s\n")

    async with await enter_site(secrets.base_url, headless=headless) as session:
        auth_at = opens_at - timedelta(seconds=auth_lead_sec)
        now = clock.now_utc()
        if now < auth_at and (auth_at - now).total_seconds() > keepalive_interval:
            wait_s = (auth_at - now).total_seconds()
            log.info(
                "entered site; keepalive until auth moment",
                wait_seconds=round(wait_s, 1),
                interval_sec=keepalive_interval,
            )
            await session.keepalive(
                clock, auth_at, interval_sec=keepalive_interval
            )

        if clock.now_utc() < auth_at:
            await clock.sleep_until(auth_at)

        log.info("auth moment reached; logging in")
        await session.authenticate(
            secrets.username, secrets.password.get_secret_value()
        )

        wait_s = (opens_at - clock.now_utc()).total_seconds()
        log.info(
            "logged in; waiting for fake opens moment",
            wait_seconds=round(wait_s, 3),
        )
        await clock.sleep_until(opens_at)

        log.warning("fake opens hit; WOULD fire booking now (smoke exits here)")
        print("\n✓ Timeline rehearsal complete — all phases fired in order.")


if __name__ == "__main__":
    import os

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    mode = os.getenv("SMOKE_MODE", "login")
    if mode == "keepalive":
        asyncio.run(_smoke_test_keepalive())
    elif mode == "scheduled":
        asyncio.run(_smoke_test_scheduled())
    else:
        asyncio.run(_smoke_test())
