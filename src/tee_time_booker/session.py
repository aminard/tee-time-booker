"""Playwright-based session. Login runs interactively, then subsequent HTTP
requests use the browser's real network stack via `page.goto` (for GETs) and
in-page `fetch()` via `page.evaluate` (for POSTs).

Using the page navigation stack — rather than Playwright's separate
`context.request` API — is what actually shares TLS + cookies + any
additional state the server expects from a real browser.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import structlog
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

log = structlog.get_logger()


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


if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    asyncio.run(_smoke_test())
