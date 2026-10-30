"""Plain-English prompt -> structured targeting model.

The parser is deliberately deterministic: no LLM needed, same input gives the
same plan, and every extracted value is visible in the UI for overriding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Filler that should never end up in a dork's free-text slot.
STOPWORDS = {
    "find", "show", "me", "all", "get", "list", "search", "please", "want",
    "looking", "for", "the", "and", "with", "from", "that", "have", "has",
    "any", "some", "about", "into", "over", "their", "there", "this", "these",
    "leaked", "leak", "exposed", "expose", "open", "public", "profiles",
    "profile", "pages", "page", "site", "sites", "emails", "email", "contact",
    "contacts", "phone", "numbers", "document", "documents", "file", "files",
    "please", "would", "like", "need", "give", "gimme", "everything", "alle",
    "und", "der", "die", "das", "mir", "bitte", "nach", "von", "mit", "auf",
    "ich", "suche", "finde", "zeige", "kontakte", "kontakt", "firmen", "firma",
    "ansprechpartner", "daten", "lead", "leads", "generieren", "generiere",
    "sammle", "sammeln", "extrahiere", "extract", "harvest", "scrape",
}

# Multi-word hints score double, single words once.
CATEGORY_HINTS: dict[str, list[str]] = {
    "contacts": ["contact", "kontakt", "impressum", "imprint", "email", "e-mail",
                 "phone", "telefon", "reach", "ansprechpartner", "adresse"],
    "people": ["ceo", "founder", "geschaeftsfuehrer", "head of", "leiter",
               "director", "team", "mitarbeiter", "employees", "decision maker",
               "linkedin", "profile", "organigramm"],
    "company": ["handelsregister", "uid", "commercial register", "firmenprofil",
                "company profile", "annual report", "geschaeftsbericht",
                "partner", "reseller", "customer", "kunde", "referenz"],
    "careers": ["career", "karriere", "jobs", "stellen", "hiring", "apply",
                "bewerbung", "hr", "recruiting"],
    "documents": ["pdf", "document", "dokument", "brochure", "whitepaper",
                  "pricing", "preisliste", "spreadsheet", "xlsx", "csv", "catalog"],
    "directories": ["directory", "verzeichnis", "listing", "mitglieder", "members",
                    "chamber of commerce", "handelskammer", "supplier", "lieferant"],
    "events": ["event", "veranstaltung", "webinar", "messe", "trade fair",
               "conference", "konferenz", "registration", "anmeldung"],
    "social": ["linkedin", "xing", "twitter", "github", "instagram", "social"],
    "infrastructure": ["subdomain", "staging", "api", "swagger", "sitemap",
                       "robots", "docs", "index of"],
    "exposure": ["leak", "leaked", "exposed", "confidential", "vertraulich",
                 "password", "passwort", "backup", "env", "sql dump", "secret"],
}

FILE_TYPES = {
    "pdf", "xlsx", "xls", "csv", "docx", "doc", "pptx", "ppt", "txt", "json",
    "sql", "env", "log", "bak", "zip", "xml", "yaml", "yml", "md",
}

# Anything after the TLD that looks like a path/port is stripped.
_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b", re.IGNORECASE
)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s/.-]?)?(?:\(?\d{2,4}\)?[\s/.-]?){2,5}\d{2,6}")
_QUOTED_RE = re.compile(r"\"([^\"]{2,60})\"|'([^']{2,60})'")

# Second-level labels that are part of a public suffix, not the brand.
_SECOND_LEVEL = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "co.nz",
    "com.br", "co.jp", "co.za", "com.tr", "co.in", "com.cn", "com.mx",
}


@dataclass
class Target:
    """Structured version of the user's sentence."""

    prompt: str = ""
    domain: str = ""
    urls: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    filetype: str = ""
    category: str = ""
    risk_ceiling: str = "medium"

    def slots(self) -> dict[str, str]:
        return {
            "domain": self.domain,
            "kw": self.keywords[0] if self.keywords else "",
            "ext": self.filetype,
            "place": self.places[0] if self.places else "",
            "name": self.names[0] if self.names else "",
        }

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "domain": self.domain,
            "urls": self.urls,
            "emails": self.emails,
            "phones": self.phones,
            "names": self.names,
            "places": self.places,
            "keywords": self.keywords,
            "filetype": self.filetype,
            "category": self.category,
            "risk_ceiling": self.risk_ceiling,
        }


