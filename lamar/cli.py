"""Terminal-Oberfläche von Lamar AI."""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path
from typing import Any

import anthropic
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .agent import DEFAULT_EFFORT, DEFAULT_MODEL, EFFORT_LEVELS, PERMISSION_MODES, Agent
from .tools import IMAGE_TYPES, MAX_IMAGE_BYTES, ToolOutcome

MODE_DESCRIPTIONS = {
    "ask": "fragt vor jeder Dateiänderung und jedem Befehl",
    "normal": "ändert Dateien selbst, fragt vor Befehlen",
    "auto": "macht alles ohne Nachfrage (Vorsicht!)",
}

HELP_TEXT = """\
[bold]Schreib einfach, was Lamar tun soll[/bold], z. B.:
  • Erstelle ein Snake-Spiel in Python mit pygame
  • Warum schlägt der Test in tests/test_api.py fehl? Repariere ihn.
  • Erkläre mir, wie dieses Projekt aufgebaut ist.

[bold]Befehle[/bold]
  /hilfe                 diese Hilfe
  /neu                   neues Gespräch beginnen
  /bild <pfad> \\[frage]   ein Bild mitschicken (png, jpg, gif, webp)
  /modus \\[ask|normal|auto]   Berechtigungen ändern
  /effort \\[low|medium|high|xhigh|max]   wie gründlich Lamar nachdenkt
  /kosten                verbrauchte Tokens und geschätzte Kosten
  /exit                  beenden

[bold]Tasten[/bold]
  Enter = senden · Esc dann Enter (oder Alt+Enter) = neue Zeile
  Strg+C während Lamar arbeitet = unterbrechen · Strg+D = beenden"""

TOOL_LABELS = {
    "read_file": ("Lese", "path"),
    "write_file": ("Schreibe", "path"),
    "edit_file": ("Bearbeite", "path"),
    "list_directory": ("Ordner", "path"),
    "find_files": ("Suche Dateien", "pattern"),
    "search_code": ("Durchsuche Code", "pattern"),
    "run_command": ("$", "command"),
    "web_search": ("Websuche", "query"),
    "web_fetch": ("Lade Webseite", "url"),
}


