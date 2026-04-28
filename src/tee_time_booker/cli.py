from pathlib import Path

import click


@click.group()
def cli() -> None:
    """A personal tee-time reservation assistant for municipal golf courses in Austin, TX."""


def _open_log_watcher_tabs(target_date) -> None:
    """Pop open two Terminal.app tabs tailing this run's stdout and stderr logs.

    Called at the start of `run` when `--watch-logs` is set — typically baked
    into a launchd-armed run via `schedule --watch` so the user sees live
    output appear the moment the bot fires, without any manual ritual.

    macOS-only. Failure here must not block the booking run.
    """
    import subprocess

    label = f"com.aminard.tee-time-booker.{target_date.isoformat()}"
    log_path = (Path("logs") / f"{label}.log").resolve()
    err_path = (Path("logs") / f"{label}.err.log").resolve()

    # Pre-create so `tail -f` doesn't complain on missing file during first ms.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    err_path.touch(exist_ok=True)

    script = f'''tell application "Terminal"
    activate
    do script "echo '[stdout] {label}'; tail -f '{log_path}'"
    do script "echo '[stderr] {label}'; tail -f '{err_path}'"
end tell'''

    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            timeout=10,
            capture_output=True,
        )
    except Exception as e:
        # Swallow — this is a convenience, not a hard requirement.
        click.echo(f"warning: could not open log-watcher tabs: {e}", err=True)


def _cleanup_spent_plists(*, dry_run: bool, verbose: bool) -> tuple[int, int]:
    """Scan ~/Library/LaunchAgents/ for spent tee-time-booker plists and
    unload + remove them. "Spent" = the plist's target booking-open moment
    is in the past.

    Returns (inspected, cleaned). If `dry_run`, still inspects but doesn't
    touch anything. If `verbose`, prints per-plist status; otherwise prints
    only when something is actually cleaned.
    """
    import re
    import subprocess
    from datetime import date, datetime, timezone

    from tee_time_booker.clock import compute_booking_opens_at

    pattern = re.compile(r"^com\.aminard\.tee-time-booker\.(\d{4}-\d{2}-\d{2})\.plist$")
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    if not agents_dir.exists():
        if verbose:
            click.echo("No LaunchAgents directory found.")
        return 0, 0

    now = datetime.now(timezone.utc)
    inspected = 0
    cleaned = 0

    for plist_path in sorted(agents_dir.iterdir()):
        m = pattern.match(plist_path.name)
        if not m:
            continue
        inspected += 1
        try:
            target_date = date.fromisoformat(m.group(1))
            opens_at = compute_booking_opens_at(target_date)
        except ValueError:
            click.echo(f"  ?? {plist_path.name} — unparseable target date, skipping")
            continue

        spent = opens_at < now
        if spent:
            action = "would clean" if dry_run else "cleaning"
            click.echo(f"  ✗ {plist_path.name} — {action} (booking opened {opens_at.astimezone().strftime('%a %Y-%m-%d %I:%M %p %Z')})")
            if not dry_run:
                # `launchctl unload` is best-effort; the plist may not be
                # loaded if the user already unloaded it manually.
                subprocess.run(
                    ["launchctl", "unload", str(plist_path)],
                    check=False,
                    capture_output=True,
                )
                plist_path.unlink()
                cleaned += 1
        elif verbose:
            click.echo(f"  ✓ {plist_path.name} — keep (booking opens {opens_at.astimezone().strftime('%a %Y-%m-%d %I:%M %p %Z')})")

    return inspected, cleaned


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned without acting.")
def cleanup(dry_run: bool) -> None:
    """Remove spent launchd plists for past booking events.

    Scans ~/Library/LaunchAgents/ for tee-time-booker plists whose target
    booking-open moment has already passed, unloads them via launchctl, and
    deletes the file. Prevents stale plists from re-firing next year on the
    same date and reduces clutter as you accumulate weekly runs.
    """
    inspected, cleaned = _cleanup_spent_plists(dry_run=dry_run, verbose=True)
    click.echo()
    if inspected == 0:
        click.echo("No tee-time-booker plists found.")
    elif dry_run:
        click.echo(f"{inspected} inspected; {cleaned} would be cleaned (dry-run, no changes made).")
    else:
        click.echo(f"{inspected} inspected; {cleaned} cleaned.")


