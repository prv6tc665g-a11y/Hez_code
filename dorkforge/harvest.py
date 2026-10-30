"""The harvesting layer: page HTML -> contacts.

Extraction order matters. Structured data (JSON-LD, microdata) is trusted most,
then explicit ``mailto:`` / ``tel:`` links, then plain-text patterns. Every
contact keeps the URL it came from, so a lead is always traceable to its source.
"""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

ROLE_PREFIXES = {
    "info", "contact", "kontakt", "hello", "hallo", "sales", "vertrieb", "mail",
    "office", "buero", "support", "help", "service", "admin", "team", "jobs",
    "career", "karriere", "hr", "recruiting", "bewerbung", "press", "presse",
    "media", "marketing", "billing", "rechnung", "invoice", "orders", "bestellung",
    "enquiries", "inquiry", "anfrage", "anfragen", "reception", "empfang",
    "datenschutz", "privacy", "legal", "newsletter", "noreply", "no-reply",
    "webmaster", "postmaster", "abuse", "security",
}

FREEMAIL = {
    "gmail.com", "googlemail.com", "bluewin.ch", "gmx.ch", "gmx.de", "gmx.net",
    "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com",
    "sunrise.ch", "hispeed.ch", "web.de", "t-online.de", "me.com", "proton.me",
    "protonmail.com", "aol.com", "msn.com",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
_MAILTO_RE = re.compile(r"""mailto:([^"'?>\s]+)""", re.IGNORECASE)
_TEL_RE = re.compile(r"""tel:([+0-9()\-.\s/]{6,25})""", re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s./-]?)?(?:\(\d{2,4}\)[\s./-]?|\d{2,4}[\s./-])"
    r"(?:\d{2,4}[\s./-]?){1,4}\d{2,4}"
)
_JSONLD_RE = re.compile(
    r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
_ITEMPROP_RE = re.compile(
    r"""itemprop=["'](email|telephone|name)["'][^>]*content=["']([^"']+)["']""", re.IGNORECASE
)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)

# Things that look like phone numbers but are identifiers.
_NOT_A_PHONE = [
    re.compile(r"^\d{3}\.\d{3}\.\d{3}$"),          # Swiss UID: 123.456.789
    re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$"),       # IPv4
    re.compile(r"^(?:\d{2}[.-]?){4,}\d{3,}$"),        # IBAN-ish / long account runs
]


def looks_like_identifier(raw: str) -> bool:
    stripped = raw.strip().strip("() ")
    return any(pattern.match(stripped) for pattern in _NOT_A_PHONE)

_OBFUSCATION = [
    (re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.I), "@"),
    (re.compile(r"\s*[\[\(\{]\s*(?:dot|punkt)\s*[\]\)\}]\s*", re.I), "."),
    (re.compile(r"\s+at\s+([A-Za-z0-9-]+)\s*(?:\.|dot|punkt)\s*", re.I), r"@\1."),
]


@dataclass
class Email:
    address: str
    kind: str            # role | personal
    source: str          # jsonld | mailto | itemprop | text
    page_url: str
    in_scope_domain: bool = True
    freemail: bool = False

    def to_dict(self) -> dict:
        return {
            "address": self.address, "kind": self.kind, "source": self.source,
            "page_url": self.page_url, "in_scope_domain": self.in_scope_domain,
            "freemail": self.freemail,
        }


@dataclass
class Phone:
    raw: str
    e164: str
    source: str
    page_url: str

    def to_dict(self) -> dict:
        return {"raw": self.raw, "e164": self.e164, "source": self.source,
                "page_url": self.page_url}


@dataclass
class Person:
    name: str
    role: str = ""
    email: str = ""
    phone: str = ""
    page_url: str = ""
    source: str = "jsonld"

    def to_dict(self) -> dict:
        return {"name": self.name, "role": self.role, "email": self.email,
                "phone": self.phone, "page_url": self.page_url, "source": self.source}


@dataclass
class Org:
    name: str = ""
    legal_form: str = ""
    vat: str = ""
    street: str = ""
    city: str = ""
    postal: str = ""
    country: str = ""
    page_url: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Harvest:
    emails: list[Email] = field(default_factory=list)
    phones: list[Phone] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    orgs: list[Org] = field(default_factory=list)

    def merge(self, other: "Harvest") -> None:
        self.emails.extend(other.emails)
        self.phones.extend(other.phones)
        self.people.extend(other.people)
        self.orgs.extend(other.orgs)

    @property
    def empty(self) -> bool:
        return not (self.emails or self.phones or self.people or self.orgs)

    def to_dict(self) -> dict:
        return {
            "emails": [e.to_dict() for e in self.emails],
            "phones": [p.to_dict() for p in self.phones],
            "people": [p.to_dict() for p in self.people],
            "orgs": [o.to_dict() for o in self.orgs],
        }


def deobfuscate(text: str) -> str:
    for pattern, replacement in _OBFUSCATION:
        text = pattern.sub(replacement, text)
    return text


def strip_tags(html: str) -> str:
    text = _TAG_RE.sub(" ", html)
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text)


def normalize_phone(raw: str, default_cc: str = "41") -> str:
    """Best-effort E.164. ``044 123 45 67`` -> ``+41441234567``."""
    digits = re.sub(r"[^\d+]", "", raw)
    if not digits:
        return ""
    if digits.startswith("+"):
        return "+" + digits[1:]
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+" + default_cc + digits[1:]
    if 9 <= len(digits) <= 15:
        return "+" + digits
    return ""


