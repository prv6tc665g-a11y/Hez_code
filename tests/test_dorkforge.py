"""Test suite for the DorkForge lead agent (offline, no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dorkforge.fetch import BlockedAddressError, FixtureFetcher, Scope, ScopeError, assert_public_host
from dorkforge.forge import forge, search_url, variants
from dorkforge.harvest import (classify_email, deobfuscate, harvest_page,
                               looks_like_identifier, normalize_phone)
from dorkforge.leads import Lead, LeadStore, export_csv, score_lead
from dorkforge.library import Library
from dorkforge.parser import normalize_domain, parse, registrable_domain
from dorkforge.pipeline import JobManager, RunRequest, Runner, extract_links

DEMO = Path(__file__).parent.parent / "dorkforge" / "demo_site"


@pytest.fixture(scope="module")
def library() -> Library:
    return Library.load()


# ------------------------------------------------------------------ library
def test_library_loads_and_validates(library: Library) -> None:
    assert len(library) >= 50
    assert "contacts" in library.categories
    assert all(entry.risk in {"low", "medium", "high"} for entry in library.entries)


def test_optional_segments_disappear(library: Library) -> None:
    recipe = next(r for r in library.entries if r.id == 2)
    assert recipe.render({"domain": "acme.ch", "kw": ""}) == "site:acme.ch inurl:kontakt"
    filled = recipe.render({"domain": "acme.ch", "kw": "einkauf"})
    assert filled == "site:acme.ch inurl:kontakt einkauf"
    for entry in library.entries:
        rendered = entry.render({"domain": "acme.ch", "kw": "einkauf", "ext": "pdf",
                                 "place": "Aarau", "name": "Marta Brunner"})
        assert rendered is None or ("[[" not in rendered and "]]" not in rendered), entry.id


def test_mandatory_slot_missing_returns_none(library: Library) -> None:
    recipe = next(r for r in library.entries if r.id == 46)
    assert recipe.render({"domain": ""}) is None


def test_no_placeholder_leaks(library: Library) -> None:
    target = parse("kontakte von acme.ch")
    result = forge(target, library, limit=60)
    assert result.dorks
    for dork in result.dorks:
        assert "{" not in dork.query and "}" not in dork.query


def test_risk_ceiling_filters(library: Library) -> None:
    target = parse("exposed env files on acme.ch")
    low = forge(target, library, limit=60, risk_ceiling="low")
    assert all(dork.risk == "low" for dork in low.dorks)
    high = forge(target, library, limit=60, risk_ceiling="high")
    assert any(dork.risk == "high" for dork in high.dorks)


# ------------------------------------------------------------------- parser
@pytest.mark.parametrize("raw,expected", [
    ("https://www.Acme.ch:443/path?x=1", "acme.ch"),
    ("acme.ch", "acme.ch"),
    ("WWW.FOO.CO.UK/bar", "foo.co.uk"),
    ("http://user:pw@mail.acme.ch/", "mail.acme.ch"),
])
def test_normalize_domain(raw: str, expected: str) -> None:
    assert normalize_domain(raw) == expected


def test_registrable_domain() -> None:
    assert registrable_domain("mail.acme.ch") == "acme.ch"
    assert registrable_domain("shop.foo.co.uk") == "foo.co.uk"


def test_parse_german_prompt() -> None:
    target = parse('Finde Kontakte von "Medizintechnik" bei Acme Demo AG, https://acme-demo.ch/kontakt')
    assert target.domain == "acme-demo.ch"
    assert "Medizintechnik" in target.keywords
    assert "kontakt" not in [k.lower() for k in target.keywords]


def test_parse_email_only() -> None:
    target = parse("leads for marta.brunner@acme-demo.ch")
    assert target.domain == "acme-demo.ch"
    assert target.emails == ["marta.brunner@acme-demo.ch"]


def test_parse_filetype_and_risk() -> None:
    target = parse("pdf Preislisten von acme.ch")
    assert target.filetype == "pdf"
    assert target.category in {"documents", "contacts", "company"}


def test_parse_is_deterministic() -> None:
    prompt = "Ansprechpartner im Einkauf bei Acme Demo AG in Aarau"
    assert parse(prompt).to_dict() == parse(prompt).to_dict()


def test_parse_empty_prompt() -> None:
    target = parse("")
    assert target.domain == "" and target.keywords == []


# ------------------------------------------------------------------ harvest
def test_normalize_phone() -> None:
    assert normalize_phone("+41 44 123 45 67") == "+41441234567"
    assert normalize_phone("044 123 45 67") == "+41441234567"
    assert normalize_phone("0041 44 123 45 67") == "+41441234567"
    assert normalize_phone("12") == ""


def test_deobfuscate() -> None:
    assert "info@acme.ch" in deobfuscate("info [at] acme [dot] ch")


def test_classify_role_vs_personal() -> None:
    assert classify_email("info@acme.ch", "acme.ch").kind == "role"
    assert classify_email("marta.brunner@acme.ch", "acme.ch").kind == "personal"
    assert classify_email("someone@gmail.com", "acme.ch").freemail is True


def test_harvest_jsonld_and_obfuscated() -> None:
    html = (DEMO / "team.html").read_text(encoding="utf-8")
    result = harvest_page(html, "https://acme-demo.ch/team", "acme-demo.ch")
    names = {person.name for person in result.people}
    assert {"Marta Brunner", "Lukas Vogt", "Sara Iten"} <= names
    assert "marta.brunner@acme-demo.ch" in {e.address for e in result.emails}


def test_harvest_org_data() -> None:
    html = (DEMO / "index.html").read_text(encoding="utf-8")
    result = harvest_page(html, "https://acme-demo.ch/", "acme-demo.ch")
    assert result.orgs[0].name == "Acme Demo AG"
    assert result.orgs[0].vat == "CHE-123.456.789"
    assert result.orgs[0].city == "Aarau"


def test_harvest_contact_page() -> None:
    html = (DEMO / "kontakt.html").read_text(encoding="utf-8")
    result = harvest_page(html, "https://acme-demo.ch/kontakt", "acme-demo.ch")
    addresses = {e.address for e in result.emails}
    assert "vertrieb@acme-demo.ch" in addresses
    assert "support@acme-demo.ch" in addresses          # deobfuscated
    assert any(phone.e164 == "+41441234568" for phone in result.phones)


@pytest.mark.parametrize("raw", ["123.456.789", "192.168.1.1", "0012345678901234"])
def test_identifiers_are_not_phones(raw: str) -> None:
    assert looks_like_identifier(raw)


def test_uid_is_not_harvested_as_phone() -> None:
    html = "<p>Handelsregister CHE-123.456.789</p>"
    result = harvest_page(html, "https://acme.ch/impressum", "acme.ch")
    assert result.phones == []


def test_harvest_ignores_asset_urls() -> None:
    result = harvest_page('<img src="logo@2x.png"><a href="mailto:a@b.ch">x</a>',
                          "https://acme.ch", "acme.ch")
    assert [e.address for e in result.emails] == ["a@b.ch"]


def test_harvest_handles_garbage() -> None:
    assert harvest_page("<html", "https://acme.ch", "acme.ch") is not None
    assert harvest_page("", "https://acme.ch", "acme.ch").empty


# -------------------------------------------------------------------- leads
def test_score_prefers_named_personal_contact() -> None:
    rich = score_lead(Lead(email="marta.brunner@acme.ch", email_kind="personal",
                           phone="+41441234567", contact_name="Marta Brunner",
                           role="Geschaeftsfuehrerin", domain="acme.ch", city="Aarau",
                           vat="CHE-123.456.789", categories=["contacts"]))
    thin = score_lead(Lead(email="info@acme.ch", email_kind="role"))
    assert rich.score > thin.score
    assert rich.score <= 100
    assert any("decision maker" in reason for reason in rich.score_reasons)


def test_store_dedupes_by_email(tmp_path: Path) -> None:
    store = LeadStore(tmp_path / "leads.json")
    first, is_new = store.upsert(Lead(email="a@acme.ch", company="Acme"))
    assert is_new
    second, is_new2 = store.upsert(Lead(email="A@acme.ch", phone="+41441234567"))
    assert not is_new2
    assert second.phone == "+41441234567"      # merged
    assert store.stats()["total"] == 1


def test_store_persists_atomically(tmp_path: Path) -> None:
    path = tmp_path / "leads.json"
    store = LeadStore(path)
    store.upsert(Lead(email="x@acme.ch", company="Acme"))
    store.save()
    reloaded = LeadStore(path)
    assert reloaded.stats()["total"] == 1


def test_backfill_fills_company_after_the_fact(tmp_path: Path) -> None:
    store = LeadStore(tmp_path / "leads.json")
    store.upsert(Lead(email="a@acme.ch", domain="acme.ch"))
    assert store.backfill_domain("acme.ch", company="Acme Demo AG", city="Aarau") == 1
    lead = store.all()[0]
    assert lead.company == "Acme Demo AG" and lead.city == "Aarau"
    # idempotent: existing values are never overwritten
    assert store.backfill_domain("acme.ch", company="Other") == 0
    assert store.all()[0].company == "Acme Demo AG"


def test_export_csv_columns(tmp_path: Path) -> None:
    path = export_csv([Lead(email="a@acme.ch", company="Acme")], tmp_path / "leads.csv")
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("id,company,domain")
    assert "a@acme.ch" in text


# --------------------------------------------------------------------- scope
def test_scope_allows_subdomains_only() -> None:
    scope = Scope(domains={"acme.ch"}, enforce=True)
    assert scope.allows("https://acme.ch/kontakt")
    assert scope.allows("https://shop.acme.ch/x")
    assert not scope.allows("https://notacme.ch")
    assert not scope.allows("https://acme.ch.evil.com")


def test_scope_require_raises() -> None:
    with pytest.raises(ScopeError):
        Scope(domains={"acme.ch"}).require("https://other.ch")


def test_scope_can_be_disabled() -> None:
    assert Scope(domains=set(), enforce=False).allows("https://anything.example")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.5", "169.254.1.1"])
def test_ssrf_guard_blocks_internal(host: str) -> None:
    with pytest.raises(BlockedAddressError):
        assert_public_host(host)


# ------------------------------------------------------------------ pipeline
def test_extract_links_ignores_mailto() -> None:
    html = '<a href="/team">t</a><a href="mailto:x@y.ch">m</a><a href="#top">t</a>'
    assert extract_links(html, "https://acme.ch/") == ["https://acme.ch/team"]


def test_demo_end_to_end(tmp_path: Path, library: Library) -> None:
    store = LeadStore(tmp_path / "leads.json")
    runner = Runner(
        library=library,
        store=store,
        output_dir=tmp_path,
        fetcher_factory=lambda scope: FixtureFetcher(root=DEMO, scope=scope),
        scope=Scope(domains=set(), enforce=False),
    )
    manager = JobManager(runner)
    request = RunRequest.from_dict({
        "prompt": "Kontakte von acme-demo.ch in Aarau",
        "domain": "acme-demo.ch",
        "seeds": ["https://acme-demo.ch/", "https://acme-demo.ch/kontakt",
                  "https://acme-demo.ch/team", "https://acme-demo.ch/karriere",
                  "https://acme-demo.ch/impressum"],
        "max_pages": 6,
        "limit": 12,
    })
    manager._counter += 1
    job = manager.create(request)
    runner.execute(job)

    assert job.status == "done", job.error
    assert job.stats["pages_ok"] == 5
    leads = store.all()
    assert any(lead.email == "marta.brunner@acme-demo.ch" for lead in leads)
    assert any(lead.phone for lead in leads)
    assert leads[0].score >= 60
    # one lead per phone number, no UID artefacts, company name everywhere
    phones = [lead.phone for lead in leads if lead.phone]
    assert len(phones) == len(set(phones))
    assert all(len(p) >= 10 for p in phones)
    assert {lead.company for lead in leads} == {"Acme Demo AG"}
    assert store.stats()["total"] == len(leads)
    assert any(path.endswith(".csv") for path in job.stats["reports"])


def test_pipeline_respects_scope(tmp_path: Path, library: Library) -> None:
    store = LeadStore(tmp_path / "leads.json")
    runner = Runner(
        library=library, store=store, output_dir=tmp_path,
        fetcher_factory=lambda scope: FixtureFetcher(root=DEMO, scope=scope),
    )
    manager = JobManager(runner)
    request = RunRequest.from_dict({
        "prompt": "kontakte", "seeds": ["https://outside.example/kontakt"], "max_pages": 2,
    })
    job = manager.create(request)
    runner.execute(job, Scope(domains={"acme.ch"}, enforce=True))
    assert job.status == "done"
    assert job.stats["pages_ok"] == 0
    assert store.stats()["total"] == 0


def test_cancel_stops_the_run(tmp_path: Path, library: Library) -> None:
    store = LeadStore(tmp_path / "leads.json")
    runner = Runner(library=library, store=store, output_dir=tmp_path,
                    fetcher_factory=lambda scope: FixtureFetcher(root=DEMO, scope=scope),
                    scope=Scope(domains=set(), enforce=False))
    job = JobManager(runner).create(RunRequest.from_dict({"prompt": "acme.ch", "max_pages": 3}))
    job.cancel()
    runner.execute(job)
    assert job.status == "cancelled"


# ----------------------------------------------------------------------- api
@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DF_DEMO", "1")
    monkeypatch.setenv("DF_OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setenv("DF_ENFORCE_SCOPE", "false")
    import importlib

    from dorkforge import api

    importlib.reload(api)
    return TestClient(api.app)


def test_api_health(client: TestClient) -> None:
    data = client.get("/api/health").json()
    assert data["status"] == "ok" and data["library_size"] >= 50


def test_api_forge(client: TestClient) -> None:
    response = client.post("/api/forge", json={"prompt": "kontakte von acme.ch", "limit": 5})
    assert response.status_code == 200
    assert 0 < response.json()["count"] <= 5


def test_api_rejects_out_of_scope_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live mode must refuse seeds outside scope.txt (demo mode never hits the network)."""
    import importlib

    from dorkforge import api

    monkeypatch.setenv("DF_DEMO", "0")
    monkeypatch.setenv("DF_OUTPUT_DIR", str(tmp_path / "outputs"))
    importlib.reload(api)
    api.SCOPE.domains = {"acme.ch"}
    api.SCOPE.enforce = True
    client = TestClient(api.app)

    blocked = client.post("/api/jobs", json={"seeds": ["https://evil.example/"]})
    assert blocked.status_code == 403
    allowed = client.post("/api/jobs", json={"seeds": ["https://acme.ch/"]})
    assert allowed.status_code == 202


def test_api_demo_run_end_to_end(client: TestClient) -> None:
    response = client.post("/api/jobs", json={
        "prompt": "Kontakte von acme-demo.ch",
        "domain": "acme-demo.ch",
        "seeds": ["https://acme-demo.ch/", "https://acme-demo.ch/team",
                  "https://acme-demo.ch/kontakt"],
        "max_pages": 3,
    })
    assert response.status_code == 202
    job_id = response.json()["id"]

    import time

    for _ in range(100):
        detail = client.get(f"/api/jobs/{job_id}").json()
        if detail["status"] in {"done", "error"}:
            break
        time.sleep(0.05)
    assert detail["status"] == "done", detail.get("error")
    leads = client.get("/api/leads").json()
    assert leads["count"] >= 3
    csv_response = client.get("/api/leads/export?fmt=csv")
    assert csv_response.status_code == 200
    assert "email" in csv_response.text.splitlines()[0]