@cli.command()
def plan() -> None:
    """Interactive picker — build a plan file for an upcoming weekend."""
    raise NotImplementedError("plan: not implemented yet")


@cli.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
@click.option("--confirm", is_flag=True, help="The scheduled run will commit the booking (default: dry-run).")
@click.option("--lead-minutes", type=int, default=None, show_default="auto",
              help="Minutes before booking opens to launch the bot process (buffers launchd jitter + Python startup). "
                   "Auto-sized to login-lead-seconds + 2 min if omitted.")
@click.option("--login-lead-seconds", type=int, default=30, show_default=True,
              help="Seconds before booking opens to start the bot's site session. "
                   "For long leads (>60s), the bot enters the site and idles through "
                   "any waiting room, then authenticates ~60s before opening. Use a "
                   "large value (e.g. 3600 = 60 min) for weekend opens.")
@click.option("--watch/--no-watch", default=True, show_default=True,
              help="When the armed run fires, auto-open Terminal tabs tailing the "
                   "stdout and stderr logs. Only effective on macOS.")
@click.option("--keep-browser-open-sec", type=int, default=0, show_default=True,
              help="After the booking finishes, keep the browser window open for "
                   "this many seconds before closing. Useful for real runs where "
                   "you want to inspect the receipt or capture DevTools data.")