def classify_email(address: str, scope_domain: str) -> Email:
    local, _, host = address.lower().partition("@")
    role = local.split("+")[0] in ROLE_PREFIXES or any(
        local.startswith(p) for p in ("info", "contact", "sales", "jobs", "hr")
    )
    in_scope = bool(scope_domain) and (
        host == scope_domain or host.endswith("." + scope_domain)
        or host == "www." + scope_domain
    )
    return Email(address=address.lower(), kind="role" if role else "personal",
                 source="text", page_url="", in_scope_domain=in_scope,
                 freemail=host in FREEMAIL)


def _valid_email(address: str) -> bool:
    if len(address) > 254 or address.count("@") != 1:
        return False
    local, _, host = address.partition("@")
    if not local or not host or ".." in address:
        return False
    if re.search(r"\.(png|jpe?g|gif|svg|webp|css|js|woff2?|ttf|ico)$", host, re.I):
        return False
    if len(host.rsplit(".", 1)[-1]) < 2:
        return False
    return True


def _walk_jsonld(node, sink: dict) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, sink)
        return
    if not isinstance(node, dict):
        return
    if "@graph" in node:
        _walk_jsonld(node["@graph"], sink)
    node_type = node.get("@type") or ""
    types = node_type if isinstance(node_type, list) else [node_type]
    types = [t.lower() for t in types]

    email = node.get("email")
    phone = node.get("telephone") or node.get("phone")
    name = node.get("name")

    if any(t in {"person"} for t in types):
        role = node.get("jobTitle") or node.get("title") or ""
        person = Person(name=str(name or "").strip(), role=str(role).strip(),
                        email=str(email or "").strip(),
                        phone=str(phone or "").strip(), source="jsonld")
        if person.name:
            sink.setdefault("people", []).append(person)
    elif any(t in {"organization", "localbusiness", "corporation", "ngo",
                   "store", "professionalservice", "restaurant"} for t in types):
        address = node.get("address") or {}
        if not isinstance(address, dict):
            address = {"streetAddress": str(address)}
        org = Org(
            name=str(name or "").strip(),
            legal_form=str(node.get("legalName") or "").strip(),
            vat=str(node.get("vatID") or node.get("taxID") or "").strip(),
            street=str(address.get("streetAddress") or "").strip(),
            city=str(address.get("addressLocality") or "").strip(),
            postal=str(address.get("postalCode") or "").strip(),
            country=str(address.get("addressCountry") or "").strip(),
        )
        if org.name or org.city:
            sink.setdefault("orgs", []).append(org)

    for key in ("email", "telephone"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            kind = "email" if key == "email" else "phone"
            sink.setdefault(kind, []).append(value.strip().removeprefix("mailto:"))

    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk_jsonld(value, sink)


def harvest_page(html: str, page_url: str, scope_domain: str = "",
                 default_cc: str = "41") -> Harvest:
    result = Harvest()
    if not html:
        return result

    decoded = html_module.unescape(html)
    text = deobfuscate(strip_tags(decoded))

    # 1. structured data
    structured: dict = {}
    for blob in _JSONLD_RE.findall(decoded):
        try:
            _walk_jsonld(json.loads(blob.strip()), structured)
        except (json.JSONDecodeError, TypeError):
            continue
    for person in structured.get("people", []):
        person.page_url = page_url
        result.people.append(person)
    for org in structured.get("orgs", []):
        org.page_url = page_url
        result.orgs.append(org)

    seen_emails: dict[str, Email] = {}
    seen_phones: set[str] = set()

    def add_email(address: str, source: str) -> None:
        address = unquote(address).strip().strip(".,;:<>()[]").lower()
        if not address or not _valid_email(address):
            return
        existing = seen_emails.get(address)
        if existing:
            rank = {"jsonld": 0, "itemprop": 1, "mailto": 2, "text": 3}
            if rank.get(source, 9) < rank.get(existing.source, 9):
                existing.source = source
            return
        contact = classify_email(address, scope_domain)
        contact.source = source
        contact.page_url = page_url
        seen_emails[address] = contact

    def add_phone(raw: str, source: str) -> None:
        if looks_like_identifier(raw):
            return
        e164 = normalize_phone(raw, default_cc)
        if not e164 or not (9 <= len(e164) <= 16):
            return
        if e164 in seen_phones:
            return
        seen_phones.add(e164)
        result.phones.append(Phone(raw=raw.strip(), e164=e164, source=source,
                                   page_url=page_url))

    for value in structured.get("email", []):
        add_email(value.replace("mailto:", ""), "jsonld")
    for value in structured.get("phone", []):
        add_phone(value.replace("tel:", ""), "jsonld")

    # 2. explicit links and microdata
    for value in _MAILTO_RE.findall(decoded):
        add_email(value, "mailto")
    for value in _TEL_RE.findall(decoded):
        add_phone(value, "tel-link")
    for prop, value in _ITEMPROP_RE.findall(decoded):
        if prop.lower() == "email":
            add_email(value, "itemprop")
        elif prop.lower() == "telephone":
            add_phone(value, "itemprop")

    # 3. plain text
    for value in _EMAIL_RE.findall(text):
        add_email(value, "text")

    for value in _PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", value)
        if 9 <= len(digits) <= 15:
            add_phone(value, "text")

    result.emails = list(seen_emails.values())
    return result
