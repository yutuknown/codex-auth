import json
import urllib.request
import urllib.error
import typer
import base64
import os
import mimetypes
from pathlib import Path
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich import box
from rich.live import Live
from rich.console import ConsoleOptions, RenderResult, RenderableType
from rich.segment import Segment
from rich.text import Text
from rich.console import Group
import shutil
from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML, FormattedText
import questionary
from .usage import load_usage, format_tokens

console = Console()

style = Style.from_dict({
    'prompt': 'ansiblue bold',
})

def encode_file_to_b64(filepath: str) -> str:
    path = Path(filepath).resolve()
    if not path.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "application/octet-stream"
        
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
        
    return f"data:{mime_type};base64,{encoded}"

class LeftBorder:
    def __init__(self, renderable: RenderableType, color: str = "cyan"):
        self.renderable = renderable
        self.color = color

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Decrease max width by 2 to account for the "│ " we are prepending
        lines = console.render_lines(self.renderable, options.update(width=options.max_width - 2))
        style = console.get_style(self.color)
        for line in lines:
            yield Segment("| ", style)
            yield from line
            yield Segment("\n")

def run_chat():
    """
    Start an interactive terminal chat session with the stealth proxy.
    """
    console.print("\n[bold magenta]▲[/bold magenta] [bold]Interactive Chat[/bold]")
    console.print("  [dim]Proxy:[/dim]   http://127.0.0.1:8000/v1")
    console.print("  [dim]Action:[/dim]  Type your message below. Use [bold]Alt+Enter[/bold] for newlines.")
    console.print("  [dim]Commands:[/dim] [bold]/model[/bold], [bold]/file <path>[/bold], [bold]/clear[/bold], [bold]/usage[/bold], [bold]/color[/bold]. (Ctrl+C to exit)\n")
    
    url = "http://127.0.0.1:8000/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    
    # Check if proxy is up
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/docs", method="HEAD")
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        console.print("[bold red]✖[/bold red] [bold]Connection Failed[/bold]")
        console.print("  [dim]Error:[/dim]  Proxy server is not running.")
        console.print("  [dim]Action:[/dim] Run [bold]codex-auth start[/bold] in another terminal first.\n")
        raise typer.Exit(code=1)

    SYSTEM_PROMPT = "You are a highly capable AI coding assistant running in the user's terminal. Provide concise, accurate answers and use markdown formatting for code snippets."
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    current_model = "auto"
    accent_color = "cyan"
    attached_files = []
    kb = KeyBindings()

    @kb.add('enter')
    def _(event):
        event.current_buffer.validate_and_handle()

    @kb.add('escape', 'enter')
    def _(event):
        event.current_buffer.insert_text('\n')

    def get_bottom_toolbar():
        data = load_usage()
        savings = data.get("total_savings_usd", 0.0)
        in_tok = data.get("total_input_tokens", 0)
        out_tok = data.get("total_output_tokens", 0)
        return FormattedText([
            ("class:toolbar-status", f" [Model: {current_model}]  [Tokens: {format_tokens(in_tok + out_tok)}]  [Est. Cost: ${savings:.2f}] "),
        ])

    def prompt_continuation(width, line_number, is_soft_wrap):
        return HTML(f'<ansi{accent_color}>| </ansi{accent_color}>')

    session_style = Style.from_dict({
        'bottom-toolbar': 'noreverse bg:#1e1e1e',
        'toolbar-rule': 'fg:#555555 bg:#1e1e1e',
        'toolbar-status': 'bg:white fg:black',
        'prompt': 'ansiblue bold',
    })

    session = PromptSession(style=session_style, key_bindings=kb, bottom_toolbar=get_bottom_toolbar)
    
    while True:
        try:
            # Check if we have pending files to show
            if attached_files:
                console.print(f"  [dim]Attached Files: {len(attached_files)}[/dim]")
                
            console.print(Rule(style="dim"))
            
            user_input = session.prompt(
                HTML(f'<ansi{accent_color}>❯</ansi{accent_color}> '), 
                multiline=True, 
                placeholder=HTML('<style color="gray">Try "how do I log an error?"</style>'),
                prompt_continuation=prompt_continuation
            )
            # No bottom Rule here — the toolbar bakes into scrollback on Enter,
            # so any line printed here would also compound. The top Rule on
            # the next loop iteration already serves as the visual separator.
            
            if not user_input.strip():
                continue
                
            # --- Slash Commands ---
            cmd_text = user_input.strip()
            
            if cmd_text == "/clear":
                messages.clear()
                messages.append({"role": "system", "content": SYSTEM_PROMPT})
                attached_files.clear()
                console.print(f"  [dim]Context cleared. Started a new session.[/dim]\n")
                continue
                
            if cmd_text.startswith("/color"):
                parts = cmd_text.split()
                if len(parts) == 2 and parts[1] in ["red", "green", "yellow", "blue", "magenta", "cyan", "white"]:
                    accent_color = parts[1]
                    console.print(f"  [dim]Theme updated to [bold {accent_color}]{accent_color}[/bold {accent_color}][/dim]\n")
                else:
                    console.print("  [dim]Usage: /color <red|green|yellow|blue|magenta|cyan|white>[/dim]\n")
                continue
                
            if cmd_text == "/usage":
                data = load_usage()
                savings = data.get("total_savings_usd", 0.0)
                in_tok = data.get("total_input_tokens", 0)
                out_tok = data.get("total_output_tokens", 0)
                console.print(f"  [dim]Session Stats:[/dim] {format_tokens(in_tok + out_tok)} tokens | Est Savings: ${savings:.2f}\n")
                continue
                
            if cmd_text.startswith("/file"):
                parts = cmd_text.split(maxsplit=1)
                if len(parts) < 2:
                    console.print("  [dim]Usage: /file <path/to/file>[/dim]\n")
                    continue
                filepath = parts[1]
                try:
                    b64_str = encode_file_to_b64(filepath)
                    attached_files.append({"type": "file_url", "file_url": {"url": b64_str}})
                    console.print(f"  [dim]Attached {os.path.basename(filepath)}[/dim]\n")
                except Exception as e:
                    console.print(f"  [dim red]Error attaching file: {e}[/dim red]\n")
                continue
                
            if cmd_text == "/model":
                available_models = ["gpt-5-5", "auto", "gpt-5-3", "gpt-5-5-mini", "gpt-5-3-mini", "gpt-5-mini"]
                try:
                    models_req = urllib.request.Request("http://127.0.0.1:8000/v1/models")
                    with urllib.request.urlopen(models_req, timeout=2) as m_resp:
                        m_data = json.loads(m_resp.read().decode('utf-8'))
                        fetched = [m["id"] for m in m_data.get("data", [])]
                        if fetched:
                            available_models = fetched
                except Exception:
                    pass
                    
                if current_model not in available_models:
                    default_choice = available_models[0] if available_models else None
                else:
                    default_choice = current_model
                    
                selected = questionary.select(
                    "Select Model:",
                    choices=available_models,
                    default=default_choice
                ).ask()
                
                if selected:
                    current_model = selected
                    console.print(f"  [dim]Switched to [bold]{current_model}[/bold][/dim]\n")
                continue
            
            # --- Normal Chat Flow ---
            if attached_files:
                msg_content = [{"type": "text", "text": user_input}] + attached_files
            else:
                msg_content = user_input
                
            messages.append({"role": "user", "content": msg_content})
            attached_files = [] # Consume attachments
            
            data = {
                "model": current_model,
                "messages": messages,
                "stream": True # Enable live streaming!
            }
            
            req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
            
            bot_reply = ""
            console.print(f"[{accent_color}]|[/{accent_color}]")
            console.print(f"[{accent_color}]◆[/{accent_color}] [bold]GPT:[/bold]")
            
            try:
                response = urllib.request.urlopen(req)
                
                # ── Reliable Windows-compatible streaming ─────────────────────────────
                # Direct sys.stdout.write per token — no Live, no ANSI cursor tricks.
                # | prefix is re-emitted after every newline to keep the chain intact.
                import sys
                pipe = f"\033[36m| \033[0m"  # cyan | prefix
                sys.stdout.write(pipe)
                sys.stdout.flush()
                at_line_start = False

                for line in response:
                    line = line.decode('utf-8').strip()
                    if not line or not line.startswith("data: "):
                        continue
                    json_str = line[6:]
                    if json_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(json_str)
                        if "error" in chunk:
                            err_msg = chunk["error"].get("message", "Unknown error")
                            err_type = chunk["error"].get("type", "Error")
                            raise Exception(f"[{err_type}] {err_msg}")
                        delta = chunk["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            bot_reply += content
                            for char in content:
                                if at_line_start:
                                    sys.stdout.write(pipe)
                                    at_line_start = False
                                sys.stdout.write(char)
                                if char == "\n":
                                    at_line_start = True
                            sys.stdout.flush()
                    except json.JSONDecodeError:
                        pass

                sys.stdout.write("\n\n")
                sys.stdout.flush()
                
                # Append to messages history
                messages.append({"role": "assistant", "content": bot_reply})
                console.print(f"[{accent_color}]|[/{accent_color}]")
                
            except urllib.error.HTTPError as he:
                # Elegantly parse API error instead of throwing python tracebacks
                err_body = he.read().decode('utf-8')
                try:
                    err_json = json.loads(err_body)
                    detail = err_json.get("detail", err_json)
                    msg = detail.get("message", str(detail))
                    typ = detail.get("type", "api_error")
                    
                    panel = Panel(
                        f"[red]{msg}[/red]", 
                        title=f"[bold red]Error: {typ}[/bold red]", 
                        border_style="red"
                    )
                    console.print(panel)
                    console.print()
                    
                    # Pop the failed user message so they can try again
                    messages.pop() 
                except:
                    console.print(f"\n[bold red]✖[/bold red] [bold]Server Error ({he.code})[/bold]")
                    console.print(f"  [dim]Details:[/dim] {err_body}\n")
                    messages.pop()

        except KeyboardInterrupt:
            console.print("\n\n[bold cyan]✔[/bold cyan] [bold]Session Ended[/bold]\n")
            break
        except Exception as e:
            console.print(f"\n[bold red]✖[/bold red] [bold]Error[/bold]")
            console.print(f"  [dim]Details:[/dim] {str(e)}\n")

