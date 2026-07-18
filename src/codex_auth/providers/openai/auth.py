import os
import sys
import time
import subprocess
import re
import typer
from rich.console import Console
from rich.panel import Panel

def launch_native_browser(url: str):
    if sys.platform == "win32":
        return subprocess.Popen(["start", "", url], shell=True)
    elif sys.platform == "darwin":
        return subprocess.Popen(["open", url])
    else:
        return subprocess.Popen(["xdg-open", url])

# Force UTF-8 encoding on Windows to support elegant bullet points (●)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

app = typer.Typer(add_completion=False)
console = Console()

@app.command()
def login():
    """
    Launch codex login in the background, automatically intercept its OAuth URL, and complete the login via a Phantom browser.
    """
    console.print("\n[bold cyan]●[/bold cyan] [bold]OpenAI Auth Token Capturer[/bold]")
    console.print("  [dim]Type:[/dim]    Fully Automated Wrapper")
    console.print("  [dim]Status:[/dim]  Starting...\n")
    
    browser_process = None
    
    try:
        console.print("[yellow]Launching official codex CLI...[/yellow]")
        
        cmd = ["codex", "login"]
        if os.name == "nt":
            cmd = ["codex.cmd", "login"]
            
        # FORCE codex to write to the local directory instead of global ~/.codex
        # to satisfy the custom user rule for safe configuration management.
        env = os.environ.copy()
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        env["HOME"] = repo_root
        env["USERPROFILE"] = repo_root
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env
        )
        
        oauth_url = None
        output_buffer = ""
        
        # Read the output line by line looking for the OAuth URL
        for line in iter(process.stdout.readline, ''):
            clean_line = line.strip()
            if clean_line:
                console.print(f"[dim]codex:[/dim] {clean_line}")
                
            output_buffer += line
            
            if "https://auth.openai.com/oauth/authorize" in output_buffer and not oauth_url:
                match = re.search(r'(https://auth\.openai\.com/oauth/authorize\S+)', output_buffer)
                if match:
                    oauth_url = match.group(1)
                    console.print("\n[green][OK] Automatically intercepted OAuth PKCE URL![/green]")
                    
                    with console.status("[yellow]Launching unmonitored Chromium environment...[/yellow]", spinner="dots"):
                        browser_process = launch_native_browser(oauth_url)
                        
                    console.print("\n[bold green]●[/bold green] [bold]Browser Launched[/bold]")
                    console.print("  [dim]Mode:[/dim]    Unmonitored Phantom")
                    console.print("  [dim]Action:[/dim]  Please complete the login process in the newly opened browser window.")
                    console.print(f"  [dim]Manual URL:[/dim] [underline blue]{oauth_url}[/underline blue]")
                    console.print("  [dim]Status:[/dim]  Waiting for completion (Timeout: 180s)...\n")
                    
        # Wait for codex login to finish (with timeout to prevent infinite hangs)
        try:
            process.wait(timeout=180)
        except subprocess.TimeoutExpired:
            process.terminate()
            console.print(f"\n[bold red]●[/bold red] [bold]Authentication Failed[/bold]")
            console.print("  [dim]Error:[/dim]  Login timed out after 3 minutes.\n")
            sys.exit(1)
        
        if process.returncode == 0:
            console.print("\n[bold green]●[/bold green] [bold]Authentication Successful[/bold]")
            console.print("  [dim]Status:[/dim]  Tokens generated natively")
            console.print("  [dim]Next:[/dim]    You can now start the proxy via [bold]codex-auth start[/bold]\n")
        else:
            console.print(f"\n[bold red]●[/bold red] [bold]Authentication Failed[/bold]")
            console.print(f"  [dim]Error:[/dim]  Codex login exited with code {process.returncode}\n")
            sys.exit(1)

    except FileNotFoundError:
        console.print("\n[bold red]●[/bold red] [bold]Authentication Failed[/bold]")
        console.print("  [dim]Error:[/dim]  Could not find the 'codex' command.")
        console.print("  [dim]Action:[/dim] Ensure the official codex CLI is installed and in your PATH.\n")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]●[/bold red] [bold]Authentication Failed[/bold]")
        console.print(f"  [dim]Error:[/dim]  {str(e)}\n")
        sys.exit(1)
    finally:
        if browser_process:
            try:
                browser_process.terminate()
            except:
                pass

if __name__ == "__main__":
    app()
