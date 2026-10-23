"""The glossary target must define every governed metric the semantic layers define (or the metric is self-evident)."""

from app.expressibility import glossary_gaps


def test_glossary_covers_every_governed_metric():
    assert glossary_gaps() == []
