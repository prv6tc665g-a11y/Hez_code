"""Lead model, scoring, dedupe, persistence and export."""

from __future__ import annotations

import csv
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from .parser import registrable_domain

DECISION_ROLES = {
    "ceo", "cto", "cfo", "coo", "ciso", "founder", "co-founder", "owner",
    "inhaber", "geschaeftsfuehrer", "geschäftsführer", "managing director",
    "director", "head of", "leiter", "leiterin", "partner", "president",
    "vorstand", "prokurist", "head", "vp", "chief",
}

CSV_COLUMNS = [
    "id", "company", "domain", "contact_name", "role", "email", "email_kind",
    "phone", "city", "country", "country_code", "vat", "source_url",
    "categories", "score", "score_reasons", "first_seen",
]


@dataclass
class Lead:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    company: str = ""
    domain: str = ""
    contact_name: str = ""
    role: str = ""
    email: str = ""
    email_kind: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""
    country_code: str = ""
    vat: str = ""
    source_url: str = ""
    categories: list[str] = field(default_factory=list)
    score: int = 0
    score_reasons: list[str] = field(default_factory=list)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = 0.0

    # -- identity ----------------------------------------------------------
    @property
    def key(self) -> str:
        if self.email:
            return f"email:{self.email.lower()}"
        if self.phone:
            return f"phone:{self.phone}"
        if self.contact_name:
            return f"person:{self.domain}:{self.contact_name.lower()}"
        return f"source:{self.source_url}"

    def merge(self, other: "Lead") -> None:
        """Keep the richest value for every field."""
        for name in ("company", "contact_name", "role", "email_kind", "city",
                     "country", "country_code", "vat", "phone", "email"):
            if not getattr(self, name) and getattr(other, name):
                setattr(self, name, getattr(other, name))
        if not self.domain and other.domain:
            self.domain = other.domain
        for category in other.categories:
            if category not in self.categories:
                self.categories.append(category)
        self.last_seen = max(self.last_seen, other.last_seen)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_row(self) -> dict:
        data = self.to_dict()
        data["categories"] = ";".join(self.categories)
        data["score_reasons"] = ";".join(self.score_reasons)
        return {column: data.get(column, "") for column in CSV_COLUMNS}


def score_lead(lead: Lead) -> Lead:
    """Transparent 0-100 fit score — every point is explained."""
    reasons: list[str] = []
    score = 0

    if lead.email:
        score += 30
        reasons.append("email +30")
        if lead.email_kind == "personal":
            score += 10
            reasons.append("personal address +10")
        else:
            reasons.append("role address +0")
    if lead.phone:
        score += 15
        reasons.append("phone +15")
    if lead.contact_name:
        score += 10
        reasons.append("named contact +10")
    if lead.role:
        lowered = lead.role.lower()
        if any(token in lowered for token in DECISION_ROLES):
            score += 15
            reasons.append("decision maker +15")
        else:
            score += 5
            reasons.append("role known +5")
    if lead.vat or lead.country_code:
        score += 5
        reasons.append("verified entity +5")
    if lead.city:
        score += 3
        reasons.append("location +3")
    if lead.categories and "contacts" in lead.categories:
        score += 5
        reasons.append("contact page +5")
    if lead.contact_name and lead.email and lead.domain:
        local = lead.email.split("@")[0]
        parts = [p for p in lead.contact_name.lower().replace("-", " ").split() if len(p) > 1]
        if parts and all(p in local for p in parts):
            score += 10
            reasons.append("email matches name +10")

    lead.score = min(score, 100)
    lead.score_reasons = reasons
    return lead


class LeadStore:
    """Deduping store. Thread-safe, optionally persisted to disk."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self._leads: dict[str, Lead] = {}
        self._lock = threading.Lock()
        if self.path and self.path.exists():
            self.load()

    def upsert(self, lead: Lead) -> tuple[Lead, bool]:
        """Return (stored lead, created). ``created`` is True only for a new record."""
        with self._lock:
            existing = self._leads.get(lead.key)
            if existing is None:
                lead.last_seen = lead.last_seen or time.time()
                self._leads[lead.key] = score_lead(lead)
                return self._leads[lead.key], True
            existing.merge(lead)
            score_lead(existing)
            return existing, False

    def backfill_domain(self, domain: str, **fields) -> int:
        """Fill empty company/city/vat fields for every lead of one domain."""
        if not domain:
            return 0
        touched = 0
        with self._lock:
            for lead in self._leads.values():
                if lead.domain != domain:
                    continue
                changed = False
                for name, value in fields.items():
                    if value and not getattr(lead, name, ""):
                        setattr(lead, name, value)
                        changed = True
                if changed:
                    if not lead.country_code and lead.country:
                        lead.country_code = lead.country[:2].upper()
                    score_lead(lead)
                    touched += 1
        return touched

    def all(self, *, min_score: int = 0, sort: bool = True) -> list[Lead]:
        with self._lock:
            leads = [lead for lead in self._leads.values() if lead.score >= min_score]
        if sort:
            leads.sort(key=lambda item: (-item.score, item.company or item.email))
        return leads

    def stats(self) -> dict:
        with self._lock:
            leads = list(self._leads.values())
        return {
            "total": len(leads),
            "with_email": sum(1 for lead in leads if lead.email),
            "with_phone": sum(1 for lead in leads if lead.phone),
            "named": sum(1 for lead in leads if lead.contact_name),
            "avg_score": round(sum(lead.score for lead in leads) / len(leads), 1) if leads else 0,
            "domains": len({lead.domain for lead in leads if lead.domain}),
        }

    # -- persistence -------------------------------------------------------
    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = [lead.to_dict() for lead in self._leads.values()]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)  # atomic: no half-written lead store

    def load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        with self._lock:
            for item in payload:
                try:
                    self._leads[Lead(**{k: v for k, v in item.items()
                                       if k in Lead.__dataclass_fields__}).key] = Lead(**item)
                except TypeError:
                    continue


def export_csv(leads: Iterable[Lead], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead.to_row())
    return path


def export_jsonl(leads: Iterable[Lead], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for lead in leads:
            handle.write(json.dumps(lead.to_dict(), ensure_ascii=False) + "\n")
    return path


def export_markdown(leads: Iterable[Lead], path: Path, title: str = "Lead report") -> Path:
    leads = list(leads)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", "", f"_{len(leads)} leads — generated {time.strftime('%Y-%m-%d %H:%M')}_", ""]
    lines.append("| Score | Company | Contact | Role | Email | Phone | Source |")
    lines.append("|---|---|---|---|---|---|---|")
    for lead in leads:
        lines.append(
            f"| {lead.score} | {lead.company or lead.domain} | {lead.contact_name} | "
            f"{lead.role} | {lead.email} | {lead.phone} | [{urlparse(lead.source_url).netloc}]({lead.source_url}) |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def group_by_company(leads: Iterable[Lead]) -> dict[str, list[Lead]]:
    grouped: dict[str, list[Lead]] = {}
    for lead in leads:
        key = lead.domain or registrable_domain(urlparse(lead.source_url).netloc) or "unknown"
        grouped.setdefault(key, []).append(lead)
    return grouped
