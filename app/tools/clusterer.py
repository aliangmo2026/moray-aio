"""错误聚类：把重复错误折叠成模板，避免同一类错误重复 1000 行喂给 LLM。

normalize() 把时间戳/IP/端口/UUID/十六进制串/数字替换成占位符，
cluster() 按模板计数并返回 top_n 类（每类附样本原文）。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import List, Optional

# 顺序敏感：先长模式（时间戳/UUID/IP/HEX），最后才是裸数字
_TS = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}[T ]\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_PORT = re.compile(r"(?<=:)\d{1,5}\b")
# 十六进制串（>=6 位且不全是数字，避免把纯数字 ID 误判成 HEX）
_HEX = re.compile(r"\b(?![0-9]+\b)[0-9a-fA-F]{6,}\b")
# 裸数字（无边界限制：104ms / cache_881.bin 这类紧贴字母下划线的数字也要归一化；
# 放在 TS/UUID/IP/PORT/HEX 之后执行，此时占位符内已无数字，不会误伤）
_NUM = re.compile(r"\d+")


def normalize(line: str) -> str:
    """把一行日志中的可变字段替换为占位符，得到可比较的模板。"""
    text = str(line)
    text = _TS.sub("<TS>", text)
    text = _UUID.sub("<UUID>", text)
    text = _IP.sub("<IP>", text)
    text = _PORT.sub("<PORT>", text)
    text = _HEX.sub("<HEX>", text)
    text = _NUM.sub("<NUM>", text)
    return text


def cluster(lines: Optional[List[str]], top_n: int = 10) -> List[dict]:
    """按归一化模板聚类，返回 [{template, count, sample_line}]，按 count 降序取 top_n。

    top_n<=0 表示不截断，返回全部类。
    """
    counter: Counter = Counter()
    samples = {}
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
