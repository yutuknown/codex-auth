import typer
import click
import typer.rich_utils
from rich.console import Console

console = Console()

def custom_error(obj: click.exceptions.ClickException):
    console.print(f"\n[bold red]●[/bold red] [bold]Command Error[/bold]")
    console.print(f"  [dim]Message:[/dim] {obj.format_message()}\n")

typer.rich_utils.rich_format_error = custom_error

app = typer.Typer(rich_markup_mode="rich")

@app.command()
def start():
    pass

if __name__ == "__main__":
    app()
