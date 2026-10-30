"""Scope gate, SSRF guard and a polite HTTP fetcher.

Rules baked in:
  * every URL must fall inside ``scope.txt`` (unless the gate is disabled)
  * private / loopback / link-local addresses are refused
  * robots.txt is honoured per host
  * one request at a time per host, with a configurable delay
  * responses are cached on disk with a TTL
  * no login-walled content, no CAPTCHA solving, no header forgery
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlparse

from .parser import normalize_domain, registrable_domain

DEFAULT_UA = "DorkForge-LeadAgent/2.0 (+authorised research; contact: local operator)"


class ScopeError(PermissionError):
    """Raised when a URL is outside the declared scope."""


class BlockedAddressError(PermissionError):
    """Raised when a URL resolves to a non-public address."""


@dataclass
class Scope:
    domains: set[str] = field(default_factory=set)
    enforce: bool = True

    @classmethod
    def load(cls, path: Path | str, enforce: bool = True) -> "Scope":
        file = Path(path)
        domains: set[str] = set()
        if file.exists():
            for line in file.read_text(encoding="utf-8").splitlines():
                line = line.split("#")[0].strip()
                if line:
                    domains.add(registrable_domain(line))
        return cls(domains=domains, enforce=enforce)

    def allows(self, url_or_host: str) -> bool:
        if not self.enforce:
            return True
        host = normalize_domain(urlparse(url_or_host).netloc or url_or_host)
        if not host:
            return False
        apex = registrable_domain(host)
        return any(apex == d or host == d or host.endswith("." + d) for d in self.domains)

    def require(self, url: str) -> None:
        if not self.allows(url):
            raise ScopeError(
                f"{urlparse(url).netloc or url} is outside scope.txt — add the domain "
                "you are authorised to research, or use --no-scope-gate deliberately."
            )


def assert_public_host(host: str) -> None:
    """Refuse anything that is not a public unicast address."""
    host = normalize_domain(host)
    if not host:
        raise BlockedAddressError("empty host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:  # unresolvable -> let the HTTP layer decide
        raise BlockedAddressError(f"cannot resolve {host}: {exc}") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise BlockedAddressError(f"{host} resolves to non-public address {address}")


@dataclass
class Page:
    url: str
    status: int
    html: str = ""
    content_type: str = ""
    from_cache: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error and "html" in (self.content_type or "text/html")


class Fetcher(Protocol):
    def fetch(self, url: str) -> Page: ...
    def close(self) -> None: ...


@dataclass
class PoliteFetcher:
    scope: Scope
    cache_dir: Path = Path(".dorkforge-cache")
    cache_ttl: int = 12 * 3600
    delay: float = 1.5
    timeout: float = 15.0
    max_bytes: int = 3_000_000
    user_agent: str = DEFAULT_UA
    respect_robots: bool = True

    _last_hit: dict[str, float] = field(default_factory=dict)
    _robots: dict[str, urllib.robotparser.RobotFileParser] = field(default_factory=dict)
    _client: object | None = None

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent, "Accept": "text/html,*/*;q=0.8"},
            )
        return self._client

    # -- cache ---------------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:32]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, url: str) -> Page | None:
        path = self._cache_path(url)
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > self.cache_ttl:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("url") != url:
            return None
        return Page(url=url, status=payload["status"], html=payload["html"],
                    content_type=payload["content_type"], from_cache=True)

    def _write_cache(self, page: Page) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(page.url).write_text(
                json.dumps({"url": page.url, "status": page.status,
                            "html": page.html, "content_type": page.content_type}),
                encoding="utf-8",
            )
        except OSError:
            pass  # a broken cache must never break a run

    # -- robots / throttling -------------------------------------------------
    def _robots_for(self, scheme: str, host: str) -> urllib.robotparser.RobotFileParser:
        key = f"{scheme}://{host}"
        if key in self._robots:
            return self._robots[key]
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{key}/robots.txt")
        try:
            response = self._http().get(f"{key}/robots.txt")
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            else:
                parser.parse([])  # no robots.txt -> everything allowed
        except Exception:
            parser.parse([])  # network hiccup must not deadlock the crawl
        self._robots[key] = parser
        return parser

    def _wait_turn(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            remaining = self.delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_hit[host] = time.monotonic()

    # -- main ----------------------------------------------------------------
    def fetch(self, url: str) -> Page:
        self.scope.require(url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return Page(url=url, status=0, error=f"unsupported scheme {parsed.scheme!r}")

        cached = self._read_cache(url)
        if cached is not None:
            return cached

        host = parsed.netloc or parsed.path
        if self.respect_robots:
            robots = self._robots_for(parsed.scheme, parsed.netloc)
            if not robots.can_fetch(self.user_agent, url):
                return Page(url=url, status=0, error="disallowed by robots.txt")

        try:
            assert_public_host(host)
        except BlockedAddressError as exc:
            return Page(url=url, status=0, error=str(exc))

        self._wait_turn(parsed.netloc)
        try:
            response = self._http().get(url)
        except Exception as exc:
            return Page(url=url, status=0, error=f"{type(exc).__name__}: {exc}")

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            return Page(url=url, status=response.status_code, content_type=content_type,
                        error=f"skipped non-HTML content: {content_type or 'unknown'}")

        html = response.text[: self.max_bytes]
        page = Page(url=url, status=response.status_code, html=html, content_type=content_type)
        if response.status_code == 200:
            self._write_cache(page)
        return page

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


@dataclass
class FixtureFetcher:
    """Offline fetcher: serves bundled demo pages so the app works with no network."""

    root: Path
    scope: Scope = field(default_factory=lambda: Scope(domains=set(), enforce=False))
    hits: list[str] = field(default_factory=list)
    emitter: Callable[[str, str], None] | None = None

    def fetch(self, url: str) -> Page:
        parsed = urlparse(url)
        rel = (parsed.path or "/").strip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        if "." not in Path(rel).name:
            rel += ".html"
        path = self.root / rel
        if not path.is_file() or self.root not in path.resolve().parents:
            return Page(url=url, status=404, error="not found in demo corpus")
        self.hits.append(url)
        return Page(url=url, status=200, html=path.read_text(encoding="utf-8"),
                    content_type="text/html; charset=utf-8")

    def close(self) -> None:
        return None
