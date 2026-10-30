# PROJECT_STATE — DorkForge Lead Agent

> Ein-File-Handoff. Wenn du (Mensch oder Agent) in einem **neuen Chat** oder auf
> einer **frischen Branch** aufsetzt: das hier zuerst lesen, dann `dorkforge/README.md`.

Stand: 2026-10-30 · Branch `arena/1f034a80-hez-code` · PR #1 gegen `main`

---

## 1. In 60 Sekunden startklar

```bash
git checkout arena/1f034a80-hez-code     # oder: main, sobald PR #1 gemerged ist
python3 -m venv .venv && . .venv/bin/activate
pip install -r dorkforge/requirements.txt
pytest tests -q                          # erwartet: 47 passed
uvicorn dorkforge.api:app --reload --port 8000
# → http://localhost:8000   · API-Docs: http://localhost:8000/api/docs
```

Erster Klick-Test ohne Netz: UI → **„Demo-Daten"** → **„▶ Agent starten"**.
Erwartet: 5 Seiten, 13 Leads, Top-Lead Score 100 (Marta Brunner, Geschäftsführerin).
Kommt etwas anderes: unten „Traps" lesen, bevor du Code anfasst.

---

## 2. Was das Ding ist

Vom deutschen Satz zur fertigen Dork-Query, von der Seite zum bewerteten B2B-Lead.

| Stufe | Datei | Kern |
|---|---|---|
| Parse | `dorkforge/parser.py` | Satz → `Target` (domain, keywords, places, names, filetype, category). Deterministisch, **kein LLM**. |
| Forge | `dorkforge/library.py`, `forge.py` | 54 Rezepte → Queries; `[[…]]`-Segmente verschwinden bei leerem Slot; Risiko-Ceiling filtert |
| Fetch | `dorkforge/fetch.py` | Scope-Gate → SSRF-Guard → robots.txt → Throttle → Cache → HTTP |
| Harvest | `dorkforge/harvest.py` | JSON-LD ⇢ microdata ⇢ `mailto:`/`tel:` ⇢ Klartext; Obfuskation; E.164 |
| Score | `dorkforge/leads.py` | Dedupe (E-Mail → Telefon → Name+Domain), 0–100 mit `score_reasons` |
| Orchestrierung | `dorkforge/pipeline.py` | `Job` mit Event-Log, `Runner`, `JobManager` (Cancel, History) |
| API/UI | `dorkforge/api.py`, `static/` | REST + WebSocket, framework-freies Frontend |

Datenfluss einer Page:
`fetch.Page` → `harvest.Harvest` → `pipeline._build_leads()` → `LeadStore.upsert()` → `score_lead()` → Export.

---

## 3. Invarianten (nicht brechen)

1. **Kein SERP-Scraping.** Der Agent *baut* Dorks; Seiten kommen als Seed-URLs rein.
   Wer hier einen Google- oder Bing-Parser einbaut, bricht ToS **und** die Produktidee.
2. **Scope-Gate ist die Grenze.** Jede URL durch `Scope.require()`/`allows()`.
   `acme.ch.evil.com` darf **nicht** matchen — dafür gibt es einen Test.
3. **SSRF-Guard bleibt vor jedem Request.** `assert_public_host()` in `fetch.py`.
4. **Jeder Lead behält seine `source_url`.** Ohne Quelle kein Lead — das ist die
   Nachweisfähigkeit (DSG/nDSG).
5. **Kein Bypass-Tooling.** Kein Captcha-Solver, keine Login-Tricks, keine
   Header-Fälschung, kein Proxy-Roulette. 401/403/429 ist ein Ergebnis.
6. **Rezepte werden beim Laden validiert.** Neues Rezept ohne Test → `Library.validate()` schlägt fehl.

---

## 4. Traps (hier bin ich schon reingelaufen)

| Trap | Symptom | Fix / Regel |
|---|---|---|
| `[[optionales Segment]]` ohne Slot | Segment verschwindet nie, toter Text | `validate()` lehnt es ab — Segment muss einen Slot enthalten, sonst Pflicht machen |
| E-Mail-Scan trifft Domain | `marta.brunner@acme.ch` → domain `marta.brunner` | E-Mails **vor** der Domain-Suche maskieren (`parser.parse`) |
| UID als Telefon | `CHE-123.456.789` → `+123456789` | `harvest.looks_like_identifier()` |
| Telefon-Duplikate | gleiche Nummer als Personen-Lead **und** Telefon-Lead | Vergleich immer über `normalize_phone()`/E.164, nie über Rohstring |
| Firma fehlt | Team-Seite hat kein Org-JSON-LD | `LeadStore.backfill_domain()` nach dem Crawl |
| `upsert()`-Rückgabe | zweites Flag heißt **`created`**, nicht „changed" | nicht als „Neu vs. Update"-Metrik verwenden ohne Doku zu lesen |
| Demo-Flag global | App im Demo-Modus → Live-Modus unmöglich | `RunRequest.demo` ist **per Request**; `DF_DEMO=1` ist nur der Default |
| Sandbox ohne Internet | Live-Run schlägt fehl | Preview reicht nur bis github/npm/pypi — Live-Harvesting lokal testen |
| `next start` im alten `dashboard/` | ist **nicht** diese App | Altes Gerüst, nicht Teil von DorkForge |

