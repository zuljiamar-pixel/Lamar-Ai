"""Werkzeuge, mit denen Lamar AI auf deinem Computer arbeitet.

Jedes Werkzeug hat
  * eine Definition für die Claude-API (Name, Beschreibung, JSON-Schema),
  * ein Pydantic-Modell, das die Eingabe des Modells prüft, bevor etwas passiert,
  * eine Implementierung.

Alle Dateipfade werden auf den Arbeitsordner (Workspace) beschränkt:
Pfade, die mit ``..``, absoluten Pfaden oder Symlinks hinausführen, werden abgelehnt.
"""

from __future__ import annotations

import base64
import difflib
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Maximale Größe einer Werkzeug-Antwort, damit der Kontext nicht explodiert.
MAX_OUTPUT_CHARS = 40_000
MAX_READ_LINES = 2_000
MAX_LINE_CHARS = 2_000
IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".idea", ".tox",
    "target", ".gradle", ".dart_tool",
}
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class ToolError(Exception):
    """Fehler, der als ``is_error``-Ergebnis an das Modell zurückgeht."""


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    skipped = len(text) - len(head) - len(tail)
    return f"{head}\n\n[... {skipped} Zeichen gekürzt ...]\n\n{tail}"


# ---------------------------------------------------------------------------
# Eingabe-Modelle (Validierung der Modell-Ausgabe)
# ---------------------------------------------------------------------------


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileInput(_Input):
    path: str
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=MAX_READ_LINES, ge=1, le=10_000)


class WriteFileInput(_Input):
    path: str
    content: str


class EditFileInput(_Input):
    path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class ListDirectoryInput(_Input):
    path: str = "."
    depth: int = Field(default=2, ge=1, le=6)


class FindFilesInput(_Input):
    pattern: str
    path: str = "."


class SearchCodeInput(_Input):
    pattern: str
    path: str = "."
    glob: Optional[str] = None
    case_insensitive: bool = False


class RunCommandInput(_Input):
    command: str
    timeout: int = Field(default=120, ge=1, le=1800)


# ---------------------------------------------------------------------------
# Werkzeug-Definitionen für die API
# ---------------------------------------------------------------------------


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        # Große Eingaben (z. B. ganze Dateien) werden gestreamt, während sie entstehen.
        # Dadurch prüft der Server die Eingabe nicht mehr – das machen wir selbst (Pydantic).
        "eager_input_streaming": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS: list[dict] = [
    _tool(
        "read_file",
        "Read a file from the workspace. Returns the content with line numbers "
        "(format: line number, tab, line). Use this before editing a file so you know "
        "its exact current content. For large files use offset/limit to read a range. "
        "Image files (png, jpg, gif, webp) are returned as images you can look at.",
        {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "offset": {"type": "integer", "description": "1-based line number to start at (default 1)."},
            "limit": {"type": "integer", "description": f"Maximum number of lines (default {MAX_READ_LINES})."},
        },
        ["path"],
    ),
    _tool(
        "write_file",
        "Create a new file or completely overwrite an existing one with the given content. "
        "Parent directories are created automatically. Prefer edit_file for small changes "
        "to existing files.",
        {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "content": {"type": "string", "description": "The complete new file content."},
        },
        ["path", "content"],
    ),
    _tool(
        "edit_file",
        "Replace an exact piece of text in an existing file. old_string must match the "
        "file byte for byte (including indentation) and must be unique in the file, "
        "unless replace_all is true. Include enough surrounding lines to make it unique.",
        {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
        },
        ["path", "old_string", "new_string"],
    ),
    _tool(
        "list_directory",
        "List files and folders as a tree. Use this to get an overview of a project. "
        "Skips folders like .git, node_modules and virtual environments.",
        {
            "path": {"type": "string", "description": "Directory relative to the workspace root (default '.')."},
            "depth": {"type": "integer", "description": "How many levels deep to list (1-6, default 2)."},
        },
        [],
    ),
    _tool(
        "find_files",
        "Find files by glob pattern, e.g. '**/*.py' or 'src/**/test_*.ts'. "
        "Returns matching paths, most recently modified first.",
        {
            "pattern": {"type": "string", "description": "Glob pattern."},
            "path": {"type": "string", "description": "Directory to search in (default '.')."},
        },
        ["pattern"],
    ),
    _tool(
        "search_code",
        "Search file contents with a regular expression (like grep). Returns matching "
        "lines as path:line:text. Use this to find where functions, classes or strings "
        "are defined or used.",
        {
            "pattern": {"type": "string", "description": "Regular expression."},
            "path": {"type": "string", "description": "Directory or file to search (default '.')."},
            "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'."},
            "case_insensitive": {"type": "boolean", "description": "Ignore case (default false)."},
        },
        ["pattern"],
    ),
    _tool(
        "run_command",
        "Run a shell command in the workspace and return its output (stdout and stderr) "
        "and exit code. Use it to run programs, tests, linters, builds, git, package "
        "managers and so on. Commands must not wait for keyboard input (use flags like "
        "-y). Do not start servers that never exit unless you put them in the background.",
        {
            "command": {"type": "string", "description": "The shell command to run."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 120, max 1800)."},
        },
        ["command"],
    ),
]

