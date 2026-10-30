"""FastAPI surface for the DorkForge lead agent.

    uvicorn dorkforge.api:app --reload --port 8000

Environment:
    DF_SCOPE_FILE     path to scope.txt            (default ./scope.txt)
    DF_ENFORCE_SCOPE  "true"/"false"               (default: true when scope is non-empty)
    DF_DEMO           "1" serves the offline demo corpus instead of the network
    DF_OUTPUT_DIR     report directory             (default ./outputs)
    DF_LEADS_FILE     lead store json              (default ./outputs/leads.json)
    DF_CACHE_DIR      page cache                   (default ./.dorkforge-cache)
    DF_CACHE_TTL      cache seconds                (default 43200)
    DF_DELAY          seconds between same-host requests (default 1.5)
    DF_API_TOKEN      if set, required as X-API-Token or ?token=
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .fetch import FixtureFetcher, PoliteFetcher, Scope
from .forge import ENGINES, forge, search_url, variants
from .leads import LeadStore, export_csv, export_jsonl, export_markdown
from .library import Library
from .parser import normalize_domain, parse
from .pipeline import JobManager, RunRequest, Runner

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DEMO_DIR = BASE_DIR / "demo_site"

SCOPE_FILE = Path(os.getenv("DF_SCOPE_FILE", "scope.txt"))
OUTPUT_DIR = Path(os.getenv("DF_OUTPUT_DIR", "outputs"))
CACHE_DIR = Path(os.getenv("DF_CACHE_DIR", ".dorkforge-cache"))
TOKEN = os.getenv("DF_API_TOKEN", "").strip()
DEMO = os.getenv("DF_DEMO", "").strip() in {"1", "true", "yes"}


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_scope(demo: bool = DEMO) -> Scope:
    scope = Scope.load(SCOPE_FILE, enforce=False)
    explicit = os.getenv("DF_ENFORCE_SCOPE")
    if explicit is not None:
        scope.enforce = _truthy(explicit)
    else:
        scope.enforce = bool(scope.domains)
    if demo:
        scope.enforce = False
    return scope


LIBRARY = Library.load()
STORE = LeadStore(OUTPUT_DIR / "leads.json")
SCOPE = load_scope()


def build_fetcher(scope: Scope, demo: bool = DEMO):
    if demo:
        return FixtureFetcher(root=DEMO_DIR, scope=scope)
    return PoliteFetcher(
        scope=scope,
        cache_dir=CACHE_DIR,
        cache_ttl=int(os.getenv("DF_CACHE_TTL", "43200")),
        delay=float(os.getenv("DF_DELAY", "1.5")),
    )


RUNNER = Runner(
    library=LIBRARY,
    store=STORE,
    output_dir=OUTPUT_DIR,
    fetcher_factory=lambda scope: build_fetcher(scope, demo=False),
    demo_fetcher_factory=lambda scope: build_fetcher(scope, demo=True),
    scope=SCOPE,
)
JOBS = JobManager(RUNNER)

app = FastAPI(
    title="DorkForge Lead Agent",
    version=__version__,
    description="Scope-gated dorking and contact harvesting for authorised lead research.",
)


# --------------------------------------------------------------------------- auth
@app.middleware("http")
async def token_guard(request: Request, call_next):
    if TOKEN and request.url.path.startswith("/api") and request.url.path != "/api/health":
        supplied = request.headers.get("x-api-token") or request.query_params.get("token", "")
        if supplied != TOKEN:
            return JSONResponse({"detail": "bad or missing API token"}, status_code=401)
    return await call_next(request)


# --------------------------------------------------------------------------- meta
@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "demo": DEMO,
        "scope": sorted(SCOPE.domains),
        "enforce_scope": SCOPE.enforce,
        "library_size": len(LIBRARY),
        "leads": STORE.stats(),
        "auth": bool(TOKEN),
    }


@app.get("/api/library")
def library(category: str | None = None, risk: str | None = None) -> dict:
    entries = [
        {
            "id": entry.id, "category": entry.category, "risk": entry.risk,
            "query": entry.query, "description": entry.description, "use_case": entry.use_case,
        }
        for entry in LIBRARY.entries
        if (not category or entry.category == category) and (not risk or entry.risk == risk)
    ]
    return {"count": len(entries), "categories": LIBRARY.categories,
            "engines": sorted(ENGINES), "entries": entries}


@app.get("/api/scope")
def get_scope() -> dict:
    return {"domains": sorted(SCOPE.domains), "enforce": SCOPE.enforce, "file": str(SCOPE_FILE)}


@app.post("/api/scope")
def add_scope(payload: dict = Body(...)) -> dict:
    domain = normalize_domain(str(payload.get("domain", "")))
    if not domain or "." not in domain:
        raise HTTPException(400, "not a domain")
    SCOPE.domains.add(domain)
    SCOPE.enforce = bool(payload.get("enforce", True))
    SCOPE_FILE.write_text(
        "# Domains you are authorised to research (one per line)\n"
        + "\n".join(sorted(SCOPE.domains)) + "\n",
        encoding="utf-8",
    )
    RUNNER.scope = SCOPE
    return get_scope()


@app.delete("/api/scope")
def remove_scope(domain: str = Query(...)) -> dict:
    SCOPE.domains.discard(normalize_domain(domain))
    SCOPE_FILE.write_text(
        "# Domains you are authorised to research (one per line)\n"
        + "\n".join(sorted(SCOPE.domains)) + "\n",
        encoding="utf-8",
    )
    return get_scope()


# --------------------------------------------------------------------------- parse/forge
@app.post("/api/parse")
def parse_prompt(payload: dict = Body(...)) -> dict:
    target = parse(str(payload.get("prompt", "")), str(payload.get("risk_ceiling", "medium")))
    return target.to_dict()


@app.post("/api/forge")
def forge_prompt(payload: dict = Body(...)) -> dict:
    target = parse(str(payload.get("prompt", "")), str(payload.get("risk_ceiling", "medium")))
    for field in ("domain", "place", "name", "filetype", "category"):
        if payload.get(field):
            setattr(target, field, str(payload[field]).strip())
    if payload.get("keywords"):
        target.keywords = [str(k) for k in payload["keywords"]][:6]
    result = forge(
        target,
        LIBRARY,
        limit=int(payload.get("limit", 24)),
        risk_ceiling=str(payload.get("risk_ceiling", target.risk_ceiling)),
        engine=str(payload.get("engine", "google")),
        category=payload.get("category") or None,
    )
    return {
        "target": target.to_dict(),
        "count": len(result.dorks),
        "skipped": result.skipped,
        "dorks": [dork.to_dict() for dork in result.dorks],
    }


@app.post("/api/variants")
def make_variants(payload: dict = Body(...)) -> dict:
    query = str(payload.get("query", ""))
    engine = str(payload.get("engine", "google"))
    return {"variants": [{"query": v, "engine_url": search_url(v, engine)} for v in variants(query)]}


# --------------------------------------------------------------------------- jobs
@app.post("/api/jobs", status_code=202)
def create_job(payload: dict = Body(...)) -> dict:
    request = RunRequest.from_dict(payload)
    if DEMO:
        request.demo = True
    if not request.prompt and not request.domain and not request.seeds:
        raise HTTPException(400, "give me a prompt, a domain or at least one seed URL")
    if request.demo:
        request.demo = True
    for url in request.seeds:
        if not request.demo and not SCOPE.allows(url):
            raise HTTPException(
                403,
                f"{url} is outside scope.txt — add the domain first (POST /api/scope).",
            )
    job = JOBS.submit(request, SCOPE)
    return job.summary()


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": JOBS.history()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, since: int = 0) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    return {
        **job.summary(),
        "events": [event.to_dict() for event in job.events[since:]],
        "event_count": len(job.events),
        "target": job.target.to_dict() if job.target else None,
        "dorks": [dork.to_dict() for dork in job.forged.dorks] if job.forged else [],
    }


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    job.cancel()
    return {"id": job.id, "status": "cancelling"}


@app.websocket("/ws/jobs/{job_id}")
async def job_stream(websocket: WebSocket, job_id: str, since: int = 0) -> None:
    if TOKEN and websocket.query_params.get("token", "") != TOKEN:
        await websocket.close(code=1008)
        return
    job = JOBS.get(job_id)
    if job is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def listener(event) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    for event in job.events[since:]:
        await websocket.send_json(event.to_dict())
    if job.status in {"done", "error", "cancelled"}:
        await websocket.send_json({"type": "stream_closed", "status": job.status})
        await websocket.close()
        return

    job.subscribe(listener)
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event.to_dict())
            if event.type in {"job_finished", "job_failed"}:
                await websocket.send_json({"type": "stream_closed", "status": job.status})
                break
    except WebSocketDisconnect:
        pass
    finally:
        job.unsubscribe(listener)
        try:
            await websocket.close()
        except RuntimeError:
            pass


# --------------------------------------------------------------------------- leads
@app.get("/api/leads")
def list_leads(min_score: int = 0, q: str = "", limit: int = 500) -> dict:
    leads = STORE.all(min_score=min_score)
    if q:
        needle = q.lower()
        leads = [
            lead for lead in leads
            if any(needle in str(value).lower() for value in lead.to_dict().values()
                   if not isinstance(value, list))
        ]
    return {"count": len(leads), "stats": STORE.stats(), "leads": [l.to_dict() for l in leads[:limit]]}


@app.delete("/api/leads")
def clear_leads() -> dict:
    global STORE
    STORE = LeadStore(OUTPUT_DIR / "leads.json")
    RUNNER.store = STORE
    if (OUTPUT_DIR / "leads.json").exists():
        (OUTPUT_DIR / "leads.json").unlink()
    return {"status": "cleared", "stats": STORE.stats()}


@app.get("/api/leads/export")
def export_leads(fmt: str = "csv", min_score: int = 0) -> StreamingResponse:
    leads = STORE.all(min_score=min_score)
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    if fmt == "jsonl":
        path = export_jsonl(leads, OUTPUT_DIR / f"leads-{stamp}.jsonl")
    elif fmt in {"md", "markdown"}:
        path = export_markdown(leads, OUTPUT_DIR / f"leads-{stamp}.md")
    elif fmt == "json":
        import json as _json

        return JSONResponse([lead.to_dict() for lead in leads])
    else:
        path = export_csv(leads, OUTPUT_DIR / f"leads-{stamp}.csv")
    media = {".jsonl": "application/x-ndjson", ".md": "text/markdown"}.get(path.suffix, "text/csv")
    return StreamingResponse(
        io.BytesIO(path.read_bytes()),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# --------------------------------------------------------------------------- static
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
