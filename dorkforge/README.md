# DorkForge — Lead Agent

Vom deutschen Satz zur fertigen Dork-Query, von der Seite zum bewerteten Lead.
Eine FastAPI-App, ein Browser-UI, keine Build-Kette.

```bash
./dorkforge/run.sh              # → http://localhost:8000
.venv/bin/python -m pytest tests -q   # 47 Tests, offline
```

Erster Run ohne Netz: im UI **„Demo-Daten"** klicken → **„▶ Agent starten"**.
Der Demo-Korpus (`demo_site/`) wird lokal ausgeliefert, es verlässt kein Paket die Maschine.

---

## Was der Agent tut

| Stufe | Modul | Ergebnis |
|---|---|---|
| **1 Parse** | `parser.py` | Satz → Domain, Keywords, Ort, Name, Dateityp, Kategorie |
| **2 Forge** | `library.py` + `forge.py` | 54 Rezepte → fertige Queries, Risiko-gefiltert, mit Such-URL pro Engine |
| **3 Harvest** | `fetch.py` + `harvest.py` | Seiten abrufen (Scope-Gate, robots.txt, Rate-Limit) → E-Mails, Telefone, Personen, Firmendaten |
| **4 Score** | `leads.py` | Dedupe, 0–100-Score mit Begründung, CSV/JSONL/Markdown |

### Warum kein SERP-Scraping

Der Agent **baut** die Dorks — öffnen tust du sie. Die Treffer-URLs, die dir gefallen,
fütterst du als *Seeds* ein, und nur die werden geharvestet. Das hält dich aus den
ToS von Google/Bing raus, ist deutlich robuster als Proxy-Roulette, und du behältst
die Kontrolle darüber, welche Seite überhaupt angefasst wird.

Wer Seiten aus Sitemaps statt aus einer Suche ziehen will, trägt sie einfach als
Seeds ein — gleiche Pipeline.

### Der Scope-Gate

`scope.txt` ist die Grenze:

* Jede Seed-URL wird gegen die Liste geprüft (`acme.ch` erlaubt auch `shop.acme.ch`,
  aber nicht `acme.ch.evil.com`).
* Ohne Eintrag ist der Gate aus — das UI zeigt dann eine Warnung, und das ist
  bewusst nur für Demo-Betrieb gedacht.
* Zusätzlich: private/loopback/link-local Ziele werden immer verworfen (SSRF-Schutz),
  robots.txt wird respektiert, pro Host ein Request mit Pause.

### Datenschutz (DSG/nDSG/GDPR)

Das Tool sammelt **öffentlich publizierte Geschäftskontakte** — Impressum, Team-Seite,
Presse, HR. Genau dafür gibt es Art. 6 Abs. 1 lit. f DSGVO (berechtigtes Interesse an
B2B-Kommunikation) und die Parallelnorm im revDSG. Was das praktisch heißt:

* Jeder Lead speichert seine **Quell-URL** — du kannst jede Aussage belegen.
* Rolle von Person: `info@` ist ein Rollen-Postfach, `marta.brunner@` ist persönlich.
  Erstere sind für Outreach unkritisch, letztere brauchen einen echten Grund.
* Kein Login-Bypass, keine Captcha-Solver, keine Newsletter-Hintertüren, keine
  private Adressen aus Impressum-Pflichtangaben für Werbung.
* Löschen ist ein Klick (`DELETE /api/leads`), Speicherung ist eine JSON-Datei bei dir.

Das ist eine technische Leitlinie, keine Rechtsberatung.

---

## Aufbau

```
dorkforge/
├── library.py         Rezept-Katalog, Rendering ([[optional]] Segmente), Validierung
├── data/library.json  54 Rezepte in 10 Kategorien, je mit Risiko + Use-Case
├── parser.py          Satz → Target (deterministisch, kein LLM)
├── forge.py           Target + Rezepte → Dorks, Varianten, Engine-URLs
├── fetch.py           Scope, SSRF-Guard, robots.txt, Throttle, Cache, Fixture-Fetcher
├── harvest.py         JSON-LD / microdata / mailto / tel / Text → Contacts
├── leads.py           Lead-Modell, Scoring, Dedupe, Persistenz, Export
├── pipeline.py        Job-Orchestrierung, Live-Events, Cancel
├── api.py             FastAPI (REST + WebSocket + statisches UI)
├── static/            index.html · app.js · styles.css (kein Framework)
└── demo_site/         Offline-Korpus für den ersten Run
```

