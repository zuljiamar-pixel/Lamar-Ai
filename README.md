# Lamar AI 🤖

**Lamar AI** ist eine KI für dein Terminal, die für dich programmiert – und auch fast alles andere kann.
Du schreibst auf Deutsch (oder jeder anderen Sprache), was du brauchst, und Lamar erledigt es selbst:
Code schreiben, Fehler finden und reparieren, Tests ausführen, Projekte erklären, im Internet recherchieren.

Im Hintergrund arbeitet **Claude Opus 5.5** von Anthropic, eines der stärksten Modelle fürs Programmieren.

```
› Erstelle ein Snake-Spiel in Python mit pygame und teste, ob es startet

Ich lege das Spiel an und prüfe danach, ob es fehlerfrei startet.
● Schreibe snake.py
  ⎿ 142 Zeilen erstellt
● $ python3 -c "import ast; ast.parse(open('snake.py').read())"
  ⎿ Exit-Code: 0
Fertig! snake.py ist fertig – starte es mit `python3 snake.py`. Steuerung: Pfeiltasten …
```

## Was Lamar kann

| Fähigkeit | Wie |
|---|---|
| Code in jeder Sprache schreiben (Python, JavaScript, Java, C++, Rust, Go, …) | Dateien erstellen und bearbeiten |
| Bestehende Projekte verstehen und erklären | Dateien lesen, Ordner auflisten, Code durchsuchen |
| Fehler finden und reparieren | Programme und Tests ausführen, Ausgabe lesen, Code korrigieren |
| Aktuelle Infos nachschlagen (neue Versionen, Fehlermeldungen, Doku) | Websuche und Webseiten lesen |
| Bilder verstehen (Screenshots, Diagramme, UI-Entwürfe) | `/bild pfad.png` oder Bilddateien im Projekt |
| Alles andere: erklären, rechnen, planen, Texte schreiben, lernen | einfach fragen |

Lamar denkt vor schwierigen Aufgaben nach (du siehst eine kurze Zusammenfassung in grau),
prüft seine Arbeit selbst und sagt dir ehrlich, was funktioniert hat und was nicht.

## Installation

