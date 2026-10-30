"""Dork recipe catalogue.

Recipes are plain strings with named slots and optional segments:

    site:{domain} filetype:pdf [["kontakt" OR "contact"]]

Slots: {domain} {kw} {ext} {place} {name}
Anything wrapped in ``[[ ... ]]`` is dropped when a slot inside it is empty, so a
recipe never leaks a literal ``{placeholder}`` into the output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

DATA_DIR = Path(__file__).parent / "data"
LIBRARY_PATH = DATA_DIR / "library.json"

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

_SLOT_RE = re.compile(r"\{([a-z_]+)\}")
_OPTIONAL_RE = re.compile(r"\[\[(.*?)\]\]", re.DOTALL)


@dataclass(frozen=True)
class Recipe:
    id: int
    category: str
    risk: str
    query: str
    description: str
    use_case: str

    @property
    def slots(self) -> set[str]:
        return set(_SLOT_RE.findall(self.query))

    def render(self, values: dict[str, str]) -> str | None:
        """Fill slots; return None if a mandatory slot is missing."""
        text = self.query

        def optional(match: re.Match[str]) -> str:
            segment = match.group(1)
            for name in _SLOT_RE.findall(segment):
                if not values.get(name):
                    return " "
            return segment

        text = _OPTIONAL_RE.sub(optional, text)

        def mandatory(name: str) -> str | None:
            value = values.get(name.group(1))
            if not value:
                return "\x00"  # marker -> rejected below
            return value

        filled = _SLOT_RE.sub(lambda m: mandatory(m) or "", text)
        if "\x00" in filled:
            return None
        return re.sub(r"\s+", " ", filled).strip()


@dataclass
class Library:
    entries: list[Recipe]
    version: int = 2

    @classmethod
    def load(cls, path: Path | str = LIBRARY_PATH) -> "Library":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = [
            Recipe(
                id=int(item["id"]),
                category=item["category"],
                risk=item["risk"],
                query=item["q"],
                description=item.get("desc", ""),
                use_case=item.get("use", ""),
            )
            for item in raw["entries"]
        ]
        library = cls(entries=entries, version=int(raw.get("version", 2)))
        library.validate()
        return library

    def validate(self) -> None:
        seen: set[int] = set()
        for entry in self.entries:
            if entry.id in seen:
                raise ValueError(f"duplicate recipe id: {entry.id}")
            seen.add(entry.id)
            if entry.risk not in RISK_ORDER:
                raise ValueError(f"recipe {entry.id}: unknown risk {entry.risk!r}")
            if "{{" in entry.query:
                raise ValueError(f"recipe {entry.id}: malformed slot braces")
            for slot in entry.slots:
                if slot not in {"domain", "kw", "ext", "place", "name"}:
                    raise ValueError(f"recipe {entry.id}: unknown slot {slot!r}")
            for segment in _OPTIONAL_RE.findall(entry.query):
                if not _SLOT_RE.search(segment):
                    raise ValueError(
                        f"recipe {entry.id}: optional segment without a slot is dead "
                        f"weight — make it mandatory: {segment!r}"
                    )

    @property
    def categories(self) -> list[str]:
        return sorted({entry.category for entry in self.entries})

    def by_category(self, category: str) -> list[Recipe]:
        return [entry for entry in self.entries if entry.category == category]

    def allowed(self, *, risk_ceiling: str = "medium",
                categories: Iterable[str] | None = None) -> Iterator[Recipe]:
        ceiling = RISK_ORDER[risk_ceiling]
        wanted = set(categories) if categories else None
        for entry in self.entries:
            if RISK_ORDER[entry.risk] > ceiling:
                continue
            if wanted and entry.category not in wanted:
                continue
            yield entry

    def __len__(self) -> int:
        return len(self.entries)
