"""The business glossary (app/glossary.md): full text, plus simple keyword search over its entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

GLOSSARY_PATH = Path(__file__).with_name('glossary.md')
TEXT = GLOSSARY_PATH.read_text()

_ENTRY = re.compile(r'^\*\*(?P<term>[^*]+)\*\*:\s*(?P<body>.*)$')
_WORD = re.compile(r'[a-z0-9]+')
_STOPWORDS = {'a', 'an', 'and', 'are', 'as', 'at', 'by', 'definition', 'define', 'for', 'how', 'in', 'is', 'of',
              'on', 'or', 'our', 'the', 'to', 'we', 'what', 'with'}


@dataclass(frozen=True)
class Entry:
    term: str
    section: str
    definition: str


def _words(text: str) -> set[str]:
    # Crude stemming so "customers"/"customer" and "orders"/"order" match.
    return {w.rstrip('s') if len(w) > 3 else w for w in _WORD.findall(text.lower())} - _STOPWORDS


@cache
def entries() -> tuple[Entry, ...]:
    """Each **Term**: paragraph (with any bullet lines under it), plus the intro as 'Reporting conventions'."""
    out: list[Entry] = []
    section, intro, current = '', [], None
    for line in TEXT.splitlines():
        if line.startswith('## '):
            section, current = line[3:].strip(), None
        elif m := _ENTRY.match(line):
            current = [m['term'].strip(), m['body'].strip()]
            out.append(current)  # type: ignore[arg-type]
            current.append(section)
        elif line.strip() and current is not None:
            current[1] += '\n' + line.strip()
        elif not line.strip():
            current = None
        elif not section and not line.startswith('#'):
            intro.append(line.strip())
    result = [Entry(term=t, section=s, definition=d) for t, d, s in out]
    return (Entry('Reporting conventions', 'General', ' '.join(intro)), *result)


def search(query: str, limit: int = 3) -> list[dict]:
    """Rank entries by keyword overlap with the query; matches in the term name count triple."""
    q = _words(query)
    scored = []
    for e in entries():
        score = 3 * len(q & _words(e.term)) + len(q & _words(e.definition))
        if score:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [{'term': e.term, 'section': e.section, 'definition': e.definition} for _, e in scored[:limit]]


if __name__ == '__main__':
    for e in entries():
        print(f'[{e.section}] {e.term}: {e.definition[:70]}')
    for q in ['MRR', 'net revenue', 'active customers', 'first response time', 'inventory available', 'warp drive']:
        print(f'\n{q!r} ->', [r['term'] for r in search(q)])
