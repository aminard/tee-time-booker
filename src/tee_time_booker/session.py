"""Playwright-based login → yields cookies and user agent for follow-up requests.

A real browser runs the interactive login flow. The resulting session cookies
are then passed to curl_cffi, which preserves the browser's network behavior
so that subsequent requests stay consistent with the authenticated session.
"""

import asyncio
from dataclasses import dataclass

import structlog
from curl_cffi.requests import AsyncSession
from playwright.async_api import async_playwright

log = structlog.get_logger()


@dataclass
class BookingSession:
    cookies: dict[str, str]
    user_agent: str
    csrf_token: str
    base_url: str

    def client(self) -> AsyncSession:
        """Async HTTP client sharing the logged-in cookies and user agent.

        Callers must pass full URLs; prefix with `session.base_url`.
        """
        return AsyncSession(
            cookies=self.cookies,
            headers={
                "User-Agent": self.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br, zstd",
            },
            impersonate="chrome",
            timeout=10.0,
            allow_redirects=False,
        )


async def login(
    username: str, password: str, base_url: str, *, headless: bool = False
) -> BookingSession:
    """Log in through a real browser, return session cookies + UA.

    Default is headless=False because headless browsers can fail on sites with
    interactive checks during page load. A brief headed window during login is
    the pragmatic trade-off for reliability.
    """
    login_landing = f"{base_url}/splash.html"
    async with async_playwright() as p:
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

        log.info("loading landing page", url=login_landing)
        await page.goto(login_landing, wait_until="networkidle")

        # If the platform serves an interstitial page, the username field won't
        # appear immediately. Wait a bit for it to settle.
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
            from pathlib import Path
            dump_path = Path("/tmp/tee_time_booker-login-debug.html")
            dump_path.write_text(await page.content())
            raise RuntimeError(
                f"Login did not produce a _csrf_token in the URL.\n"
                f"  Final URL: {final_url}\n"
                f"  Title:     {await page.title()}\n"
                f"  Full HTML dumped to: {dump_path}\n"
                f"  Inspect that file to identify the button/link that advances."
            )

        cookies_list = await context.cookies()
        cookies = {c["name"]: c["value"] for c in cookies_list}
        ua = await page.evaluate("navigator.userAgent")

        from urllib.parse import parse_qs, urlparse
        csrf_token = parse_qs(urlparse(final_url).query).get("_csrf_token", [""])[0]
        if not csrf_token:
            raise RuntimeError(f"Could not parse _csrf_token from final URL: {final_url}")

        log.info(
            "login succeeded",
            final_url=final_url,
            cookie_names=sorted(cookies.keys()),
        )

        await browser.close()

        return BookingSession(
            cookies=cookies, user_agent=ua, csrf_token=csrf_token, base_url=base_url
        )


async def _smoke_test() -> None:
    """Run directly to verify login works end-to-end against the live site."""
    import os

    from dotenv import load_dotenv

    from tee_time_booker.config import Secrets

    load_dotenv()
    secrets = Secrets()  # type: ignore[call-arg]

    headless = os.getenv("HEADLESS", "0") == "1"
    log.info("starting login smoke test", username=secrets.username, headless=headless)

    sess = await login(
        secrets.username,
        secrets.password.get_secret_value(),
        secrets.base_url,
        headless=headless,
    )

    print(f"\nUser-Agent:  {sess.user_agent}")
    print(f"Cookies ({len(sess.cookies)}):")
    for name, val in sorted(sess.cookies.items()):
        masked = val[:10] + "..." if len(val) > 12 else val
        print(f"  {name:<25} {masked}")

    async with sess.client() as client:
        resp = await client.get(f"{sess.base_url}/splash.html")
        print(f"\nGET /splash.html via curl_cffi -> HTTP {resp.status_code} ({len(resp.content)} bytes)")
        if resp.status_code == 403:
            print("  WARNING: follow-up request was rejected by the edge.")
        elif resp.status_code == 200:
            print("  OK: follow-up request succeeded.")


if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    asyncio.run(_smoke_test())