def schedule(
    plan_path: Path,
    confirm: bool,
    lead_minutes: int | None,
    login_lead_seconds: int,
    watch: bool,
    keep_browser_open_sec: int,
) -> None:
    """Generate a launchd plist to run the bot automatically at the plan's booking-open moment.

    Writes a .plist into ~/Library/LaunchAgents/ that fires the bot a few
    minutes before booking opens. The bot then NTP-syncs, logs in (waiting
    through a virtual waiting room if present), and fires the booking at T=0.

    Your Mac must be awake and logged into your user account at fire time,
    AND stay awake through the booking moment. For long login-lead windows,
    use `caffeinate -d` to prevent sleep.
    """
    import shutil
    from datetime import timedelta

    from tee_time_booker.clock import compute_booking_opens_at
    from tee_time_booker.config import load_plan
    from tee_time_booker.constants import CENTRAL

    # Sweep any spent plists before adding a new one, so ~/Library/LaunchAgents/
    # doesn't accumulate stale entries that could re-fire next year on the same date.
    _, cleaned = _cleanup_spent_plists(dry_run=False, verbose=False)
    if cleaned > 0:
        click.echo(f"(auto-cleanup: removed {cleaned} spent plist{'s' if cleaned != 1 else ''})")
        click.echo()

    # Auto-size launchd firing lead = login_lead + 2 min buffer for Python startup + NTP.
    if lead_minutes is None:
        lead_minutes = max(3, (login_lead_seconds + 119) // 60 + 2)
    elif lead_minutes * 60 < login_lead_seconds + 60:
        raise click.UsageError(
            f"--lead-minutes ({lead_minutes}) must be at least "
            f"{(login_lead_seconds + 119) // 60 + 1} to cover --login-lead-seconds "
            f"({login_lead_seconds}) plus startup buffer."
        )

    plan = load_plan(plan_path)
    opens_at_utc = compute_booking_opens_at(plan.target_date)
    fire_at_utc = opens_at_utc - timedelta(minutes=lead_minutes)
    fire_at_local = fire_at_utc.astimezone()

    plan_abs = plan_path.resolve()
    project_dir = plan_abs.parent if plan_abs.parent.name == "plans" else plan_abs.parent
    # Walk up to the project root (containing pyproject.toml)
    while project_dir != project_dir.parent and not (project_dir / "pyproject.toml").exists():
        project_dir = project_dir.parent

    uv_path = shutil.which("uv")
    if not uv_path:
        raise click.ClickException("Could not locate `uv` on PATH. Is uv installed?")

    flag = "--confirm" if confirm else "--dry-run"
    label = f"com.aminard.tee-time-booker.{plan.target_date.isoformat()}"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    logs_dir = project_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{label}.log"
    err_path = logs_dir / f"{label}.err.log"

    watch_arg = "\n        <string>--watch-logs</string>" if watch else ""
    keep_open_arg = (
        f"\n        <string>--keep-browser-open-sec</string>"
        f"\n        <string>{keep_browser_open_sec}</string>"
        if keep_browser_open_sec > 0
        else ""
    )
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{uv_path}</string>
        <string>run</string>
        <string>--project</string>
        <string>{project_dir}</string>
        <string>tee-time-booker</string>
        <string>run</string>
        <string>--login-lead-seconds</string>
        <string>{login_lead_seconds}</string>{watch_arg}{keep_open_arg}
        <string>{flag}</string>
        <string>{plan_abs}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project_dir}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Month</key><integer>{fire_at_local.month}</integer>
        <key>Day</key><integer>{fire_at_local.day}</integer>
        <key>Hour</key><integer>{fire_at_local.hour}</integer>
        <key>Minute</key><integer>{fire_at_local.minute}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{err_path}</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)

    opens_at_ct = opens_at_utc.astimezone(CENTRAL)
    entry_at_local = (opens_at_utc - timedelta(seconds=login_lead_seconds)).astimezone()
    click.echo(f"Plist written:  {plist_path}")
    click.echo(f"Fire time:      {fire_at_local.strftime('%a %Y-%m-%d %I:%M %p %Z')} "
               f"(lead {lead_minutes} min)")
    click.echo(f"Site entry at:  {entry_at_local.strftime('%a %Y-%m-%d %I:%M:%S %p %Z')} "
               f"(lead {login_lead_seconds} sec)")
    if login_lead_seconds > 60:
        auth_at_local = (opens_at_utc - timedelta(seconds=60)).astimezone()
        click.echo(f"Login at:       {auth_at_local.strftime('%a %Y-%m-%d %I:%M:%S %p %Z')} "
                   f"(deferred; keepalive idle between entry and login)")
    click.echo(f"Booking opens:  {opens_at_ct.strftime('%a %Y-%m-%d %I:%M:%S %p %Z')}")
    click.echo(f"Mode:           {'REAL BOOKING (--confirm)' if confirm else 'dry-run'}")
    click.echo()
    click.echo("To activate:   launchctl load " + str(plist_path))
    click.echo("To verify:     launchctl list | grep tee-time-booker")
    click.echo("To deactivate: launchctl unload " + str(plist_path) + " && rm " + str(plist_path))
    click.echo()
    click.echo("Logs will land at:")
    click.echo(f"  stdout: {log_path}")
    click.echo(f"  stderr: {err_path}")
    click.echo()
    click.secho("!! Your Mac must be AWAKE and LOGGED IN at fire time. !!", fg="yellow")
    click.secho("!! Remember to unload the plist after the run so it doesn't re-fire yearly. !!",
                fg="yellow")


@cli.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Run full flow but stop before the binding POST.")
@click.option("--confirm", is_flag=True, help="Required for a real booking (no-op without it).")
@click.option("--login-lead-seconds", type=int, default=30, show_default=True,
              help="Seconds before booking opens to start the bot's site session. "
                   "For long leads (>60s), login is deferred and fires "
                   "~60s before T=0. Use a large value (e.g. 3600) for opens "
                   "that route through a virtual waiting room.")
@click.option("--watch-logs", is_flag=True,
              help="Open Terminal tabs tailing stdout/stderr as soon as this run "
                   "starts. Intended to be baked into launchd-armed runs via "
                   "`schedule --watch` so the user sees live output automatically.")
