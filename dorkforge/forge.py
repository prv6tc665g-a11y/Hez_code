"""Target + library -> ready-to-run dork queries."""

from __future__ import annotations

from dataclasses import dataclass, field

from .library import RISK_ORDER, Library, Recipe
from .parser import Target


@dataclass
class ForgedDork:
    query: str
    category: str
    risk: str
    recipe_id: int
    description: str
    use_case: str
    engine_url: str = ""

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "category": self.category,
            "risk": self.risk,
            "recipe_id": self.recipe_id,
            "description": self.description,
            "use_case": self.use_case,
            "engine_url": self.engine_url,
        }


ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "startpage": "https://www.startpage.com/sp/search?query={q}",
    "brave": "https://search.brave.com/search?q={q}",
    "mojeek": "https://www.mojeek.com/search?q={q}",
}


def search_url(query: str, engine: str = "google") -> str:
    from urllib.parse import quote_plus

    template = ENGINES.get(engine, ENGINES["google"])
    return template.format(q=quote_plus(query))


@dataclass
class ForgeResult:
    target: Target
    dorks: list[ForgedDork] = field(default_factory=list)
    skipped: int = 0

    def by_category(self) -> dict[str, list[ForgedDork]]:
        grouped: dict[str, list[ForgedDork]] = {}
        for dork in self.dorks:
            grouped.setdefault(dork.category, []).append(dork)
        return grouped


def forge(
    target: Target,
    library: Library,
    *,
    limit: int = 24,
    risk_ceiling: str | None = None,
    engine: str = "google",
    category: str | None = None,
) -> ForgeResult:
    """Render recipes against a target.

    Preferred order: the target's detected category first, then the categories
    most useful for lead generation, then everything else inside the risk ceiling.
    """
    ceiling = risk_ceiling or target.risk_ceiling
    values = target.slots()

    preference = [target.category] if target.category else []
    preference = [c for c in preference if c]
    preference += [c for c in ("contacts", "people", "company", "careers",
                               "documents", "directories", "events", "social",
                               "infrastructure", "exposure")
                   if c not in preference]

    def rank(recipe: Recipe) -> tuple[int, int, int]:
        try:
            bucket = preference.index(recipe.category)
        except ValueError:
            bucket = len(preference)
        return (bucket, RISK_ORDER[recipe.risk], recipe.id)

    categories = [category] if category else None
    candidates = sorted(library.allowed(risk_ceiling=ceiling, categories=categories), key=rank)

    result = ForgeResult(target=target)
    seen: set[str] = set()
    for recipe in candidates:
        if len(result.dorks) >= limit:
            break
        query = recipe.render(values)
        if not query:
            result.skipped += 1
            continue
        key = query.lower()
        if key in seen:
            result.skipped += 1
            continue
        seen.add(key)
        result.dorks.append(
            ForgedDork(
                query=query,
                category=recipe.category,
                risk=recipe.risk,
                recipe_id=recipe.id,
                description=recipe.description,
                use_case=recipe.use_case,
                engine_url=search_url(query, engine),
            )
        )
    return result


def variants(query: str) -> list[str]:
    """Cheap high-value rewrites: widen the file type, flip intitle/intext."""
    out: list[str] = []
    if "filetype:pdf" in query:
        out.append(query.replace("filetype:pdf", "(filetype:docx OR filetype:xlsx)"))
    if "inurl:" not in query and query.startswith("site:"):
        head, _, tail = query.partition(" ")
        if tail:
            out.append(f"{head} intitle:{tail.split()[0].strip(chr(34))}")
    if "-inurl:" not in query:
        out.append(f"{query} -inurl:(htm|html|php)")
    return out[:3]