---

## 5. Datei-Landkarte für typische Aufgaben

| Wenn du … willst | fass das an |
|---|---|
| neue Dork-Rezepte | `dorkforge/data/library.json` (id eindeutig, `risk` ∈ low/medium/high) |
| andere Kategorien/Erkennung | `parser.CATEGORY_HINTS`, `forge.preference` |
| Scoring anders gewichten | `leads.DECISION_ROLES`, `leads.score_lead()` |
| Export-Format | `leads.CSV_COLUMNS`, `export_csv/jsonl/markdown` |
| UI | `static/index.html` + `static/app.js` (kein Bundler, direkt ausgeliefert) |
| neue API-Route | `api.py`, danach Test in `tests/test_dorkforge.py` |
| Demo-Korpus erweitern | `dorkforge/demo_site/*.html` + Seed-Liste in `static/app.js` (`#btn-demo`) |

Tests sind offline und schnell (<1 s). Änderung ohne Test = nicht fertig.

---

## 6. Nächste Schritte (priorisiert)

1. **PDF-Text-Extraktion** — größter Lead-Zugewinn im DACH-Markt (Preislisten,
   Broschüren, Organigramme). Einstieg: `fetch.PoliteFetcher` liefert Nicht-HTML
   heute als Fehler; dort `application/pdf` abfangen, Text extrahieren
   (`pypdf`), dann `harvest.harvest_page(text_as_fake_html, …)`.
2. **Sitemap-Ingest** — `sitemap.xml` einer Scope-Domain als Seed-Quelle,
   gefiltert über `pipeline.page_category()`.
3. **Import der 3.294-Dork-CSV** aus dem DorkForge-Original → Mapping auf
   `library.Recipe`, Kategorie/`risk` aus den Spalten.
4. **Firmen-Dedupe über Namen** (heute nur E-Mail → Telefon → Name+Domain).
5. **`LeadStore` auf SQLite**, sobald >10k Leads oder mehrere Runs parallel.

---

## 7. Bekannte Grenzen

- Nur HTML wird geharvestet, PDFs werden erkannt aber nicht geparst.
- Personen ohne Kontaktdaten bleiben als Low-Score-Lead (Organigramm-Info).
- Keine Fuzzy-Dedupe über Firmennamen.
- Kein Rate-Limit-Backoff über 429 hinaus (Host wird für den Run verworfen).
- Rechtliches: technische Leitlinie, **keine Rechtsberatung** — B2B-Kontakte aus
  Impressum/Team/Presse, keine Privatadressen für Werbung.

---

## 8. Prompt zum Kopieren für einen neuen Chat

```text
Repo: prv6tc665g-a11y/Hez_code, Branch arena/1f034a80-hez-code (PR #1).
Lies PROJECT_STATE.md und dorkforge/README.md, dann:
python3 -m venv .venv && . .venv/bin/activate && pip install -r dorkforge/requirements.txt
pytest tests -q   # muss 47 passed sein
Starte danach uvicorn dorkforge.api:app --host 0.0.0.0 --port 8000
und bestätige mir den Demo-Run (5 Seiten, 13 Leads).

Aufgabe: <hier dein Ziel>
Nicht anfassen: models/, tools/, src/, dashboard/ (alter Stand).
```

---

## 9. Git-Stand

```bash
git log --oneline -3
# a541f65 Add DorkForge lead agent: dork library, scope-gated harvesting, lead scoring, web UI
# 2189b23 Add Stripe API abuse logic          (main)
```

- PR: https://github.com/prv6tc665g-a11y/Hez_code/pull/1 (`arena/1f034a80-hez-code` → `main`)
- `README-legacy.md` = frühere Projektbeschreibung, historisch, nicht löschen ohne Rückfrage
- Nicht committen: `.venv/`, `outputs/`, `.dorkforge-cache/` (siehe `.gitignore`)
