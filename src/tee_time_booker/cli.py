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
def schedule(plan_path: Path) -> None:
    """Compute release moment for a plan and schedule its execution."""
    raise NotImplementedError("schedule: not implemented yet")


@cli.command()
@click.argument("plan_path", type=click.Path(exists=True, path_type=Path))
@click.option("--dry-run", is_flag=True, help="Run full flow but stop before the binding POST.")
@click.option("--confirm", is_flag=True, help="Required for a real booking (no-op without it).")
def run(plan_path: Path, dry_run: bool, confirm: bool) -> None:
    """Execute a booking run against a plan.

    Waits until the plan's release moment (NTP-synced) before firing. If the
    release moment has already passed, fires immediately.
    """
    if not dry_run and not confirm:
        raise click.UsageError("Real runs require --confirm. Use --dry-run otherwise.")

    import asyncio

    import structlog
    from dotenv import load_dotenv

    from tee_time_booker.book import run_scheduled_booking
    from tee_time_booker.config import Secrets, load_plan

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )

    load_dotenv()
    secrets = Secrets()  # type: ignore[call-arg]
    plan = load_plan(plan_path)

    result = asyncio.run(
        run_scheduled_booking(plan, secrets, dry_run=not confirm)
    )

    click.echo()
    click.echo("=== DONE ===" if not result.dry_run else "=== DRY RUN DONE ===")
    click.echo(f"Steps: {' → '.join(result.steps_completed)}")
    if result.slot:
        click.echo(
            f"Slot:  {result.slot.course} @ "
            f"{result.slot.tee_time.strftime('%a %m/%d %I:%M %p')}"
        )
    if result.confirmation_url:
        click.echo(f"Confirmation URL: {result.confirmation_url}")


@cli.command()
@click.argument("confirmation_number")
@click.argument("tee_time")
def cancel(confirmation_number: str, tee_time: str) -> None:
    """Cancel a booking given its confirmation number and tee-time."""
    raise NotImplementedError("cancel: not implemented yet (flow not captured)")
