# Contributing

Danke für dein Interesse an diesem Projekt! Es ist ein privates Hobby-/Heimprojekt,
das aber gerne auch von anderen genutzt und weiterentwickelt werden darf. Diese
kurze Anleitung beschreibt die Konventionen, die sich im Projekt bisher etabliert
haben.

## Voraussetzungen

- Backend: Python 3.12, siehe `backend/requirements.txt` und
  `backend/requirements-dev.txt`.
- Frontend-Tests: Node.js 20, siehe `frontend/tests/package.json`.
- Details zum lokalen Setup stehen in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Tests

Jede inhaltliche Änderung sollte von passenden Tests begleitet werden und die
komplette Suite muss vor einem Pull Request grün sein:

```bash
# Backend
cd backend
python -m pytest -q

# Frontend
cd frontend/tests
npm test
```

CI (`.github/workflows/ci.yml`) führt bei jedem Pull Request beide Suiten sowie
einen Docker-Build aus.

## Branches und Commits

- Kleine, thematisch fokussierte Branches (`feature/…`, `fix/…`, `chore/…`) statt
  eines langlebigen Branches für mehrere Themen gleichzeitig.
- Commit-Nachrichten auf Deutsch, im Stil "was hat sich geändert und warum" –
  ein kurzer Titel reicht bei kleinen Änderungen, größere Änderungen bekommen
  zusätzlich einen erklärenden Absatz.
- Merges in `master` möglichst mit `--no-ff`, damit der jeweilige
  Feature-Zusammenhang im Log sichtbar bleibt.

## Code-Stil

- Kommentare erklären bevorzugt das **Warum** (Randfälle, Trade-offs, historisch
  gewachsene Entscheidungen), nicht nochmal wortwörtlich das Was – der Code
  selbst sollte für sich lesbar sein.
- Bewusst schlanke Abhängigkeiten: siehe z.B. `backend/app/auth.py`, das ohne
  externe Auth-Bibliothek auskommt, um kein zusätzliches Compile-/Build-Gewicht
  in ein schlankes Docker-Image zu ziehen. Neue Abhängigkeiten bitte nur bei
  echtem Mehrwert einführen.
- Frontend ist bewusst framework-frei (Vanilla JS/HTML/CSS + Chart.js) – siehe
  [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) für Details zu Struktur und
  Test-Setup (jsdom-basierte Tests ohne Browser).
- Deutschsprachige Kommentare/Doku, da das Projekt in erster Linie für den
  deutschsprachigen Raum (private PV-Anlagen) entstanden ist.

## Dokumentation

Nutzer-/betriebsrelevante Änderungen (neue Umgebungsvariablen, neue
Endpunkte, geänderte Berechnungen) bitte direkt mit anpassen:

- [README.md](README.md) – Kurzüberblick, Schnellstart.
- [docs/INSTALLATION.md](docs/INSTALLATION.md) – Einrichtung, Benutzerverwaltung, Mail-Report.
- [docs/CALCULATIONS.md](docs/CALCULATIONS.md) – Dashboard-Kennzahlen, Mehrgeräte-Logik.
- [docs/DATA_IMPORT.md](docs/DATA_IMPORT.md) – Automatischer Logdaten-Import.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) – Betrieb, Logs, Backup.
- [docs/API.md](docs/API.md) – API-Referenz.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) – Entwicklung und Tests.

## Sicherheit

Bitte keine echten Zugangsdaten, IP-Adressen oder personenbezogenen Daten in
Commits, Issues oder Pull Requests einfügen – auch nicht in Beispieldaten oder
Screenshots. Verdacht auf ein Sicherheitsproblem gerne über ein privates
GitHub-Issue oder direkt an die Repository-Inhaber melden statt öffentlich zu
diskutieren.

## Pull Requests

- Kurze Beschreibung, was sich ändert und warum.
- Bei UI-Änderungen gerne ein Screenshot/GIF.
- Tests grün, Dokumentation aktualisiert – siehe oben.
