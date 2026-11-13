"""Die Agenten-Schleife von Lamar AI.

Ablauf pro Nachricht des Benutzers:
  1. Anfrage an Claude streamen (Text, Denk-Zusammenfassungen und Werkzeugaufrufe
     erscheinen sofort im Terminal).
  2. Ruft Claude Werkzeuge auf, werden sie lokal ausgeführt und die Ergebnisse
     zurückgeschickt.
  3. Wiederholen, bis Claude fertig ist.

Der Verlauf wird nur angehängt, nie nachträglich verändert. Das hält das
Prompt-Caching warm und die Denk-Blöcke des Modells gültig.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

import anthropic

from .prompts import build_system_prompt
from .tools import SERVER_TOOLS, TOOL_DEFINITIONS, ToolBox, ToolOutcome

DEFAULT_MODEL = "claude-opus-5-5"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"
PERMISSION_MODES = ("ask", "normal", "auto")
MAX_TOKENS = 64_000
MAX_STEPS_PER_TURN = 200
MAX_JSON_RETRIES = 2
MAX_PAUSE_CONTINUATIONS = 10

# Beta-Funktionen:
#  * server-side-fallback: lehnt ein Sicherheitsfilter eine harmlose Anfrage ab,
#    beantwortet automatisch ein anderes Modell, statt dass die Antwort ausfällt.
#  * compact: sehr lange Sitzungen werden serverseitig zusammengefasst, statt am
#    Kontextlimit zu scheitern.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
COMPACTION_BETA = "compact-2026-01-12"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1", "claude-sonnet-5-5"}

# Preise für Claude Opus 5.5 in US-Dollar pro 1 Mio. Tokens (nur zur Schätzung).
PRICE_INPUT = 4.00
PRICE_OUTPUT = 20.00
PRICE_CACHE_WRITE = 5.00
PRICE_CACHE_READ = 0.20
PRICE_WEB_SEARCH = 0.01  # pro Suche


class UI(Protocol):
    """Was die Agenten-Schleife von der Oberfläche braucht."""

    def status(self, message: Optional[str]) -> None: ...  # Lade-Anzeige; None = aus
    def text(self, delta: str) -> None: ...
    def thinking(self, delta: str) -> None: ...
    def block_end(self) -> None: ...
    def tool_call(self, name: str, tool_input: Any) -> None: ...
    def tool_result(self, name: str, outcome: ToolOutcome) -> None: ...
    def show_change(self, kind: str, title: str, detail: str) -> None: ...
    def approve(self, kind: str, title: str, detail: str) -> str: ...  # "yes" | "no" | "always"
    def info(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    web_searches: int = 0

    def add(self, message: Any) -> None:
        self.requests += 1
        usage = getattr(message, "usage", None)
        if usage is None:
            return
        # Bei Fallback oder Zusammenfassung gibt es mehrere Durchläufe; die Summe
        # steht dann in usage.iterations, das Top-Level-Feld nur für den letzten.
        parts = getattr(usage, "iterations", None) or [usage]
        for part in parts:
            self.input_tokens += getattr(part, "input_tokens", 0) or 0
            self.output_tokens += getattr(part, "output_tokens", 0) or 0
            self.cache_write_tokens += getattr(part, "cache_creation_input_tokens", 0) or 0
            self.cache_read_tokens += getattr(part, "cache_read_input_tokens", 0) or 0
        server = getattr(usage, "server_tool_use", None)
        if server is not None:
            self.web_searches += getattr(server, "web_search_requests", 0) or 0

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens * PRICE_INPUT
            + self.output_tokens * PRICE_OUTPUT
            + self.cache_write_tokens * PRICE_CACHE_WRITE
            + self.cache_read_tokens * PRICE_CACHE_READ
        ) / 1_000_000 + self.web_searches * PRICE_WEB_SEARCH


def sanitize_after_fallback(content: list[Any]) -> list[Any]:
    """Inhalt so aufbereiten, wie die API ihn nach einem Modellwechsel zurückerwartet.

    Hat mitten in der Antwort ein Ersatzmodell übernommen, markiert ein
    ``fallback``-Block die Stelle. Davor dürfen nur Text-Blöcke und vollständige
    Server-Werkzeug-Paare zurückgeschickt werden; Denk-Blöcke und unfertige
    Werkzeugaufrufe des abgelehnten Versuchs fallen weg. Der ``fallback``-Block
    selbst bleibt an seiner Position.
    """
    boundary = max((i for i, b in enumerate(content) if b.type == "fallback"), default=None)
    if boundary is None:
        return list(content)
    before = content[:boundary]
    result_ids = {getattr(b, "tool_use_id", None) for b in before if b.type.endswith("_tool_result")}
    paired = {b.id for b in before if b.type == "server_tool_use" and b.id in result_ids}
    kept = []
    for block in before:
        if block.type in ("text", "fallback"):
            kept.append(block)
        elif block.type == "server_tool_use" and block.id in paired:
            kept.append(block)
        elif block.type.endswith("_tool_result") and getattr(block, "tool_use_id", None) in paired:
            kept.append(block)
    return kept + list(content[boundary:])


def tool_result_block(tool_use_id: str, outcome: ToolOutcome) -> dict:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": outcome.content}
    if outcome.is_error:
        block["is_error"] = True
    return block


class Agent:
    def __init__(
        self,
        workspace: Path,
        ui: UI,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        permission_mode: str = "normal",
        client: Optional[anthropic.Anthropic] = None,
    ):
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"Unbekannte Stufe: {effort}")
        if permission_mode not in PERMISSION_MODES:
            raise ValueError(f"Unbekannter Modus: {permission_mode}")
        self.workspace = Path(workspace).resolve()
        self.ui = ui
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        self.client = client or anthropic.Anthropic(max_retries=4)
        self.toolbox = ToolBox(self.workspace, approver=self._approve)
        self.system = build_system_prompt(self.workspace)
        self.tools = TOOL_DEFINITIONS + SERVER_TOOLS
        self.messages: list[dict] = []
        self.usage = Usage()
        self._always_allowed: set[str] = set()
        self._pending: Optional[tuple[list[Any], list[dict]]] = None  # (tool_uses, results)

    # -- öffentliche API ------------------------------------------------------

    def reset(self) -> None:
        """Neues Gespräch beginnen (Kosten-Zähler bleibt erhalten)."""
        self.messages = []
        self._pending = None

    def run(self, content: Any) -> None:
        """Eine Nachricht des Benutzers bearbeiten, bis Claude fertig ist."""
        turn_start = len(self.messages)
        self.messages.append({"role": "user", "content": content})
        try:
            self._loop(turn_start)
        except KeyboardInterrupt:
            self._close_open_tool_calls("Der Benutzer hat die Ausführung unterbrochen.")
            self.ui.warn("Unterbrochen. Du kannst einfach weiterschreiben.")
        except anthropic.BadRequestError:
            # Die Anfrage selbst war ungültig – diesen Schritt zurücknehmen, damit
            # der fehlerhafte Inhalt nicht jede weitere Anfrage blockiert.
            del self.messages[turn_start:]
            self._pending = None
            raise
        except anthropic.APIError:
            # Netzwerk-/Serverfehler: bisherige Schritte behalten, damit "weiter" funktioniert.
            if len(self.messages) == turn_start + 1:
                del self.messages[turn_start:]
            raise

    # -- Berechtigungen -------------------------------------------------------

    def _approve(self, kind: str, title: str, detail: str) -> bool:
        must_ask = self.permission_mode == "ask" or (self.permission_mode == "normal" and kind == "command")
        if not must_ask or kind in self._always_allowed:
            self.ui.show_change(kind, title, detail)
            return True
        answer = self.ui.approve(kind, title, detail)
        if answer == "always":
            self._always_allowed.add(kind)
            return True
        return answer == "yes"

    # -- Anfrage an Claude ----------------------------------------------------

    def _request_params(self) -> dict[str, Any]:
        betas = [COMPACTION_BETA]
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": self.system,
            "messages": self.messages,
            "tools": self.tools,
            # Denken ist immer an; "summarized" zeigt eine lesbare Zusammenfassung.
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.effort},
            # Automatisches Prompt-Caching: der wachsende Verlauf wird zwischengespeichert.
            "cache_control": {"type": "ephemeral"},
            "context_management": {"edits": [{"type": "compact_20260112"}]},
        }
        if self.model in FALLBACK_MODELS:
            betas.append(FALLBACK_BETA)
            params["fallbacks"] = "default"
        params["betas"] = betas
        return params

    def _stream_once(self) -> Any:
        self.ui.status("Lamar denkt nach …")
        try:
            with self.client.beta.messages.stream(**self._request_params()) as stream:
                for event in stream:
                    kind = event.type
                    if kind == "text":
                        self.ui.status(None)
                        self.ui.text(event.text)
                    elif kind == "thinking":
                        if event.thinking:
                            self.ui.status(None)
                        self.ui.thinking(event.thinking)
                    elif kind == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            self.ui.status(f"Lamar bereitet {block.name} vor …")
                        elif block.type == "fallback":
                            self.ui.info(
                                f"{block.from_.model} hat abgelehnt – {block.to.model} übernimmt die Antwort."
                            )
                        elif block.type == "compaction":
                            self.ui.status("Der Verlauf ist sehr lang und wird zusammengefasst …")
                    elif kind == "content_block_stop":
                        block = event.content_block
                        if block.type in ("text", "thinking"):
                            self.ui.block_end()
                        elif block.type == "server_tool_use":
                            # Läuft bei Anthropic (Websuche/-abruf) – nur anzeigen.
                            self.ui.status(None)
                            self.ui.tool_call(block.name, block.input)
                        self.ui.status("Lamar denkt nach …")
                return stream.get_final_message()
        finally:
            self.ui.status(None)

    # -- Schleife -------------------------------------------------------------

    def _loop(self, turn_start: int) -> None:
        json_retries = 0
        pauses = 0
        announced_model: Optional[str] = None

        for _ in range(MAX_STEPS_PER_TURN):
            try:
                response = self._stream_once()
            except ValueError:
                # Das SDK konnte gestreamtes Werkzeug-JSON gar nicht lesen. Der Block
                # ist unvollständig, es gibt keine tool_use_id – Anfrage neu stellen.
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    self.ui.warn("Claude hat wiederholt fehlerhafte Werkzeug-Eingaben erzeugt. Abbruch.")
                    return
                self.ui.warn("Fehlerhafte Werkzeug-Eingabe empfangen – neuer Versuch …")
                continue
            json_retries = 0
            self.usage.add(response)

            if response.stop_reason == "refusal":
                del self.messages[turn_start:]
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                extra = f" (Kategorie: {category})" if category else ""
                self.ui.warn(
                    f"Diese Anfrage wurde abgelehnt{extra}. Sie wurde aus dem Verlauf entfernt – "
                    "formuliere sie bitte anders."
                )
                return

            if response.model != self.model and response.model != announced_model:
                announced_model = response.model
                self.ui.info(f"Diese Antwort kommt von {response.model} (Ersatzmodell).")

            content = sanitize_after_fallback(response.content)
            self.messages.append({"role": "assistant", "content": content})

            if response.stop_reason == "pause_turn":
                # Server-Werkzeug (z. B. Websuche) läuft länger: einfach fortsetzen.
                pauses += 1
                if pauses > MAX_PAUSE_CONTINUATIONS:
                    self.ui.warn("Die Websuche hat zu lange gedauert. Schreib 'weiter', um fortzufahren.")
                    return
                continue

            tool_uses = [b for b in content if b.type == "tool_use"]
            if not tool_uses:
                if response.stop_reason == "max_tokens":
                    self.ui.warn("Die Antwort wurde wegen des Längenlimits abgeschnitten. Schreib 'weiter'.")
                return

            if response.stop_reason == "max_tokens":
                # Abgeschnittene Werkzeug-Eingaben sehen oft gültig aus – nie ausführen.
                truncated = ToolOutcome(
                    "Not executed: your response hit the output length limit, so this tool input "
                    "was cut off. Split the work into smaller steps (for example, write a large "
                    "file in several parts with edit_file).",
                    is_error=True,
                )
                self.messages.append(
                    {"role": "user", "content": [tool_result_block(b.id, truncated) for b in tool_uses]}
                )
                continue

            self._run_tools(tool_uses)
        else:
            self.ui.warn(f"Nach {MAX_STEPS_PER_TURN} Schritten angehalten. Schreib 'weiter', um fortzufahren.")

    def _run_tools(self, tool_uses: list[Any]) -> None:
        results: list[dict] = []
        self._pending = (tool_uses, results)
        for block in tool_uses:
            self.ui.tool_call(block.name, block.input)
            outcome = self.toolbox.execute(block.name, block.input)
            self.ui.tool_result(block.name, outcome)
            results.append(tool_result_block(block.id, outcome))
        self._pending = None
        # Alle Ergebnisse in EINER Nachricht zurückgeben.
        self.messages.append({"role": "user", "content": results})

    def _close_open_tool_calls(self, reason: str) -> None:
        """Nach einer Unterbrechung jedem offenen Werkzeugaufruf ein Ergebnis geben."""
        if self._pending is None:
            return
        tool_uses, results = self._pending
        self._pending = None
        done = {r["tool_use_id"] for r in results}
        for block in tool_uses:
            if block.id not in done:
                results.append(tool_result_block(block.id, ToolOutcome(reason, is_error=True)))
        self.messages.append({"role": "user", "content": results})
