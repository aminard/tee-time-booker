"""Playwright-based session. Login runs interactively, then subsequent HTTP
requests use the browser's real network stack via `page.goto` (for GETs) and
in-page `fetch()` via `page.evaluate` (for POSTs).

Using the page navigation stack — rather than Playwright's separate
`context.request` API — is what actually shares TLS + cookies + any
additional state the server expects from a real browser.

Some release windows run behind a virtual waiting room. When the platform
redirects us to it, we wait for the queue to release us automatically
(detected via URL leaving the queue host). Once released, a signed pass
cookie is set on the platform domain and persists for ~24 hours, so
subsequent navigations in the same browser context sail through.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date as ddate, datetime, timedelta
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
    """True if the context holds a signed pass cookie for this release event.

    The pass is issued by the waiting room and set on the platform domain
    when it releases us. Name format observed: `QueueITAccepted-*_txaustin{YYYYMMDD}`.
    """
    suffix = f"txaustin{target_date.strftime('%Y%m%d')}"
    for c in await context.cookies():
        name = c.get("name", "")
        if name.startswith("QueueITAccepted-") and name.endswith(suffix):
            return True
    return False


async def wait_for_queue_release(
    page: Page,
    *,
    poll_interval_sec: float = 2.0,
    log_interval_sec: float = 30.0,
    timeout_sec: int = 3600,
) -> None:
    """Block until the page navigates out of the virtual waiting room.

    The waiting room JS polls its own status endpoint and triggers a full
    redirect back to the target URL when our turn is up, so we just watch
    the page URL. Progress is logged every ~30 sec for observability.
    """
    loop = asyncio.get_event_loop()
    start = loop.time()
    last_log = start
    log.warning(
        "virtual waiting room: entered",
        url=page.url,
        title=await page.title(),
    )
    while True:
        if not is_in_queue(page):
            log.info(
                "virtual waiting room: released",
                wait_seconds=round(loop.time() - start, 1),
                url=page.url,
            )
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            return

        elapsed = loop.time() - start
        if elapsed >= timeout_sec:
            raise RuntimeError(
                f"virtual waiting room: timed out after {timeout_sec}s (still on {page.url})"
            )
        if loop.time() - last_log >= log_interval_sec:
            log.info(
                "virtual waiting room: still waiting",
                wait_seconds=round(elapsed, 1),
                url=page.url,
            )
            last_log = loop.time()
        await asyncio.sleep(poll_interval_sec)


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
    """An authenticated browser session. Use as an async context manager so the
    underlying Playwright resources are cleaned up deterministically."""

    csrf_token: str
    base_url: str
    user_agent: str
    _pw_mgr: Any = field(repr=False)
    _browser: Browser = field(repr=False)
    _context: BrowserContext = field(repr=False)
    _page: Page = field(repr=False)

    async def __aenter__(self) -> "BookingSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        try:
            await self._browser.close()
        finally:
            await self._pw_mgr.__aexit__(None, None, None)

    async def get(self, url: str) -> Response:
        """Navigate the live page to `url` and return the final status + HTML."""
        response = await self._page.goto(url, wait_until="networkidle")
        status = response.status if response else 0
        return Response(
            status=status,
            url=self._page.url,
            text=await self._page.content(),
        )

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
    ) -> None:
        """Refresh splash.html periodically until `deadline_utc`.

        Purpose: two-fold.
          (1) Keep short-TTL platform cookies (Cloudflare bot-management etc.)
              from expiring during long idle waits.
          (2) Detect virtual-waiting-room activation during the idle period;
              if a refresh lands us in the queue, we wait for release.

        `clock` is a tee_time_booker.clock.Clock (offset-corrected NTP wrapper).
        """
        splash_url = f"{self.base_url}/splash.html"
        log.info(
            "keepalive: starting",
            deadline_utc=deadline_utc.isoformat(),
            interval_sec=interval_sec,
        )
        while True:
            now = clock.now_utc()
            if now >= deadline_utc:
                log.info("keepalive: deadline reached")
                return

            next_tick = min(now + timedelta(seconds=interval_sec), deadline_utc)
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
                    await wait_for_queue_release(self._page)
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

    async def _fetch(
        self, url: str, *, body_type: str, fields: dict[str, str]
    ) -> Response:
        """Run fetch() from the live page's JS context so it uses the real
        browser network stack. Returns status, final URL, and body text."""
        js = r"""
        async ({url, bodyType, fields}) => {
            let body;
            if (bodyType === 'multipart') {
                body = new FormData();
                for (const [k, v] of Object.entries(fields)) body.append(k, v);
            } else {
                body = new URLSearchParams();
                for (const [k, v] of Object.entries(fields)) body.append(k, v);
            }
            const resp = await fetch(url, {
                method: 'POST',
                body,
                redirect: 'follow',
                credentials: 'include',
            });
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


async def login(
    username: str, password: str, base_url: str, *, headless: bool = False
) -> BookingSession:
    """Log in through a real browser. Returns a BookingSession that keeps the
    browser alive; close it (or use `async with`) when done."""
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

        login_landing = f"{base_url}/splash.html"
        log.info("loading landing page", url=login_landing)
        await page.goto(login_landing, wait_until="networkidle")

        # During high-traffic release windows, the platform redirects all
        # incoming traffic through a virtual waiting room. If we land there,
        # block until the queue releases us (signed pass cookie is issued
        # on release and the page auto-redirects back to splash).
        if is_in_queue(page):
            log.warning("login: virtual waiting room detected on landing")
            await wait_for_queue_release(page)

        username_input = page.locator('input[name="weblogin_username"]')
        try:
            await username_input.wait_for(state="visible", timeout=15_000)
        except Exception:
            log.error(
                "login form did not appear",
                url=page.url,
                title=await page.title(),
                body_snippet=(await page.content())[:500],
            )
            raise

        log.info("filling credentials")
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
            log.info("active session alert — taking over previous session")
            async with page.expect_navigation(wait_until="networkidle"):
                await resume_button.click()

        final_url = page.url
        if "_csrf_token" not in final_url:
            raise RuntimeError(
                f"Login did not produce a _csrf_token in the URL.\n"
                f"  Final URL: {final_url}\n"
                f"  Title:     {await page.title()}\n"
            )

        ua = await page.evaluate("navigator.userAgent")
        csrf_token = parse_qs(urlparse(final_url).query).get("_csrf_token", [""])[0]
        if not csrf_token:
            raise RuntimeError(f"Could not parse _csrf_token from final URL: {final_url}")

        log.info("login succeeded", final_url=final_url)
        return BookingSession(
            csrf_token=csrf_token,
            base_url=base_url,
            user_agent=ua,
            _pw_mgr=pw_mgr,
            _browser=browser,
            _context=context,
            _page=page,
        )
    except BaseException:
        try:
            await pw_mgr.__aexit__(None, None, None)
        except Exception:
            pass
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
    else:
        asyncio.run(_smoke_test())
