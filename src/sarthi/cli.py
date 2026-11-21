"""Command-line entry point: ``sarthi <command>``."""

from __future__ import annotations

import asyncio
import contextlib

import typer

from sarthi import __version__
from sarthi.app import BROWSER_PROFILE, SetupError, build
from sarthi.apply.engine import LOGIN_PAGES
from sarthi.apply.engine import login as login_to
from sarthi.chat.pairing import CODE_LIFETIME, new_code
from sarthi.clock import utcnow
from sarthi.config import get_settings
from sarthi.db.chat import ChatStore
from sarthi.db.connection import connect
from sarthi.log import setup_logging
from sarthi.sources.channels import SESSION_FILE as CHANNEL_SESSION
from sarthi.sources.channels import login as telegram_login

app = typer.Typer(help="Sarthi: finds, matches and applies to jobs for you.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Configure logging before any command runs."""
    setup_logging(get_settings().log_level)


@app.command()
def version() -> None:
    """Print the installed Sarthi version."""
    typer.echo(__version__)


@app.command()
def pair() -> None:
    """Show a one-time code that links your Telegram chat to Sarthi."""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        code = new_code(ChatStore(conn), utcnow())
    finally:
        conn.close()
    minutes = int(CODE_LIFETIME.total_seconds() // 60)
    typer.echo(f"In Telegram, open your Sarthi bot and send:\n\n    /start {code}\n")
    typer.echo(f"The code works once and expires in {minutes} minutes. Then run: uv run sarthi run")


@app.command()
def run() -> None:
    """Run Sarthi: job sources, matching, Telegram alerts and the daily digest (Ctrl+C to stop)."""
    try:
        sarthi = build(get_settings())
    except SetupError as error:
        typer.echo(f"Cannot start: {error}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("Sarthi is running. Press Ctrl+C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(sarthi.run())


@app.command()
def catchup(
    days: int = typer.Option(1, min=1, max=7, help="How many days back to search (1-7)."),
) -> None:
    """Search every source for the last N days, send the results to Telegram in one batch, then exit.

    For a computer that is on only part of the day: run it from cron or a startup task.
    """
    try:
        sarthi = build(get_settings())
    except SetupError as error:
        typer.echo(f"Cannot start: {error}", err=True)
        raise typer.Exit(code=1) from None
    problem = sarthi.catchup_problem()
    if problem:
        sarthi.conn.close()
        typer.echo(f"Cannot catch up: {problem}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Searching every source for the last {days} day(s)… results will arrive in Telegram.")
    with contextlib.suppress(KeyboardInterrupt):
        sent = asyncio.run(sarthi.run_catchup(days * 24))
        typer.echo(
            "Done: results sent to Telegram." if sent else "Stopped: results took too long (try again)."
        )


@app.command()
def login(site: str = typer.Argument(..., help="linkedin, naukri or telegram")) -> None:
    """Log in once so Sarthi can use your account: apply on a job site, or read Telegram job channels.

    You type your password into the site itself; Sarthi never sees it. Stop `sarthi run` first (the saved
    browser profile can be used by one program at a time).
    """
    site = site.strip().lower()
    if site == "telegram":
        _login_telegram()
        return
    if site not in LOGIN_PAGES:
        choices = ", ".join((*LOGIN_PAGES, "telegram"))
        typer.echo(f"Unknown site {site!r}. Choose one of: {choices}", err=True)
        raise typer.Exit(code=2)
    settings = get_settings()
    typer.echo(f"A browser window is opening. Log in to {site} there; it closes by itself once you're in.")
    done = asyncio.run(login_to(site, settings.data_dir / BROWSER_PROFILE))
    typer.echo(
        "Logged in: Sarthi can now apply there." if done else "Not logged in (window closed or timed out)."
    )


def _login_telegram() -> None:
    """Log in to Telegram in the terminal (phone number, then the code Telegram sends)."""
    settings = get_settings()
    if not settings.telegram_api_id or settings.telegram_api_hash is None:
        typer.echo(
            "Set SARTHI_TELEGRAM_API_ID and SARTHI_TELEGRAM_API_HASH in .env first "
            "(create them at https://my.telegram.org under 'API development tools').",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo("Logging in to Telegram. Sarthi only reads the channels you list; it never posts or joins.")
    name = asyncio.run(
        telegram_login(
            settings.telegram_api_id,
            settings.telegram_api_hash.get_secret_value(),
            settings.data_dir / CHANNEL_SESSION,
        )
    )
    typer.echo(f"Logged in as {name}. Add channels to SARTHI_TELEGRAM_CHANNELS, then run: uv run sarthi run")
