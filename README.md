# tee-time-booker

A personal tee-time reservation assistant for municipal golf courses in Austin, TX.

## Why

Austin's four municipal golf courses release weekend tee times at 8:00 PM on the preceding Monday, and weekday times seven days in advance at 9:00 AM. This is a personal Python project that books a tee time at the moment booking opens, so I don't have to sit at a keyboard at precisely 8 PM every Monday.

## How it works

The tool drives a real browser (Playwright Chromium) for everything: login, the vendor's virtual waiting room when one is active, and the booking flow itself — GETs as page navigations, POSTs as in-page `fetch()` calls, so every request shares the browser's own session state. Search results are parsed from HTML; form fields are submitted with CSRF tokens scraped from each page. Scheduling is NTP-synced. Configuration — course preferences, party size, time window, target date(s) — comes from a YAML plan file.

A run can search multiple days at once (both weekend days open at the same moment) and ranks every candidate slot with a single score that trades off tee time against course preference. If nothing is bookable at the open, it keeps re-searching on a budget rather than giving up — inventory reappears as other shoppers' carts expire.

## Architecture

```
session.py    Browser session: login, waiting-room handling, keepalive
search.py     Tee-time search and result parsing
clock.py      NTP sync, precise scheduling, booking-open-moment rules
book.py       Booking + cancellation pipelines, slot ranking, scheduled runner
cli.py        click subcommands: run | schedule | cancel | cleanup
config.py     Pydantic models for plan.yaml and .env secrets
constants.py  Course list, booking-open constants
```

## Tech stack

Python 3.13 · [uv](https://docs.astral.sh/uv/) · Playwright · Pydantic v2 · click · structlog · BeautifulSoup + lxml · PyYAML · ntplib · python-dotenv

## Safety rails

- Books **exactly one** tee time per run; exits on first success.
- `--dry-run` runs the full flow but stops before the binding POST (the held cart item auto-releases after the platform's inactivity timeout).
- `--confirm` is required for a real booking — no accidental production runs.
- The finalize POST is never retried, so a double-booking can't happen; every other step retries with backoff.
- Plan files are validated at load (unknown course names, impossible time windows) — bad config fails at schedule time, not booking time.
- Every run writes a JSON result summary and appends to a registry (`logs/index.jsonl`) for post-hoc review.

## Status

Working end to end and in routine weekly use: scheduled runs via launchd, waiting-room handling, multi-day search, persistent re-search when sold out, booking, and cancellation. Still on the list: email notifications, an interactive plan builder.

## Running it

```bash
uv sync
uv run playwright install chromium

# Create a .env file with your platform credentials, member ID, and billing info
#   (see the Secrets model in src/tee_time_booker/config.py for required fields)

# Create a plan file for a target date (see plans/example.yaml)

# Rehearse without booking anything
uv run tee-time-booker run plans/my-weekend.yaml --dry-run

# Arm an automatic run for the plan's booking-open moment (launchd)
uv run tee-time-booker schedule plans/my-weekend.yaml --confirm
# ...then load the printed plist with launchctl, and keep the Mac awake.

# Cancel an existing reservation
uv run tee-time-booker cancel "<confirmation numbers>" "7:10 AM" --confirm

# Remove spent launchd plists from past booking events
uv run tee-time-booker cleanup
```

`schedule` figures out the right lead time from the plan's target day (weekend opens route through a waiting room and need an early arrival; weekday opens don't).

## Disclaimer

Personal project for personal use. One account, one booking at a time — the same frequency a fast human could achieve. Not affiliated with the reservation platform's operator or the vendor that built it.