class TerminalUI:
    def __init__(self, console: Console):
        self.console = console
        self._open: str | None = None  # "text" | "thinking" | None
        self._status = None

    # -- Lade-Anzeige --------------------------------------------------------

    def status(self, message: str | None) -> None:
        if message is None:
            if self._status is not None:
                self._status.stop()
                self._status = None
            return
        if not self.console.is_terminal:
            return
        if self._status is None:
            self._status = self.console.status(f"[dim]{escape(message)}[/dim]", spinner="dots")
            self._status.start()
        else:
            self._status.update(f"[dim]{escape(message)}[/dim]")

    # -- Streaming -----------------------------------------------------------

    def _switch(self, kind: str) -> None:
        self.status(None)
        if self._open != kind:
            if self._open is not None:
                self.console.print()
            if kind == "thinking":
                self.console.print("[dim]∴ Denkt nach …[/dim]")
            self._open = kind

    def text(self, delta: str) -> None:
        self._switch("text")
        self.console.print(delta, end="", markup=False, highlight=False, soft_wrap=True)

    def thinking(self, delta: str) -> None:
        if not delta:
            return
        self._switch("thinking")
        self.console.print(delta, end="", style="dim italic", markup=False, highlight=False, soft_wrap=True)

    def block_end(self) -> None:
        self.status(None)
        if self._open is not None:
            self.console.print()
            self._open = None

    def _close(self) -> None:
        self.block_end()

    # -- Werkzeuge -----------------------------------------------------------

    def tool_call(self, name: str, tool_input: Any) -> None:
        self._close()
        label, key = TOOL_LABELS.get(name, (name, None))
        value = ""
        if isinstance(tool_input, dict) and key:
            value = str(tool_input.get(key, ""))
        if len(value) > 200:
            value = value[:200] + " …"
        color = "magenta" if name.startswith("web_") else "cyan"
        self.console.print(f"[bold {color}]●[/] [bold]{escape(label)}[/bold] {escape(value)}")

    def tool_result(self, name: str, outcome: ToolOutcome) -> None:
        style = "red" if outcome.is_error else "dim"
        summary = outcome.summary or ("Fehler" if outcome.is_error else "ok")
        if name == "run_command" and isinstance(outcome.content, str):
            body = outcome.content.rsplit("\n\n[", 1)[0].strip()
            lines = body.splitlines()
            if lines and body != "(keine Ausgabe)":
                shown = lines[-12:]
                if len(lines) > 12:
                    self.console.print(f"  [dim]… {len(lines) - 12} Zeilen davor[/dim]")
                for line in shown:
                    self.console.print(f"  [dim]│[/dim] {escape(line[:300])}", highlight=False)
        if outcome.is_error and name != "run_command" and isinstance(outcome.content, str):
            summary = outcome.content[:300]
        self.console.print(f"  [{style}]⎿ {escape(summary)}[/{style}]")

    def show_change(self, kind: str, title: str, detail: str) -> None:
        if kind != "file":
            return
        lines = detail.splitlines()
        if len(lines) > 30:
            detail = "\n".join(lines[:30]) + f"\n… ({len(lines) - 30} weitere Zeilen)"
        lexer = "diff" if detail.startswith("---") else "text"
        self.console.print(Syntax(detail, lexer, theme="ansi_dark", word_wrap=True, background_color="default"))

    def approve(self, kind: str, title: str, detail: str) -> str:
        self._close()
        lexer = "bash" if kind == "command" else ("diff" if detail.startswith("---") else "text")
        lines = detail.splitlines()
        if len(lines) > 60:
            detail = "\n".join(lines[:60]) + f"\n… ({len(lines) - 60} weitere Zeilen)"
        self.console.print(
            Panel(Syntax(detail, lexer, theme="ansi_dark", word_wrap=True, background_color="default"),
                  title=f"[bold yellow]{escape(title)}[/]", border_style="yellow")
        )
        what = "Befehle" if kind == "command" else "Dateiänderungen"
        while True:
            choices = escape(f"[j]a / [n]ein / [i]mmer ({what} in dieser Sitzung): ")
            answer = self.console.input(f"[bold yellow]Erlauben?[/] {choices}").strip().lower()
            if answer in ("j", "ja", "y", "yes", ""):
                return "yes"
            if answer in ("n", "nein", "no"):
                return "no"
            if answer in ("i", "immer", "a", "always"):
                return "always"

    # -- Meldungen -----------------------------------------------------------

    def info(self, message: str) -> None:
        self._close()
        self.console.print(f"[blue]ℹ {escape(message)}[/blue]")

    def warn(self, message: str) -> None:
        self._close()
        self.console.print(f"[yellow]⚠ {escape(message)}[/yellow]")

    def error(self, message: str) -> None:
        self._close()
        self.console.print(f"[bold red]✖ {escape(message)}[/bold red]")


# ---------------------------------------------------------------------------


def load_dotenv(workspace: Path) -> None:
    """ANTHROPIC_API_KEY & Co. aus einer .env-Datei laden (falls nicht schon gesetzt)."""
    for candidate in (workspace / ".env", Path.home() / ".lamar.env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key.startswith("ANTHROPIC_") and key not in os.environ:
                os.environ[key] = value


def image_message(ui: TerminalUI, workspace: Path, argument: str) -> list[dict] | None:
    raw_path, _, question = argument.strip().partition(" ")
    if not raw_path:
        ui.warn("Benutzung: /bild <pfad> [frage]")
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = workspace / path
    media_type = IMAGE_TYPES.get(path.suffix.lower())
    if not path.is_file() or media_type is None:
        ui.warn(f"Kein unterstütztes Bild gefunden: {raw_path}")
        return None
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        ui.warn("Das Bild ist größer als 5 MB.")
        return None
    return [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                      "data": base64.standard_b64encode(data).decode()}},
        {"type": "text", "text": question.strip() or "Was siehst du auf diesem Bild?"},
    ]


