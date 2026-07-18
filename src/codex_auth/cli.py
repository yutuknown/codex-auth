import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint
import os
import sys
import json
import base64
import urllib.request
from pathlib import Path
import uvicorn

# Force UTF-8 encoding on Windows to support elegant bullet points (●)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import click
import typer.rich_utils
from .api import app as fastapi_app
from .providers.openai.auth import login
from . import __version__
from .chat import run_chat
from .usage import load_usage, format_tokens

# Enable rich markup mode for beautiful help text formatting
app = typer.Typer(
    help="[bold cyan]Codex-Auth[/bold cyan]: A professional Stealth API Proxy for ChatGPT.",
    rich_markup_mode="rich",
    add_completion=False,
)
console = Console()

# Monkeypatch Typer's default error handler to match our minimalist Vercel-style CLI
def custom_format_error(self: click.exceptions.ClickException) -> None:
    console.print("\n[bold red]●[/bold red] [bold]Command Error[/bold]")
    console.print(f"  [dim]Message:[/dim] {self.format_message()}\n")

typer.rich_utils.rich_format_error = custom_format_error

def version_callback(value: bool):
    if value:
        console.print(f"Codex-Auth version: [bold green]{__version__}[/bold green]")
        raise typer.Exit()

@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True, help="Show the version and exit."
    ),
):
    """
    [bold]Codex-Auth CLI[/bold] 🚀
    
    Automate your ChatGPT interactions by exposing a robust, stealthy OpenAI-compatible proxy API.
    """
    pass

@app.command()
def start(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="The host to bind the proxy server to."),
    port: int = typer.Option(8000, "--port", "-p", help="The port to run the proxy server on."),
    reload: bool = typer.Option(False, "--reload", "-r", help="Enable auto-reload for development."),
):
    """
    Start the Codex-Auth Stealth Proxy API server.
    """
    # Quick sanity check for auth token
    auth_file = Path(__file__).resolve().parent.parent.parent / ".codex" / "auth.json"
    if not auth_file.exists():
        console.print("\n[bold red]●[/bold red] [bold]Authentication Missing[/bold]")
        console.print("  [dim]Error:[/dim]  Could not find a valid `.codex/auth.json` token.")
        console.print("  [dim]Action:[/dim] Run [bold]codex-auth auth[/bold] first before starting the proxy.\n")
        raise typer.Exit(code=1)

    console.print(f"\n[bold green]●[/bold green] [bold]Starting Codex-Auth Proxy[/bold]")
    console.print(f"  [dim]Base URL:[/dim] [cyan]http://{host}:{port}/v1[/cyan]")
    console.print(f"  [dim]API Key:[/dim]  [dim](Any dummy string works)[/dim]\n")
    
    try:
        if reload:
            uvicorn.run("codex_auth.api:app", host=host, port=port, reload=True)
        else:
            uvicorn.run("codex_auth.api:app", host=host, port=port)
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")
        sys.exit(0)

@app.command()
def auth():
    """
    Authenticate and capture Codex tokens using the automated headless flow.
    """
    login()

from rich.columns import Columns
from rich.align import Align
from rich.text import Text
from rich.box import ROUNDED

@app.command()
def status():
    """
    Check the health and configuration status of the Codex-Auth setup.
    """
    auth_file = Path(__file__).resolve().parent.parent.parent / ".codex" / "auth.json"
    
    # 1. Header
    console.print(f"\n[bold]Codex-Auth[/bold] [dim]v{__version__}[/dim]\n")
    
    # 2. Authentication Status
    if auth_file.exists():
        console.print("[bold green]●[/bold green] [bold]Authentication[/bold]")
        console.print("  [dim]Status:[/dim]  [green]Active[/green]")
        console.print(f"  [dim]Path:[/dim]    {auth_file.name}\n")
        
        # Parse Account Details from JWT
        try:
            with open(auth_file, "r") as f:
                auth_data = json.load(f)
                id_token = auth_data.get("tokens", {}).get("id_token", "")
                if id_token and "." in id_token:
                    payload_b64 = id_token.split(".")[1]
                    # Add padding if needed
                    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
                    
                    email = payload.get("https://api.openai.com/profile", {}).get("email", "Unknown")
                    plan = payload.get("https://api.openai.com/auth", {}).get("chatgpt_plan_type", "Unknown").title()
                    
                    console.print("[bold cyan]●[/bold cyan] [bold]Account Details[/bold]")
                    console.print(f"  [dim]Email:[/dim]   {email}")
                    console.print(f"  [dim]Plan:[/dim]    {plan}\n")
        except Exception:
            pass
    else:
        console.print("[bold red]●[/bold red] [bold]Authentication[/bold]")
        console.print("  [dim]Status:[/dim]  [red]Missing[/red]")
        console.print("  [dim]Action:[/dim]  Run [bold]codex-auth auth[/bold] to login\n")
        
    # 3. API Server
    console.print("[bold blue]●[/bold blue] [bold]API Server[/bold]")
    console.print("  [dim]URL:[/dim]     http://127.0.0.1:8000/v1")
    console.print("  [dim]Action:[/dim]  Run [bold]codex-auth start[/bold] to boot proxy\n")

    # 4. Environment
    console.print("[bold magenta]●[/bold magenta] [bold]Environment[/bold]")
    console.print(f"  [dim]Python:[/dim]  {sys.version.split()[0]}")
    console.print(f"  [dim]OS:[/dim]      {os.name.upper()}\n")

    # 5. Usage Statistics
    usage_data = load_usage()
    in_tok = usage_data.get("total_input_tokens", 0)
    out_tok = usage_data.get("total_output_tokens", 0)
    savings = usage_data.get("total_savings_usd", 0.0)
    reqs = usage_data.get("total_requests", 0)
    
    console.print("[bold yellow]●[/bold yellow] [bold]Usage Statistics[/bold]")
    console.print(f"  [dim]Requests:[/dim] {reqs}")
    console.print(f"  [dim]Tokens:[/dim]   {format_tokens(in_tok)} in / {format_tokens(out_tok)} out")
    console.print(f"  [dim]Savings:[/dim]  [bold green]${savings:.2f}[/bold green]\n")

@app.command()
def chat():
    """
    Start an interactive terminal chat session with the stealth proxy.
    """
    run_chat()

if __name__ == "__main__":
    app()