Du brauchst **Python 3.10 oder neuer** ([python.org](https://www.python.org/downloads/)).

```bash
git clone https://github.com/zuljiamar-pixel/Lamar-Ai.git
cd Lamar-Ai
pip install .
```

### API-Schlüssel einrichten

Lamar braucht einen API-Schlüssel von Anthropic:

1. Gehe auf <https://console.anthropic.com/> und melde dich an.
2. Unter **API Keys** einen neuen Schlüssel erstellen und kopieren.
3. Lege eine Datei **`.env`** in deinem Projektordner an (oder `~/.lamar.env` für alle Projekte):

   ```
   ANTHROPIC_API_KEY=sk-ant-dein-schluessel
   ```

   Alternativ als Umgebungsvariable: `export ANTHROPIC_API_KEY=sk-ant-...` (Linux/macOS)
   bzw. `setx ANTHROPIC_API_KEY sk-ant-...` (Windows, danach neues Terminal öffnen).

> ⚠️ Teile deinen Schlüssel mit niemandem und lade ihn nie auf GitHub hoch.
> Die `.gitignore` dieses Projekts schließt `.env` schon aus.

## Benutzung

Wechsle in den Ordner deines Projekts und starte Lamar:

```bash
cd mein-projekt
lamar
```

Oder gib den Ordner direkt an: `lamar pfad/zum/projekt`. Ohne Installation geht auch `python -m lamar`.

Eine einzelne Aufgabe ohne Chat:

```bash
lamar -p "Schreib Tests für utils.py und führe sie aus"
```

### Befehle im Chat

| Befehl | Was er macht |
|---|---|
| `/hilfe` | Hilfe anzeigen |
| `/neu` | neues Gespräch beginnen |
| `/bild <pfad> [frage]` | ein Bild mitschicken |
| `/modus ask\|normal\|auto` | Berechtigungen ändern (siehe unten) |
| `/effort low\|medium\|high\|xhigh\|max` | wie gründlich Lamar nachdenkt |
| `/kosten` | verbrauchte Tokens und geschätzte Kosten |
| `/exit` | beenden |

**Tasten:** Enter = senden · Esc dann Enter (oder Alt+Enter) = neue Zeile ·
Strg+C während Lamar arbeitet = unterbrechen · Strg+D = beenden

### Berechtigungen (Sicherheit)

Lamar kann nur Dateien **innerhalb des Projektordners** lesen und ändern. Wie viel es ohne Nachfrage darf, bestimmst du:

| Modus | Verhalten |
|---|---|
| `ask` | fragt vor **jeder** Dateiänderung und jedem Befehl |
| `normal` *(Standard)* | ändert Dateien selbst (du siehst jede Änderung als Diff), fragt vor jedem Befehl |
| `auto` | macht alles ohne Nachfrage – nur nutzen, wenn du Lamar vertraust und das Projekt in Git gesichert ist |

Bei jeder Abfrage kannst du mit `j` (ja), `n` (nein) oder `i` (immer, für diese Sitzung) antworten.
Starten mit einem bestimmten Modus: `lamar --modus ask`.

### Projekt-Notizen: `LAMAR.md`

Lege eine Datei `LAMAR.md` in dein Projekt, und Lamar liest sie bei jedem Start. Schreib dort hinein,
was Lamar über dein Projekt wissen soll, zum Beispiel:

```markdown
- Das ist ein Discord-Bot in Python 3.12 mit discord.py.
- Tests laufen mit `pytest`.
- Kommentare im Code bitte auf Deutsch.
```

### Denk-Stufe (`/effort`)

| Stufe | Wofür |
|---|---|
| `low` | schnelle, einfache Fragen – am günstigsten |
| `medium` | normale Aufgaben |
| `high` *(Standard)* | Programmieren, Fehlersuche |
| `xhigh` / `max` | sehr schwierige Probleme – langsamer und teurer |

## Kosten

Die Nutzung der Claude-API kostet Geld (Abrechnung über dein Anthropic-Konto). Claude Opus 5.5 kostet
4 $ pro 1 Mio. Eingabe-Tokens und 20 $ pro 1 Mio. Ausgabe-Tokens; eine Websuche kostet 1 Cent.
Lamar nutzt **Prompt-Caching**, dadurch wird der Gesprächsverlauf bei jeder weiteren Anfrage deutlich günstiger.
Mit `/kosten` siehst du jederzeit, was die aktuelle Sitzung ungefähr gekostet hat.
Tipp: Lege im Anthropic-Konto ein monatliches Ausgabenlimit fest.

## Wie es funktioniert (für Neugierige)

```
lamar/
├── cli.py      Terminal-Oberfläche: Eingabe, farbige Ausgabe, Befehle, Abfragen
├── agent.py    Agenten-Schleife: Anfrage an Claude streamen → Werkzeuge ausführen → wiederholen
├── tools.py    Werkzeuge: Dateien lesen/schreiben/bearbeiten, suchen, Befehle ausführen
└── prompts.py  System-Prompt (Anweisungen, wie Lamar arbeiten soll)
```

- **Agenten-Schleife:** Claude entscheidet selbst, welche Werkzeuge es braucht. Lamar führt sie lokal aus
  und schickt die Ergebnisse zurück, bis die Aufgabe erledigt ist.
- **Streaming:** Text, Denk-Zusammenfassungen und Werkzeugaufrufe erscheinen sofort.
- **Sicherheit:** Jede Werkzeug-Eingabe wird geprüft, bevor etwas passiert; Pfade außerhalb des
  Projektordners werden abgelehnt; Befehle laufen erst nach deiner Erlaubnis.
- **Ersatzmodell:** Lehnt ein Sicherheitsfilter eine harmlose Anfrage fälschlich ab, übernimmt automatisch
  ein anderes Claude-Modell (`fallbacks`).
- **Lange Sitzungen:** Wird der Verlauf sehr lang, fasst die API ihn automatisch zusammen (Compaction).

### Tests

```bash
pip install ".[dev]"
pytest
```

Die Tests brauchen keinen API-Schlüssel: Ein simulierter Claude-Server prüft die komplette Schleife.
