"""Error clustering: fold repeated errors into templates so the LLM never sees 1000 dupes.

normalize() replaces timestamps/IPs/ports/UUIDs/hex strings/numbers with placeholders;
cluster() counts templates and returns the top_n classes (each with a sample line).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import List, Optional

# Order matters: long patterns first (timestamp/UUID/IP/HEX), bare digits last
_TS = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}[T ]\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_PORT = re.compile(r"(?<=:)\d{1,5}\b")
# Hex token (>=6 chars, not all digits — keeps pure numeric IDs out of HEX)
_HEX = re.compile(r"\b(?![0-9]+\b)[0-9a-fA-F]{6,}\b")
# Bare digits without boundaries: "104ms" / "cache_881.bin" must fold too;
# runs after TS/UUID/IP/PORT/HEX so placeholders contain no digits and are safe
_NUM = re.compile(r"\d+")


def normalize(line: str) -> str:
    """Replace volatile fields of a log line with placeholders, producing a template."""
    text = str(line)
    text = _TS.sub("<TS>", text)
    text = _UUID.sub("<UUID>", text)
    text = _IP.sub("<IP>", text)
    text = _PORT.sub("<PORT>", text)
    text = _HEX.sub("<HEX>", text)
    text = _NUM.sub("<NUM>", text)
    return text


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
