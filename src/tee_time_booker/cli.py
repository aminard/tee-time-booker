from pathlib import Path

import click


@click.group()
def cli() -> None:
    """A personal tee-time reservation assistant for municipal golf courses in Austin, TX."""


@cli.command()
def plan() -> None:
    """Interactive picker — build a plan file for an upcoming weekend."""
    raise NotImplementedError("plan: not implemented yet")


@cli.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
@click.option("--confirm", is_flag=True, help="The scheduled run will commit the booking (default: dry-run).")
@click.option("--lead-minutes", type=int, default=None, show_default="auto",
              help="Minutes before release to launch the bot process (buffers launchd jitter + Python startup). "
                   "Auto-sized to login-lead-seconds + 2 min if omitted.")
@click.option("--login-lead-seconds", type=int, default=30, show_default=True,
              help="Seconds before release the bot should be logged in and idling. "
                   "Use a large value (e.g. 3600 = 60 min) for weekend releases "
                   "that route through a virtual waiting room.")
def schedule(plan_path: Path, confirm: bool, lead_minutes: int | None, login_lead_seconds: int) -> None:
    """Generate a launchd plist to run the bot automatically at a plan's release moment.

    Writes a .plist into ~/Library/LaunchAgents/ that fires the bot a few
    minutes before release. The bot then NTP-syncs, logs in (waiting through
    a virtual waiting room if present), and fires the booking at T=0.

    Your Mac must be awake and logged into your user account at fire time,
    AND stay awake through the booking moment. For long login-lead windows,
    use `caffeinate -d` to prevent sleep.
    """
    import shutil
    from datetime import timedelta

    from tee_time_booker.clock import compute_release_moment
    from tee_time_booker.config import load_plan
    from tee_time_booker.constants import CENTRAL

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
    release_utc = compute_release_moment(plan.target_date)
    fire_at_utc = release_utc - timedelta(minutes=lead_minutes)
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
        <string>{login_lead_seconds}</string>
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

    release_ct = release_utc.astimezone(CENTRAL)
    login_at_local = (release_utc - timedelta(seconds=login_lead_seconds)).astimezone()
    click.echo(f"Plist written:  {plist_path}")
    click.echo(f"Fire time:      {fire_at_local.strftime('%a %Y-%m-%d %I:%M %p %Z')} "
               f"(lead {lead_minutes} min)")
    click.echo(f"Login at:       {login_at_local.strftime('%a %Y-%m-%d %I:%M:%S %p %Z')} "
               f"(lead {login_lead_seconds} sec)")
    click.echo(f"Release moment: {release_ct.strftime('%a %Y-%m-%d %I:%M:%S %p %Z')}")
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
              help="Seconds before release to be logged in and idling. Use a large "
                   "value (e.g. 3600) for release windows with a virtual waiting room.")
def run(plan_path: Path, dry_run: bool, confirm: bool, login_lead_seconds: int) -> None:
    """Execute a booking run against a plan.

    Waits until the plan's release moment (NTP-synced) before firing. If the
    release moment has already passed, fires immediately. Writes a JSON result
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
                plan, secrets, dry_run=not confirm, lead_time_sec=login_lead_seconds
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
