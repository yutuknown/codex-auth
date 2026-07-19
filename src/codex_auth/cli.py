import base64
import json
import os
import sys
from pathlib import Path

import typer
import uvicorn
from rich.console import Console

# Force UTF-8 encoding on Windows to support elegant bullet points (●)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# If running as a PyInstaller standalone executable, force Playwright to look for the bundled browser
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

import click
import typer.rich_utils

from . import __version__
from .chat import run_chat
from .providers.openai.auth import login
from .usage import format_tokens, load_usage

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

    console.print("\n[bold green]●[/bold green] [bold]Starting Codex-Auth Proxy[/bold]")
    console.print(f"  [dim]Base URL:[/dim] [cyan]http://{host}:{port}/v1[/cyan]")
    console.print("  [dim]API Key:[/dim]  [dim](Any dummy string works)[/dim]\n")
    
    # Pre-flight port check — give a clean error before uvicorn crashes with WinError 10048
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sock.connect_ex((host, port)) == 0:
            # Port is occupied — try to find out who's using it
            pid_hint = ""
            try:
                import subprocess
                result = subprocess.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True
                )
                for line in result.stdout.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        pid = line.strip().split()[-1]
                        pid_hint = f"\n  [dim]PID:[/dim]    {pid} — run [bold]taskkill /F /PID {pid}[/bold] to free the port"
                        break
            except Exception:
                pass

            console.print(f"[bold red]✖[/bold red] [bold]Port {port} Is Already In Use[/bold]")
            console.print(f"  [dim]Error:[/dim]  Another process is already listening on {host}:{port}.")
            console.print("  [dim]Action:[/dim] Stop the existing proxy with [bold]Ctrl+C[/bold] in its terminal,")
            console.print(f"          or start on a different port: [bold]codex-auth start --port 8001[/bold]{pid_hint}\n")
            raise typer.Exit(code=1)

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

@app.command()
def install():
    """
    Install the required headless browser (Chromium) for Codex-Auth.
    This is required after installing via pipx.
    """
    console.print("\n[bold cyan]●[/bold cyan] [bold]Installing Chromium Browser[/bold]")
    console.print("  [dim]This may take a minute as Playwright downloads the browser engine...[/dim]\n")
    
    import subprocess
    try:
        # Run playwright install chromium
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        console.print("\n[bold green]✔[/bold green] [bold]Browser Installed Successfully![/bold]")
        console.print("  [dim]You can now run [bold]codex-auth auth[/bold] to login.[/dim]\n")
    except subprocess.CalledProcessError:
        console.print("\n[bold red]✖[/bold red] [bold]Browser Installation Failed[/bold]")
        console.print("  [dim]Action:[/dim] Try running `playwright install chromium` manually.\n")
        raise typer.Exit(code=1)


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
