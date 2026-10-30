# DorkForge — Lead Agent

**Web-App zum Dorking und zur Lead-Generierung.** Satz eingeben, Dorks bauen,
Seiten im erlaubten Scope harvesten, Kontakte bewerten und exportieren.

```bash
./dorkforge/run.sh
# → http://localhost:8000   (API-Docs: /api/docs)
```

Erster Start ohne Netz: UI öffnen → **„Demo-Daten"** → **„▶ Agent starten"**.
Der Demo-Korpus liegt lokal in `dorkforge/demo_site/`.

**Neuer Chat / neue Branch?** [`PROJECT_STATE.md`](PROJECT_STATE.md) — Bootstrap,
Invarianten, Traps, nächste Schritte in einer Datei.

Vollständige Doku: [`dorkforge/README.md`](dorkforge/README.md)

---

## Was es kann

* **54 Dork-Rezepte** in 10 Kategorien (Contacts, People, Company, Careers,
  Documents, Directories, Events, Social, Infrastructure, Exposure) mit
  Risiko-Labels und optionalen Slot-Segmenten.
* **Deterministischer Parser** — deutscher oder englischer Satz rein, Domain,
  Keywords, Ort, Name und Dateityp raus. Kein LLM, keine Überraschungen.
* **Forge** — fertige Queries inkl. Such-URL für Google, Bing, DuckDuckGo,
  Startpage, Brave, Mojeek, plus Varianten auf Knopfdruck.
* **Harvest-Layer** — JSON-LD, microdata, `mailto:`/`tel:`-Links, Klartext,
  E-Mail-Obfuskation (`info [at] domain [dot] ch`), Telefon-Normalisierung auf E.164,
  Firmendaten aus Schema.org.
* **Lead-Scoring** — 0–100 mit nachvollziehbaren Gründen, Dedupe über
  E-Mail → Telefon → Name+Domain, Export als CSV / JSONL / Markdown.
* **Live-Log** über WebSocket, Jobs laufen abbrechbar im Hintergrund.

## Die Grenze, die eingebaut ist

* `scope.txt` bestimmt, welche Domains überhaupt geharvestet werden dürfen.
  Ohne Eintrag ist der Gate aus und das UI warnt.
* Private, Loopback- und Link-Local-Ziele werden immer verworfen (SSRF-Schutz).
* robots.txt wird respektiert, pro Host ein Request mit Pause, 12 h Cache.
* Kein SERP-Scraping: der Agent baut die Dorks, **du** öffnest sie, und die
  Treffer-URLs fütterst du als Seeds ein. Das ist ToS-sauber und robuster.
* Kein Login-Bypass, keine Captcha-Solver, keine Header-Fälschung.

Gedacht für B2B-Recherche auf öffentlich publizierten Geschäftskontakten
(Impressum, Team, Presse, HR) — jede Aussage ist über die Quell-URL belegbar.
Das ist eine technische Leitlinie, keine Rechtsberatung.

## Repo-Aufbau

| Pfad | Inhalt |
|---|---|
| `dorkforge/` | **Die App** — FastAPI, Bibliothek, Parser, Harvesting, UI |
| `tests/` | 47 Tests, laufen offline (`pytest`) |
| `scope.txt` | Allow-List für das Harvesting |
| `README-legacy.md` | Frühere Projektbeschreibung, historisch |

Die Ordner `models/`, `tools/`, `src/` und `dashboard/` gehören zum alten Stand
dieses Repos (u. a. Finanz-/Kartendaten-Tooling). Sie sind **nicht** Teil dieser
App, werden von ihr nicht importiert und nicht gestartet.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

Abgedeckt: Bibliotheks-Validierung, Slot-Rendering, Parser, Obfuskations-Dekodierung,
JSON-LD-Harvesting, UID/Telefon-Verwechslung, Scope- und SSRF-Gate, Lead-Dedupe,
Persistenz, End-to-End-Run inkl. API und Abbruch.