# Server-Werkzeuge: laufen bei Anthropic, wir müssen nichts implementieren.
SERVER_TOOLS: list[dict] = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]

INPUT_MODELS: dict[str, type[_Input]] = {
    "read_file": ReadFileInput,
    "write_file": WriteFileInput,
    "edit_file": EditFileInput,
    "list_directory": ListDirectoryInput,
    "find_files": FindFilesInput,
    "search_code": SearchCodeInput,
    "run_command": RunCommandInput,
}

# Werkzeuge, die etwas verändern (für die Berechtigungs-Abfrage).
FILE_CHANGING_TOOLS = {"write_file", "edit_file"}
COMMAND_TOOLS = {"run_command"}


# ---------------------------------------------------------------------------
# Implementierung
# ---------------------------------------------------------------------------

# approver(kind, title, detail) -> bool ; kind ist "file" oder "command"
Approver = Callable[[str, str, str], bool]


@dataclass
class ToolOutcome:
    content: Any  # str oder Liste von Content-Blöcken (Text/Bild)
    is_error: bool = False
    summary: str = ""  # kurze Zusammenfassung für die Anzeige


class ToolBox:
    def __init__(self, workspace: Path, approver: Optional[Approver] = None):
        self.root = Path(workspace).resolve()
        self.approver = approver or (lambda kind, title, detail: True)

    # -- Hilfsfunktionen ----------------------------------------------------

    def resolve(self, raw_path: str) -> Path:
        """Pfad auflösen und sicherstellen, dass er im Workspace bleibt."""
        if not raw_path or "\x00" in raw_path:
            raise ToolError("Ungültiger Pfad.")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        target = candidate.resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise ToolError(
                f"Pfad '{raw_path}' liegt außerhalb des Workspace ({self.root}). "
                "Nur Dateien im Workspace sind erlaubt."
            )
        return target

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix() or "."
        except ValueError:
            return str(path)

    # -- Einstiegspunkt -----------------------------------------------------

    def execute(self, name: str, raw_input: Any) -> ToolOutcome:
        model = INPUT_MODELS.get(name)
        if model is None:
            return ToolOutcome(f"Unbekanntes Werkzeug: {name}", is_error=True, summary="unbekannt")
        try:
            args = model.model_validate(raw_input)
        except ValidationError as exc:
            # Kann bei gestreamten Eingaben passieren (abgeschnittenes oder fehlerhaftes JSON).
            import json

            return ToolOutcome(
                json.dumps({"INVALID_JSON": json.dumps(raw_input, default=str)[:5000],
                            "errors": exc.errors(include_url=False, include_input=False)},
                           default=str),
                is_error=True,
                summary="ungültige Eingabe",
            )
        try:
            return getattr(self, f"_{name}")(args)
        except ToolError as exc:
            return ToolOutcome(str(exc), is_error=True, summary=str(exc)[:120])
        except Exception as exc:  # noqa: BLE001 - jeder Fehler geht ans Modell zurück
            return ToolOutcome(f"{type(exc).__name__}: {exc}", is_error=True, summary=type(exc).__name__)

    # -- Dateien ------------------------------------------------------------

    def _read_file(self, args: ReadFileInput) -> ToolOutcome:
        path = self.resolve(args.path)
        if not path.exists():
            raise ToolError(f"Datei nicht gefunden: {args.path}")
        if path.is_dir():
            raise ToolError(f"'{args.path}' ist ein Ordner. Nutze list_directory.")

        media_type = IMAGE_TYPES.get(path.suffix.lower())
        if media_type:
            data = path.read_bytes()
            if len(data) > MAX_IMAGE_BYTES:
                raise ToolError(f"Bild ist zu groß ({len(data) // 1024} KB, max. 5 MB).")
            return ToolOutcome(
                [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                  "data": base64.standard_b64encode(data).decode()}},
                    {"type": "text", "text": f"Bild {self.rel(path)} ({len(data) // 1024} KB)"},
                ],
                summary=f"Bild {self.rel(path)}",
            )

        raw = path.read_bytes()
        if b"\x00" in raw[:8000]:
            raise ToolError(f"'{args.path}' ist eine Binärdatei ({len(raw)} Bytes).")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if not lines:
            return ToolOutcome(f"(Datei {self.rel(path)} ist leer)", summary="leer")
        start = args.offset - 1
        chunk = lines[start : start + args.limit]
        numbered = []
        for i, line in enumerate(chunk, start=args.offset):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + " [... Zeile gekürzt]"
            numbered.append(f"{i:>6}\t{line}")
        text = "\n".join(numbered)
        end = start + len(chunk)
        if end < len(lines):
            text += f"\n\n[Zeilen {args.offset}-{end} von {len(lines)}. Mit offset={end + 1} weiterlesen.]"
        return ToolOutcome(truncate(text), summary=f"{len(chunk)} Zeilen")

    def _write_file(self, args: WriteFileInput) -> ToolOutcome:
        path = self.resolve(args.path)
        if path.is_dir():
            raise ToolError(f"'{args.path}' ist ein Ordner.")
        existed = path.exists()
        old = path.read_text(encoding="utf-8", errors="replace") if existed else ""
        detail = _diff(old, args.content, self.rel(path)) if existed else _preview(args.content)
        verb = "Überschreiben" if existed else "Erstellen"
        if not self.approver("file", f"{verb}: {self.rel(path)}", detail):
            raise ToolError("Der Benutzer hat diese Dateiänderung abgelehnt.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(args.content)
        n = args.content.count("\n") + (0 if args.content.endswith("\n") or not args.content else 1)
        action = "überschrieben" if existed else "erstellt"
        return ToolOutcome(f"Datei {self.rel(path)} {action} ({n} Zeilen).", summary=f"{n} Zeilen {action}")

    def _edit_file(self, args: EditFileInput) -> ToolOutcome:
        path = self.resolve(args.path)
        if not path.is_file():
            raise ToolError(f"Datei nicht gefunden: {args.path}")
        if args.old_string == args.new_string:
            raise ToolError("old_string und new_string sind identisch.")
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            text = fh.read()

        old, new = args.old_string, args.new_string
        count = text.count(old) if old else 0
        if count == 0 and "\r\n" in text and "\n" in old:
            # Datei mit Windows-Zeilenenden: Modell schreibt meist nur \n.
            old, new = old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")
            count = text.count(old)
        if count == 0:
            raise ToolError(
                "old_string wurde in der Datei nicht gefunden. Lies die Datei erneut mit "
                "read_file und kopiere den Text exakt (ohne Zeilennummern)."
            )
        if count > 1 and not args.replace_all:
            raise ToolError(
                f"old_string kommt {count}-mal vor. Gib mehr Kontext an, damit die Stelle "
                "eindeutig ist, oder setze replace_all=true."
            )
        updated = text.replace(old, new) if args.replace_all else text.replace(old, new, 1)
        if not self.approver("file", f"Bearbeiten: {self.rel(path)}", _diff(text, updated, self.rel(path))):
            raise ToolError("Der Benutzer hat diese Dateiänderung abgelehnt.")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(updated)
        n = count if args.replace_all else 1
        return ToolOutcome(f"{n} Stelle(n) in {self.rel(path)} ersetzt.", summary=f"{n} Stelle(n) ersetzt")

    # -- Überblick & Suche --------------------------------------------------

    def _list_directory(self, args: ListDirectoryInput) -> ToolOutcome:
        base = self.resolve(args.path)
        if not base.is_dir():
            raise ToolError(f"Kein Ordner: {args.path}")
        lines: list[str] = [f"{self.rel(base)}/"]
        count = 0
        limit = 1_000

        def walk(directory: Path, prefix: str, level: int) -> None:
            nonlocal count
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError:
                return
            for entry in entries:
                if count >= limit:
                    return
                if entry.is_dir() and entry.name in IGNORED_DIRS:
                    lines.append(f"{prefix}{entry.name}/ (übersprungen)")
                    continue
                count += 1
                if entry.is_dir():
                    lines.append(f"{prefix}{entry.name}/")
                    if level < args.depth:
                        walk(entry, prefix + "  ", level + 1)
                else:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    lines.append(f"{prefix}{entry.name} ({_human_size(size)})")

        walk(base, "  ", 1)
        if count >= limit:
            lines.append(f"[... nach {limit} Einträgen abgebrochen]")
        return ToolOutcome(truncate("\n".join(lines)), summary=f"{count} Einträge")

    def _iter_files(self, base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for name in filenames:
                yield Path(dirpath) / name

    def _find_files(self, args: FindFilesInput) -> ToolOutcome:
        base = self.resolve(args.path)
        if not base.is_dir():
            raise ToolError(f"Kein Ordner: {args.path}")
        pattern = args.pattern.replace("\\", "/")
        matches = []
        for file in self._iter_files(base):
            rel = file.relative_to(base).as_posix()
            if _glob_match(rel, pattern):
                try:
                    mtime = file.stat().st_mtime
                except OSError:
                    mtime = 0
                matches.append((mtime, self.rel(file)))
        matches.sort(reverse=True)
        if not matches:
            return ToolOutcome("Keine Dateien gefunden.", summary="0 Treffer")
        shown = [m[1] for m in matches[:300]]
        text = "\n".join(shown)
        if len(matches) > 300:
            text += f"\n[... {len(matches) - 300} weitere]"
        return ToolOutcome(text, summary=f"{len(matches)} Treffer")

    def _search_code(self, args: SearchCodeInput) -> ToolOutcome:
        base = self.resolve(args.path)
        if not base.exists():
            raise ToolError(f"Pfad nicht gefunden: {args.path}")
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "--line-number", "--no-heading", "--color=never", "--max-columns=500"]
            if args.case_insensitive:
                cmd.append("-i")
            if args.glob:
                cmd += ["--glob", args.glob]
            for d in IGNORED_DIRS:
                cmd += ["--glob", f"!{d}/"]
            cmd += ["-e", args.pattern, "--", str(base)]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=60)
            if proc.returncode not in (0, 1):
                raise ToolError(proc.stderr.strip() or "Suche fehlgeschlagen.")
            out_lines = [self._relativize_grep_line(line) for line in proc.stdout.splitlines()]
        else:
            flags = re.IGNORECASE if args.case_insensitive else 0
            try:
                regex = re.compile(args.pattern, flags)
            except re.error as exc:
                raise ToolError(f"Ungültiger regulärer Ausdruck: {exc}") from exc
            files = [base] if base.is_file() else self._iter_files(base)
            out_lines = []
            for file in files:
                if args.glob and not fnmatch.fnmatch(file.name, args.glob):
                    continue
                try:
                    raw = file.read_bytes()
                except OSError:
                    continue
                if b"\x00" in raw[:8000]:
                    continue
                for no, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    if regex.search(line):
                        out_lines.append(f"{self.rel(file)}:{no}:{line[:500]}")
                if len(out_lines) > 2_000:
                    break
        if not out_lines:
            return ToolOutcome("Keine Treffer.", summary="0 Treffer")
        text = "\n".join(out_lines[:500])
        if len(out_lines) > 500:
            text += f"\n[... {len(out_lines) - 500} weitere Treffer – Suche eingrenzen]"
        return ToolOutcome(truncate(text), summary=f"{len(out_lines)} Treffer")

    def _relativize_grep_line(self, line: str) -> str:
        root = str(self.root) + os.sep
        return line[len(root):] if line.startswith(root) else line

    # -- Befehle ------------------------------------------------------------

    def _run_command(self, args: RunCommandInput) -> ToolOutcome:
        if not self.approver("command", "Befehl ausführen", args.command):
            raise ToolError("Der Benutzer hat die Ausführung dieses Befehls abgelehnt.")
        popen_kwargs: dict[str, Any] = {
            "cwd": self.root,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": {**os.environ, "PAGER": "cat", "GIT_PAGER": "cat", "PYTHONUNBUFFERED": "1"},
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
            bash = shutil.which("bash")
            cmd: Any = [bash, "-c", args.command] if bash else args.command
            popen_kwargs["shell"] = not bash
        else:
            cmd = args.command
            popen_kwargs["shell"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)
        timed_out = False
        try:
            output, _ = proc.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            output, _ = proc.communicate()
        except KeyboardInterrupt:
            _kill(proc)
            proc.communicate()
            raise
        output = (output or "").rstrip()
        status = f"Zeitlimit von {args.timeout}s überschritten – Prozess beendet." if timed_out \
            else f"Exit-Code: {proc.returncode}"
        text = f"{truncate(output) if output else '(keine Ausgabe)'}\n\n[{status}]"
        return ToolOutcome(text, is_error=timed_out, summary=status)


# ---------------------------------------------------------------------------


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _glob_match(rel_path: str, pattern: str) -> bool:
    """Glob mit ``**``-Unterstützung (beliebig viele Ordner)."""
    regex = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            regex += ".*"
            i += 2
            continue
        if c == "*":
            regex += "[^/]*"
        elif c == "?":
            regex += "[^/]"
        elif c == "[":
            j = pattern.find("]", i)
            if j == -1:
                regex += re.escape(c)
            else:
                regex += pattern[i : j + 1]
                i = j
        else:
            regex += re.escape(c)
        i += 1
    if "/" not in pattern:
        # Muster ohne Ordner gilt für den Dateinamen in jedem Ordner (wie bei .gitignore).
        return re.fullmatch(regex, rel_path.rsplit("/", 1)[-1]) is not None
    return re.fullmatch(regex, rel_path) is not None


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _diff(old: str, new: str, name: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(), new.splitlines(), fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="", n=2
    )
    return truncate("\n".join(diff), 6_000) or "(keine Änderung)"


def _preview(content: str) -> str:
    lines = content.splitlines()
    head = "\n".join(lines[:40])
    if len(lines) > 40:
        head += f"\n... ({len(lines) - 40} weitere Zeilen)"
    return head


def platform_description() -> str:
    if sys.platform.startswith("win"):
        return "Windows (Befehle laufen in cmd.exe)"
    if sys.platform == "darwin":
        return "macOS (Befehle laufen in bash)"
    return "Linux (Befehle laufen in bash)"
