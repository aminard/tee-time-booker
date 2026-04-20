# tee-time-booker

A personal tee-time reservation assistant for municipal golf courses in Austin, TX.

## Why

Austin's four municipal golf courses release weekend tee times at 8:00 PM on the preceding Monday, and weekday times seven days in advance at 9:00 AM. This is a personal Python project that books a tee time at the moment of release, so I don't have to sit at a keyboard at precisely 8 PM every Monday.

## How it works

The tool authenticates through a real browser (Playwright Chromium), then runs the booking flow as a sequence of HTTP requests with NTP-synced scheduling. Search results are parsed from HTML; form fields are submitted with CSRF tokens scraped from each page. Configuration — course preferences, party size, time window, target date — comes from a YAML plan file.

## Architecture

```
session.py    Authentication and session cookies
search.py     Tee-time search and result parsing
clock.py      NTP sync + precise scheduling (planned)
book.py       Booking flow (planned)
notify.py     Email notification (planned)
cli.py        click subcommands: plan | schedule | run | cancel
config.py     Pydantic models for plan.yaml and .env secrets
constants.py  Course list, release-time constants
```

## Tech stack

Python 3.13 · [uv](https://docs.astral.sh/uv/) · Playwright · curl_cffi · Pydantic v2 · click · structlog · BeautifulSoup + lxml · PyYAML · ntplib · python-dotenv

## Safety rails (planned)

- Books **exactly one** tee time per run; exits on first success.
- `--dry-run` runs the full flow but stops before the binding POST.
- `--confirm` flag is required for a real booking — no accidental production runs.
- Idempotency lockfile prevents duplicate runs for the same target date.
- Time-window gate refuses to execute outside the expected release moment.
- Cost check aborts if the cart total is ever not $0.00.

## Status

Actively building. **Working:** login, session handling, search and parse. **In progress:** scheduling, booking, notifications.

## Running it

```bash
uv sync
uv run playwright install chromium

# Create a .env file with your platform credentials, member ID, and billing info
#   (see the Secrets model in src/tee_time_booker/config.py for required fields)

# Create a plan file for a target date (see plans/example.yaml)

uv run tee-time-booker run plans/my-weekend.yaml --dry-run
```

## Disclaimer

Personal project for personal use. One account, one booking at a time — the same frequency a fast human could achieve. Not affiliated with the reservation platform's operator or the vendor that built it.