def normalize_domain(value: str) -> str:
    """``https://www.Acme.ch:443/path`` -> ``acme.ch``."""
    value = value.strip().strip(".,;:()[]<>\"'")
    value = re.sub(r"^[a-z]+://", "", value, flags=re.IGNORECASE)
    value = value.split("/")[0].split("?")[0].split("#")[0]
    value = value.split("@")[-1]           # drop userinfo
    value = value.split(":")[0]            # drop port
    value = value.strip(".").lower()
    if value.startswith("www."):
        value = value[4:]
    return value


def registrable_domain(host: str) -> str:
    """Best-effort eTLD+1 without a public-suffix dependency."""
    host = normalize_domain(host)
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _score_categories(text: str) -> str:
    lowered = text.lower()
    best, best_score = "", 0
    for category, hints in CATEGORY_HINTS.items():
        score = 0
        for hint in hints:
            if hint in lowered:
                score += 2 if " " in hint else 1
        if score > best_score:
            best, best_score = category, score
    return best


def _extract_places(text: str, known_names: set[str]) -> list[str]:
    places: list[str] = []
    for match in re.finditer(
        r"\b(?:in|at|near|located in|based in|aus|bei)\s+([A-ZÄÖÜ][\wäöüß-]{2,30}(?:\s+[A-ZÄÖÜ][\wäöüß-]{2,30})?)",
        text,
    ):
        candidate = match.group(1).strip()
        if candidate.lower() not in STOPWORDS and candidate not in known_names:
            places.append(candidate)
    return places


def _keywords(text: str, taken: set[str]) -> list[str]:
    """Quoted phrases first, then longest meaningful words."""
    phrases = [a or b for a, b in _QUOTED_RE.findall(text)]
    words = re.findall(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9-]{2,}", text)
    pool: list[str] = []
    for candidate in phrases + words:
        lowered = candidate.lower()
        if lowered in STOPWORDS or lowered in taken or len(lowered) < 3:
            continue
        if lowered in FILE_TYPES:
            continue
        if candidate.isdigit():
            continue
        pool.append(candidate)
    # Deduplicate case-insensitively, prefer the longest spelling.
    seen: dict[str, str] = {}
    for candidate in pool:
        key = candidate.lower()
        if key not in seen or len(candidate) > len(seen[key]):
            seen[key] = candidate
    return sorted(seen.values(), key=lambda w: (-len(w), w))[:6]


def parse(prompt: str, risk_ceiling: str = "medium") -> Target:
    target = Target(prompt=prompt, risk_ceiling=risk_ceiling)

    for url in _URL_RE.findall(prompt):
        cleaned = url.rstrip(".,;)]}\"'")
        if cleaned not in target.urls:
            target.urls.append(cleaned)

    emails = [e.lower() for e in _EMAIL_RE.findall(prompt)]
    target.emails = list(dict.fromkeys(emails))

    domains: list[str] = []
    for url in target.urls:
        candidate = normalize_domain(url)
        if candidate and candidate not in domains:
            domains.append(candidate)
    domain_scan_text = _EMAIL_RE.sub(" ", _URL_RE.sub(" ", prompt))
    for match in _DOMAIN_RE.findall(domain_scan_text):
        candidate = normalize_domain(match)
        if not candidate or candidate in domains:
            continue
        domains.append(candidate)
    if not domains:
        for email in target.emails:
            local, _, host = email.partition("@")
            if host and local not in {"info", "contact", "mail", "noreply"}:
                domains.append(normalize_domain(host))
                break
    if domains:
        target.domain = registrable_domain(domains[0])

    for match in _PHONE_RE.findall(prompt):
        digits = re.sub(r"\D", "", match)
        if 9 <= len(digits) <= 15:
            pretty = match.strip()
            if pretty not in target.phones:
                target.phones.append(pretty)
    # Long digit runs are usually reference numbers, not phones.
    target.phones = [p for p in target.phones if len(re.sub(r"\D", "", p)) >= 9][:4]

    for match in re.finditer(r"\b([A-ZÄÖÜ][a-zäöüß]{2,20})\s+([A-ZÄÖÜ][a-zäöüß]{2,24})\b", prompt):
        first, last = match.group(1), match.group(2)
        if first.lower() in STOPWORDS or last.lower() in STOPWORDS:
            continue
        name = f"{first} {last}"
        if name not in target.names:
            target.names.append(name)

    taken = {p.lower() for p in target.places}
    taken |= {t.lower() for t in target.names}
    taken |= {target.domain} if target.domain else set()
    taken |= {p.lower() for p in target.phones}
    taken |= {e.split("@")[0].lower() for e in target.emails}

    target.places = _extract_places(prompt, set(target.names))

    for ext in FILE_TYPES:
        if re.search(rf"\b{ext}\b", prompt, re.IGNORECASE):
            target.filetype = ext
            break

    target.keywords = _keywords(prompt, taken)
    target.category = _score_categories(prompt)
    return target
