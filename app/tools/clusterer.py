"""Error clustering: fold repeated errors into templates so the LLM never sees 1000 dupes.

normalize() replaces timestamps/IPs/ports/UUIDs/hex strings/numbers with placeholders;
cluster() counts templates and returns the top_n classes (each with a sample line).

Performance: normalization runs ONE precompiled alternation pass (instead of six
sequential ``.sub`` calls), which matters at million-line scale — every pattern is a
module-level ``re.Pattern`` compiled exactly once at import.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import List, Optional

# Single combined pattern; alternative order preserves the original semantics:
# timestamps/UUIDs/IPs first, then port (only after ':'), then hex (not all-digits),
# and bare digits last.
_COMBINED_RE = re.compile(
    r"(?P<ts>\d{4}-\d{1,2}-\d{1,2}[T ]\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
    r"|(?P<uuid>\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b)"
    r"|(?P<ip>\b\d{1,3}(?:\.\d{1,3}){3}\b)"
    r"|(?P<port>(?<=:)\d{1,5}\b)"
    r"|(?P<hex>\b(?![0-9]+\b)[0-9a-fA-F]{6,}\b)"
    r"|(?P<num>\d+)"
)
_PLACEHOLDERS = {
    "ts": "<TS>",
    "uuid": "<UUID>",
    "ip": "<IP>",
    "port": "<PORT>",
    "hex": "<HEX>",
    "num": "<NUM>",
}


def normalize(line: str) -> str:
    """Replace volatile fields of a log line with placeholders, producing a template."""
    return _COMBINED_RE.sub(lambda m: _PLACEHOLDERS[m.lastgroup], str(line))


def cluster(lines: Optional[List[str]], top_n: int = 10) -> List[dict]:
    """Group lines by normalized template; returns [{template, count, sample_line}]."""
    counter: Counter = Counter()
    samples: dict = {}
    for line in lines or []:
        template = normalize(line)
        counter[template] += 1
        samples.setdefault(template, line)
    limit = int(top_n) if top_n and int(top_n) > 0 else None
    ranked = counter.most_common(limit)
    return [
        {"template": template, "count": count, "sample_line": samples[template]}
        for template, count in ranked
    ]