### Rezept-Format

```json
{ "id": 30, "category": "documents", "risk": "low",
  "q": "site:{domain} (filetype:xlsx OR filetype:csv) [[{kw}]]",
  "desc": "Spreadsheets", "use": "Listen und Preisblätter" }
```

Slots: `{domain} {kw} {ext} {place} {name}`.
Alles in `[[ … ]]` verschwindet, wenn ein Slot darin leer ist — deshalb leakt nie
ein `{placeholder}` in die Ausgabe. Ein optionales Segment **ohne** Slot ist ein
Fehler und wird beim Laden abgelehnt (das war ein realer Bug beim Bau).

Neues Rezept: Eintrag in `library.json` ergänzen, `pytest` laufen lassen, fertig.

---

## API

| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/api/health` | Version, Demo-Modus, Scope, Bibliotheksgröße |
| GET | `/api/library?category=&risk=` | Rezept-Katalog |
| POST | `/api/parse` | Satz → strukturierte Felder |
| POST | `/api/forge` | Dorks bauen (ohne zu crawlen) |
| POST | `/api/variants` | Umformulierungen einer Query |
| POST | `/api/jobs` | Run starten (202) |
| GET | `/api/jobs/{id}?since=` | Status + Events ab Index |
| POST | `/api/jobs/{id}/cancel` | Abbrechen |
| WS | `/ws/jobs/{id}` | Live-Log |
| GET | `/api/leads` | Leads, Filter `min_score`, `q` |
| GET | `/api/leads/export?fmt=csv\|jsonl\|md` | Download |
| DELETE | `/api/leads` | Store leeren |
| GET/POST/DELETE | `/api/scope` | Scope pflegen |

Beispiel:

```bash
curl -X POST localhost:8000/api/jobs -H 'Content-Type: application/json' -d '{
  "prompt": "Einkauf und Ansprechpartner bei Acme, Aarau",
  "domain": "acme.ch",
  "seeds": ["https://acme.ch/kontakt", "https://acme.ch/team"],
  "max_pages": 8,
  "follow_links": true
}'
```

### Konfiguration

Alles über Umgebungsvariablen, siehe `.env.example`:
`DF_ENFORCE_SCOPE`, `DF_SCOPE_FILE`, `DF_DEMO`, `DF_DELAY`, `DF_CACHE_TTL`,
`DF_OUTPUT_DIR`, `DF_API_TOKEN`. Mit `DF_API_TOKEN` verlangt die API einen
`X-API-Token`-Header (WebSocket: `?token=`).

---

## Live-Events

```
job_started → target_parsed → dorks_ready → crawl_started
   → page_started → page_done / page_failed → lead_found …
   → job_finished
```

Jedes Event hat `ts`, `type` und einen flachen Payload. Das UI rendert daraus
Log, Statistik und Lead-Tabelle; ein anderer Client kann dieselbe Quelle nutzen.

---

## Score

| Signal | Punkte |
|---|---|
| E-Mail vorhanden | +30 |
| … davon persönliche Adresse | +10 |
| Telefon | +15 |
| Name der Kontaktperson | +10 |
| Entscheider-Rolle (CEO, Leiter, Geschäftsführer …) | +15 |
| verifizierte Firma (UID/Handelsregister/Land) | +5 |
| Kontaktseite als Quelle | +5 |
| E-Mail passt zum Namen | +10 |

Die Punkte stehen als `score_reasons` im Export — du siehst also immer, warum
ein Lead oben landet. Für andere Vertriebsmodelle: `DECISION_ROLES` und
`score_lead()` in `leads.py` sind die einzigen Stellen, die du anfassen musst.

---

## Bekannte Grenzen

* **Personen ohne Kontaktdaten** bleiben als Lead stehen (niedriger Score) — sie
  sind als Organigramm-Info nützlich, aber kein Outreach-Ziel.
* **Nur HTML** wird geharvestet; PDFs werden erkannt, aber nicht geparst.
* **Keine Duplikat-Erkennung über Firmennamen hinweg** — Dedupe läuft über
  E-Mail → Telefon → Name+Domain.
* **Kein Login, keine Captchas, keine Header-Fälschung.** Wenn eine Seite 403 sagt,
  ist das ein Ergebnis, kein Hindernis.