@click.option("--keep-browser-open-sec", type=int, default=0, show_default=True,
              help="After the booking pipeline finishes, keep the browser window "
                   "open for this many seconds before closing — lets you inspect "
                   "the receipt page, capture cookies in DevTools, etc.")
def run(
    plan_path: Path,
    dry_run: bool,
    confirm: bool,
    login_lead_seconds: int,
    watch_logs: bool,
    keep_browser_open_sec: int,
) -> None:
    """Execute a booking run against a plan.

    Waits until the plan's booking-open moment (NTP-synced) before firing.
    If booking has already opened, fires immediately. Writes a JSON result
    summary to logs/ regardless of outcome, for post-hoc observability.
    """
    if not dry_run and not confirm:
        raise click.UsageError("Real runs require --confirm. Use --dry-run otherwise.")

    import asyncio
    import json
    import traceback
    from datetime import datetime

    import structlog
    from dotenv import load_dotenv

    from tee_time_booker.book import run_scheduled_booking
    from tee_time_booker.config import Secrets, load_plan
    from tee_time_booker.constants import CENTRAL

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    load_dotenv()
    secrets = Secrets()  # type: ignore[call-arg]
    plan = load_plan(plan_path)

    if watch_logs:
        _open_log_watcher_tabs(plan.target_date)

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    run_id = datetime.now(tz=CENTRAL).strftime("%Y-%m-%dT%H%M%S")
    result_path = logs_dir / f"{run_id}-result.json"

    started_at = datetime.now(tz=CENTRAL)
    result = None
    error = None
    error_traceback = None
    try:
        result = asyncio.run(
            run_scheduled_booking(
                plan, secrets,
                dry_run=not confirm,
                lead_time_sec=login_lead_seconds,
                keep_browser_open_sec=keep_browser_open_sec,
            )
        )
    except Exception as e:
        error = e
        error_traceback = traceback.format_exc()
    finished_at = datetime.now(tz=CENTRAL)

    summary = {
        "run_id": run_id,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 2),
        "target_date": plan.target_date.isoformat(),
        "plan_path": str(plan_path),
        "mode": "real" if confirm else "dry_run",
        "success": error is None,
        "steps_completed": result.steps_completed if result else [],
        "slot": (
            {
                "course": result.slot.course,
                "tee_time": result.slot.tee_time.isoformat(),
                "grfmid": result.slot.grfmid,
            }
            if result and result.slot
            else None
        ),
        "confirmation_url": result.confirmation_url if result else None,
        "error": str(error) if error else None,
        "error_type": type(error).__name__ if error else None,
        "error_traceback": error_traceback,
    }
    result_path.write_text(json.dumps(summary, indent=2, default=str))

    click.echo()
    if error is None:
        click.echo("=== DONE ===" if confirm else "=== DRY RUN DONE ===")
        click.echo(f"Steps: {' → '.join(result.steps_completed)}")
        if result.slot:
            click.echo(
                f"Slot:  {result.slot.course} @ "
                f"{result.slot.tee_time.strftime('%a %m/%d %I:%M %p')}"
            )
        if result.confirmation_url:
            click.echo(f"Confirmation URL: {result.confirmation_url}")
    else:
        click.secho(f"=== FAILED: {type(error).__name__}: {error} ===", fg="red")
        if result and result.steps_completed:
            click.echo(f"Steps completed before failure: {' → '.join(result.steps_completed)}")

    click.echo()
    click.echo(f"Result summary: {result_path.resolve()}")

    if error is not None:
        raise error


@cli.command()
@click.argument("confirmation_number")
@click.argument("tee_time")
def cancel(confirmation_number: str, tee_time: str) -> None:
    """Cancel a booking given its confirmation number and tee-time."""
    raise NotImplementedError("cancel: not implemented yet (flow not captured)")