def print_usage(console: Console, agent: Agent) -> None:
    u = agent.usage
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Anfragen", str(u.requests))
    table.add_row("Eingabe-Tokens", f"{u.input_tokens:,}".replace(",", "."))
    table.add_row("Aus dem Cache gelesen", f"{u.cache_read_tokens:,}".replace(",", "."))
    table.add_row("In den Cache geschrieben", f"{u.cache_write_tokens:,}".replace(",", "."))
    table.add_row("Ausgabe-Tokens", f"{u.output_tokens:,}".replace(",", "."))
    table.add_row("Websuchen", str(u.web_searches))
    table.add_row("[bold]Geschätzte Kosten[/bold]", f"[bold]ca. ${u.cost_usd:.4f}[/bold]")
    console.print(table)


NO_KEY_HELP = """\
Kein API-Schlüssel gefunden.

So bekommst du einen:
  1. Gehe auf https://console.anthropic.com/ und melde dich an.
  2. Unter "API Keys" einen neuen Schlüssel erstellen und kopieren.
  3. Den Schlüssel setzen – eine der beiden Möglichkeiten:
       • Datei .env im Projektordner (oder ~/.lamar.env) mit der Zeile
         ANTHROPIC_API_KEY=sk-ant-...
       • Umgebungsvariable:  export ANTHROPIC_API_KEY=sk-ant-...   (Linux/macOS)
                             setx ANTHROPIC_API_KEY sk-ant-...      (Windows, neues Terminal öffnen)

Teile den Schlüssel mit niemandem und lade ihn nie auf GitHub hoch."""


