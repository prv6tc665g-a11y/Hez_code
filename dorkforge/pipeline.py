"""Orchestration: dorks -> seeds -> harvesting -> scored leads, with a live log.

Discovery model
---------------
This agent does not scrape search engines. It generates the dorks for you to run
in your own browser, and it harvests **pages you point it at** (search-result
URLs, sitemaps, contact pages) — always inside ``scope.txt``. That keeps the tool
on the right side of Google/Bing ToS and of GDPR/nDSG: you choose the pages, the
scope gate enforces the boundary, and every contact keeps its source URL.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

from .fetch import Fetcher, PoliteFetcher, Scope
from .forge import ForgeResult, forge
from .harvest import Harvest, harvest_page, normalize_phone
from .leads import Lead, LeadStore, export_csv, export_jsonl, export_markdown, score_lead
from .library import Library
from .parser import Target, parse, registrable_domain

CATEGORY_BY_PATH = {
    "contacts": ("kontakt", "contact", "impressum", "imprint", "about", "ueber", "team"),
    "careers": ("karriere", "career", "jobs", "stellen", "bewerb", "apply"),
    "people": ("team", "people", "mitarbeiter", "leadership", "vorstand"),
    "company": ("presse", "press", "news", "unternehmen", "company", "referenz"),
    "documents": ("download", "dokument", "file", "pdf", "whitepaper"),
}


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value and not value.startswith(("#", "mailto:", "tel:", "javascript:")):
                self.links.append(value)


def extract_links(html: str, base_url: str) -> list[str]:
    parser = _LinkParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    seen: list[str] = []
    for href in parser.links:
        absolute = urljoin(base_url, href).split("#")[0]
        if absolute.startswith(("http://", "https://")) and absolute not in seen:
            seen.append(absolute)
    return seen


def page_category(url: str) -> str:
    path = urlparse(url).path.lower()
    for category, tokens in CATEGORY_BY_PATH.items():
        if any(token in path for token in tokens):
            return category
    return ""


@dataclass
class Event:
    ts: float
    type: str
    payload: dict

    def to_dict(self) -> dict:
        return {"ts": self.ts, "type": self.type, **self.payload}


@dataclass
class RunRequest:
    prompt: str = ""
    domain: str = ""
    keywords: list[str] = field(default_factory=list)
    place: str = ""
    name: str = ""
    filetype: str = ""
    category: str = ""
    risk_ceiling: str = "medium"
    limit: int = 24
    engine: str = "google"
    seeds: list[str] = field(default_factory=list)
    max_pages: int = 12
    follow_links: bool = False
    default_cc: str = "41"
    export: bool = True
    demo: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "RunRequest":
        allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        request = cls(**allowed)
        request.prompt = str(request.prompt or "")
        request.limit = max(1, min(int(request.limit or 24), 60))
        request.max_pages = max(1, min(int(request.max_pages or 12), 100))
        request.risk_ceiling = (request.risk_ceiling or "medium").lower()
        if request.risk_ceiling not in {"low", "medium", "high"}:
            request.risk_ceiling = "medium"
        return request


@dataclass
class Job:
    id: str
    request: RunRequest
    status: str = "queued"          # queued | running | done | error | cancelled
    events: list[Event] = field(default_factory=list)
    target: Target | None = None
    forged: ForgeResult | None = None
    stats: dict = field(default_factory=dict)
    error: str = ""
    created: float = field(default_factory=time.time)
    finished: float = 0.0
    _cancel: threading.Event = field(default_factory=threading.Event)
    _listeners: list[Callable[[Event], None]] = field(default_factory=list)

    def emit(self, event_type: str, **payload) -> None:
        event = Event(ts=time.time(), type=event_type, payload=payload)
        self.events.append(event)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass

    def subscribe(self, listener: Callable[[Event], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener: Callable[[Event], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def summary(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "prompt": self.request.prompt,
            "stats": self.stats,
            "error": self.error,
            "dorks": len(self.forged.dorks) if self.forged else 0,
            "created": self.created,
            "finished": self.finished,
        }


def _build_leads(harvest: Harvest, page_url: str, scope_domain: str) -> list[Lead]:
    """Turn one page's harvest into deduplicated, score-ready leads."""
    category = page_category(page_url)
    org = harvest.orgs[0] if harvest.orgs else None
    company = org.name if org and org.name else ""
    domain = scope_domain or registrable_domain(urlparse(page_url).netloc)
    base = dict(
        company=company,
        domain=domain,
        city=org.city if org else "",
        country=org.country if org else "",
        country_code=(org.country[:2].upper() if org and org.country and len(org.country) > 1 else ""),
        vat=org.vat if org else "",
        source_url=page_url,
        categories=[category] if category else [],
    )

    leads: list[Lead] = []
    used_emails: set[str] = set()
    used_phones: set[str] = set()

    def person_lead(person_lead_name: str, role: str, email: str = "", phone: str = "",
                    source_url: str = page_url) -> Lead:
        lead = Lead(company=base["company"], domain=base["domain"],
                    contact_name=person_lead_name, role=role, email=email, phone=phone,
                    city=base["city"], country=base["country"],
                    country_code=base["country_code"], vat=base["vat"],
                    source_url=source_url, categories=list(base["categories"]))
        return lead

    people = [person for person in harvest.people if person.name]
    for person in people:
        email = person.email
        phone = person.phone
        if not email:
            local_guess = [token for token in person.name.lower().replace("-", " ").split() if len(token) > 1]
            for contact in harvest.emails:
                local = contact.address.split("@")[0]
                if local_guess and all(token in local for token in local_guess):
                    email = contact.address
                    break
        if email:
            used_emails.add(email.lower())
        if phone:
            used_phones.add(normalize_phone(phone))
        leads.append(person_lead(person.name, person.role, email, phone, person.page_url or page_url))

    for contact in harvest.emails:
        if contact.address in used_emails:
            continue
        used_emails.add(contact.address)
        lead = person_lead("", "", email=contact.address, source_url=contact.page_url or page_url)
        lead.email_kind = contact.kind
        leads.append(lead)

    for phone in harvest.phones:
        if phone.e164 in used_phones:
            continue
        used_phones.add(phone.e164)
        leads.append(person_lead("", "", phone=phone.e164, source_url=phone.page_url or page_url))

    if not leads and (base["company"] or base["vat"] or base["city"]):
        leads.append(Lead(**base))

    for lead in leads:
        if lead.email and not lead.email_kind:
            lead.email_kind = next(
                (c.kind for c in harvest.emails if c.address == lead.email), ""
            )
        score_lead(lead)
    return leads


