"""`tepub web` — launch the family-facing FastAPI server."""

from __future__ import annotations

import os

import click

from console_singleton import get_console

console = get_console()


@click.command(name="web")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host.")
@click.option("--port", default=8765, show_default=True, type=int, help="Bind port.")
@click.option(
    "--reload",
    is_flag=True,
    help="Auto-reload on code changes (dev only).",
)
def web_command(host: str, port: int, reload: bool) -> None:
    """Start the TEPUB web UI for family use.

    Set TEPUB_WEB_PASSWORD before launching:

        export TEPUB_WEB_PASSWORD='shared-password'
        tepub web

    Login user is always 'family'. Bind to 127.0.0.1 and expose via Tailscale
    or Cloudflare Tunnel; do not put this on the public internet without TLS.
    """
    try:
        import uvicorn  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "Web extras not installed. Run: pip install 'tepub[web]'"
        ) from exc

    if not os.environ.get("TEPUB_WEB_PASSWORD"):
        raise click.ClickException(
            "TEPUB_WEB_PASSWORD is not set. "
            "Run: export TEPUB_WEB_PASSWORD='your-shared-password'"
        )

    console.print(f"[cyan]TEPUB web on http://{host}:{port}[/cyan]  (user: family)")

    import uvicorn

    uvicorn.run(
        "web.server:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
        log_level="info",
    )