def has_credentials() -> bool:
    """Grobe Prüfung, ob das SDK Zugangsdaten finden kann."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if os.environ.get("ANTHROPIC_PROFILE") or os.environ.get("ANTHROPIC_FEDERATION_RULE_ID"):
        return True
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return (config_home / "anthropic").is_dir()  # Profil von `ant auth login`


def explain_api_error(ui: TerminalUI, exc: Exception) -> None:
    if isinstance(exc, anthropic.AuthenticationError):
        ui.error(
            "Kein gültiger API-Schlüssel. Hol dir einen unter https://console.anthropic.com/ "
            "und setze ihn als Umgebungsvariable ANTHROPIC_API_KEY oder in eine .env-Datei "
            "(siehe README)."
        )
    elif isinstance(exc, anthropic.PermissionDeniedError):
        ui.error(f"Zugriff verweigert: {exc.message}")
    elif isinstance(exc, anthropic.NotFoundError):
        ui.error(f"Modell oder Endpunkt nicht gefunden: {exc.message}")
    elif isinstance(exc, anthropic.RateLimitError):
        ui.error("Zu viele Anfragen (Rate-Limit). Warte kurz und schreib dann 'weiter'.")
    elif isinstance(exc, anthropic.BadRequestError):
        ui.error(f"Ungültige Anfrage: {exc.message}")
    elif isinstance(exc, anthropic.APIStatusError):
        ui.error(f"API-Fehler {exc.status_code}: {exc.message}. Schreib 'weiter', um es erneut zu versuchen.")
    elif isinstance(exc, anthropic.APIConnectionError):
        ui.error("Keine Verbindung zur API. Prüfe deine Internetverbindung und schreib dann 'weiter'.")
    else:
        ui.error(f"{type(exc).__name__}: {exc}")


def run_turn(agent: Agent, ui: TerminalUI, content: Any) -> bool:
    try:
        agent.run(content)
        ui.block_end()
        return True
    except anthropic.APIError as exc:
        explain_api_error(ui, exc)
        return False
    except TypeError as exc:
        if "authentication" not in str(exc).lower():
            raise
        ui.error(NO_KEY_HELP)
        return False


def make_prompt_session():
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _newline(event):  # Esc+Enter bzw. Alt+Enter: neue Zeile
        event.current_buffer.insert_text("\n")

    history_file = Path.home() / ".lamar_history"
    try:
        history = FileHistory(str(history_file))
    except OSError:
        history = None
    return PromptSession(history=history, key_bindings=bindings, multiline=False)


def handle_command(line: str, agent: Agent, ui: TerminalUI, console: Console) -> Any:
    """Gibt False zum Beenden, None für 'erledigt' oder Inhalt zum Senden zurück."""
    command, _, argument = line.partition(" ")
    command = command.lower()
    argument = argument.strip()
    if command in ("/exit", "/quit", "/beenden", "/q"):
        return False
    if command in ("/hilfe", "/help", "/?"):
        console.print(Panel(HELP_TEXT, title="Lamar AI – Hilfe", border_style="cyan"))
    elif command in ("/neu", "/clear", "/new"):
        agent.reset()
        ui.info("Neues Gespräch begonnen.")
    elif command in ("/kosten", "/cost", "/usage"):
        print_usage(console, agent)
    elif command in ("/modus", "/mode"):
        if argument in PERMISSION_MODES:
            agent.permission_mode = argument
            agent._always_allowed.clear()
            ui.info(f"Modus: {argument} – {MODE_DESCRIPTIONS[argument]}")
        else:
            ui.info(f"Aktueller Modus: {agent.permission_mode} – {MODE_DESCRIPTIONS[agent.permission_mode]}")
            for mode, text in MODE_DESCRIPTIONS.items():
                console.print(f"  [bold]{mode}[/bold]: {text}")
    elif command == "/effort":
        if argument in EFFORT_LEVELS:
            agent.effort = argument
            ui.info(f"Denk-Stufe: {argument}")
        else:
            ui.info(f"Aktuelle Denk-Stufe: {agent.effort}. Möglich: {', '.join(EFFORT_LEVELS)} "
                    "(höher = gründlicher, aber langsamer und teurer)")
    elif command in ("/bild", "/image"):
        return image_message(ui, agent.workspace, argument)
    else:
        ui.warn(f"Unbekannter Befehl {command}. Tippe /hilfe.")
    return None


def interactive(agent: Agent, ui: TerminalUI, console: Console) -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]Lamar AI[/bold cyan] v{__version__} – dein KI-Programmierer\n"
            f"[dim]Ordner:[/dim] {escape(str(agent.workspace))}\n"
            f"[dim]Modell:[/dim] {agent.model}  [dim]Denk-Stufe:[/dim] {agent.effort}  "
            f"[dim]Modus:[/dim] {agent.permission_mode}\n"
            "[dim]Tippe /hilfe für Hilfe, /exit zum Beenden.[/dim]",
            border_style="cyan",
        )
    )
    session = make_prompt_session()
    while True:
        try:
            line = session.prompt("\n› ")
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        line = line.strip()
        if not line:
            continue
        content: Any = line
        if line.startswith("/"):
            content = handle_command(line, agent, ui, console)
            if content is False:
                break
            if content is None:
                continue
        console.print()
        run_turn(agent, ui, content)
    console.print("[dim]Tschüss![/dim]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lamar",
        description="Lamar AI – eine KI, die für dich programmiert (und fast alles andere kann).",
    )
    parser.add_argument("ordner", nargs="?", default=".", help="Projektordner, in dem Lamar arbeitet (Standard: aktueller Ordner)")
    parser.add_argument("-p", "--prompt", help="Eine Aufgabe direkt ausführen und danach beenden")
    parser.add_argument("--effort", choices=EFFORT_LEVELS, default=DEFAULT_EFFORT, help=f"Denk-Stufe (Standard: {DEFAULT_EFFORT})")
    parser.add_argument("--modus", choices=PERMISSION_MODES, default="normal", help="Berechtigungen (Standard: normal)")
    parser.add_argument("--modell", default=DEFAULT_MODEL, help=f"Claude-Modell (Standard: {DEFAULT_MODEL})")
    parser.add_argument("--version", action="version", version=f"Lamar AI {__version__}")
    args = parser.parse_args(argv)

    workspace = Path(args.ordner).expanduser().resolve()
    console = Console(highlight=False)
    if not workspace.is_dir():
        console.print(f"[red]Ordner nicht gefunden: {escape(str(workspace))}[/red]")
        return 2
    load_dotenv(workspace)

    ui = TerminalUI(console)
    if not has_credentials():
        ui.error(NO_KEY_HELP)
        return 1
    agent = Agent(workspace, ui, model=args.modell, effort=args.effort, permission_mode=args.modus)

    if args.prompt:
        ok = run_turn(agent, ui, args.prompt)
        return 0 if ok else 1
    try:
        interactive(agent, ui, console)
    except KeyboardInterrupt:
        console.print("\n[dim]Tschüss![/dim]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