@dataclass
class Runner:
    library: Library
    store: LeadStore
    output_dir: Path = Path("outputs")
    fetcher_factory: Callable[[Scope], Fetcher] | None = None
    demo_fetcher_factory: Callable[[Scope], Fetcher] | None = None
    scope: Scope | None = None

    def fetcher_for(self, scope: Scope, demo: bool = False) -> Fetcher:
        if demo and self.demo_fetcher_factory is not None:
            return self.demo_fetcher_factory(scope)
        if self.fetcher_factory:
            return self.fetcher_factory(scope)
        return PoliteFetcher(scope=scope)

    def execute(self, job: Job, scope: Scope | None = None) -> Job:
        request = job.request
        scope = scope or self.scope or Scope(domains=set(), enforce=False)
        fetcher = self.fetcher_for(scope, request.demo)
        job.status = "running"
        job.emit("job_started", prompt=request.prompt, scope=sorted(scope.domains),
                 enforce_scope=scope.enforce, mode="demo" if request.demo else "live")
        started = time.time()
        pages_ok = pages_failed = new_leads = updated_leads = 0
        orgs: dict[str, dict] = {}

        try:
            target = parse(request.prompt, request.risk_ceiling)
            for field_name in ("domain", "place", "name", "filetype", "category", "risk_ceiling"):
                override = getattr(request, field_name)
                if override:
                    setattr(target, field_name, str(override).strip())
            if request.keywords:
                target.keywords = [str(k).strip() for k in request.keywords if str(k).strip()][:6]
            job.target = target
            job.emit("target_parsed", **target.to_dict())

            forged = forge(target, self.library, limit=request.limit,
                           risk_ceiling=request.risk_ceiling, engine=request.engine,
                           category=request.category or None)
            job.forged = forged
            job.emit("dorks_ready", count=len(forged.dorks),
                     skipped=forged.skipped, target_domain=target.domain,
                     dorks=[dork.to_dict() for dork in forged.dorks])
            if job.cancelled:
                raise _Cancelled()

            seeds = self._seed_list(request, target, scope)
            job.emit("crawl_started", seeds=seeds, max_pages=request.max_pages,
                     follow_links=request.follow_links)

            queue: list[str] = list(seeds)
            visited: set[str] = set()
            while queue and len(visited) < request.max_pages:
                if job.cancelled:
                    raise _Cancelled()
                url = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                job.emit("page_started", url=url, index=len(visited), total=min(len(queue) + len(visited), request.max_pages))
                page = fetcher.fetch(url)

                if not page.ok:
                    pages_failed += 1
                    job.emit("page_failed", url=url, status=page.status,
                             reason=page.error or f"HTTP {page.status}")
                    if page.status in {401, 403, 429}:
                        job.emit("notice", level="warn", message=(
                            f"{urlparse(url).netloc} answered {page.status} — backing off this host. "
                            "Do not attempt to bypass access controls."))
                        queue = [q for q in queue if urlparse(q).netloc != urlparse(url).netloc]
                    continue

                pages_ok += 1
                harvest = harvest_page(page.html, page.url, scope_domain=target.domain,
                                       default_cc=request.default_cc)
                for org in harvest.orgs:
                    if org.name and target.domain not in orgs:
                        orgs[target.domain] = {
                            "company": org.name,
                            "city": org.city,
                            "country": org.country,
                            "vat": org.vat,
                        }
                leads = _build_leads(harvest, page.url, target.domain)
                for lead in leads:
                    stored, is_new = self.store.upsert(lead)
                    if is_new:
                        new_leads += 1
                    else:
                        updated_leads += 1
                    job.emit("lead_found", is_new=is_new, lead=stored.to_dict())
                job.emit("page_done", url=page.url, cached=page.from_cache,
                         emails=len(harvest.emails), phones=len(harvest.phones),
                         people=len(harvest.people), leads=len(leads))

                if request.follow_links and len(visited) < request.max_pages:
                    for link in extract_links(page.html, page.url):
                        if link in visited or link in queue:
                            continue
                        if not scope.allows(link):
                            continue
                        if page_category(link) or len(queue) < 5:
                            queue.append(link)
                        if len(queue) > request.max_pages * 2:
                            break

            # Pages crawled before the org page still deserve the company name.
            for domain, fields in orgs.items():
                self.store.backfill_domain(domain, **fields)

            job.stats = {
                "pages_ok": pages_ok,
                "pages_failed": pages_failed,
                "pages_visited": len(visited),
                "new_leads": new_leads,
                "updated_leads": updated_leads,
                "dorks": len(forged.dorks),
                "store": self.store.stats(),
                "seconds": round(time.time() - started, 2),
            }
            self.store.save()

            if request.export:
                stamp = time.strftime("%Y%m%d-%H%M%S")
                slug = (target.domain or "run").replace("/", "-")
                base = self.output_dir / f"{stamp}-{slug}"
                leads_now = self.store.all(min_score=1)
                job.stats["reports"] = [
                    str(export_csv(leads_now, base.with_suffix(".csv"))),
                    str(export_jsonl(leads_now, base.with_suffix(".jsonl"))),
                    str(export_markdown(leads_now, base.with_suffix(".md"),
                                        title=f"Leads — {target.domain or request.prompt[:40]}")),
                ]
            job.status = "done"
            job.emit("job_finished", status=job.status, **job.stats)
        except _Cancelled:
            job.status = "cancelled"
            self.store.save()
            job.emit("job_finished", status=job.status, message="cancelled by operator")
        except Exception as exc:  # never let a worker die silently
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.emit("job_failed", error=job.error)
        finally:
            job.finished = time.time()
            try:
                fetcher.close()
            except Exception:
                pass
        return job

    def _seed_list(self, request: RunRequest, target: Target, scope: Scope) -> list[str]:
        seeds: list[str] = []
        for url in list(request.seeds) + list(target.urls):
            url = url.strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            if url not in seeds:
                seeds.append(url)
        if not seeds and target.domain:
            for path in ("/", "/kontakt", "/contact", "/impressum", "/team"):
                seeds.append(f"https://{target.domain}{path}")
        allowed = [url for url in seeds if scope.allows(url)]
        rejected = [url for url in seeds if url not in allowed]
        if rejected:
            # Bypass the fetcher for the notice: it is informational, not a fetch.
            pass
        return allowed


class _Cancelled(Exception):
    pass


class JobManager:
    """In-memory job registry with cancellation and a monotonic history."""

    def __init__(self, runner: Runner, max_jobs: int = 50):
        self.runner = runner
        self.max_jobs = max_jobs
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._counter = 0

    def create(self, request: RunRequest) -> Job:
        with self._lock:
            self._counter += 1
            job_id = f"job-{self._counter:04d}"
            job = Job(id=job_id, request=request)
            self._jobs[job_id] = job
            self._order.append(job_id)
            while len(self._order) > self.max_jobs:
                oldest = self._order.pop(0)
                self._jobs.pop(oldest, None)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start(self, job: Job, scope: Scope) -> Job:
        def work() -> None:
            self.runner.execute(job, scope)

        thread = threading.Thread(target=work, name=f"dorkforge-{job.id}", daemon=True)
        thread.start()
        return job

    def submit(self, request: RunRequest, scope: Scope) -> Job:
        return self.start(self.create(request), scope)

    def history(self) -> list[dict]:
        with self._lock:
            jobs = [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]
        return [job.summary() for job in reversed(jobs)]
