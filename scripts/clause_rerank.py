#!/usr/bin/env python3
"""候选级"款拆分"重排：把已召回的条文按款拆开，用现有嵌入模型重新打分。

为什么需要：100 题主测试集实测 —— 召回已经到 **@50 = 94%**，但 **@5 只有 69%**，
中间 25 点是"找到了但排太深"。原因是长条文被整条压成一个向量时，真正对上的
那一款会被其余内容稀释；把候选条文按款拆开、取"与查询最相似的那一款"作为该
条文的分数，能把对的抬到前面。

零新增依赖：沿用索引里同一个嵌入模型（`index["meta"]["model"]`），不建新索引、
不装新模型、不引入交叉编码器——先验证"款级粒度"是不是缺的那一步。

同时它也是 A+C+B 切分方案的轻量版：这里只对**候选条文**现场拆款（每题约几十
段），而不是把全库 57k 条全部重切重建索引。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

ARTICLE_RE = re.compile(r"^第[零一二三四五六七八九十百千万两]+\s*条")
ITEM_RE = re.compile(r"^[（(][一二三四五六七八九十]+[）)]")
MAX_SEGMENTS = 8  # 超长条文只取前若干款，控制每题嵌入量


@lru_cache(maxsize=512)
def _file_lines(path: str) -> tuple[str, ...]:
    """按文件缓存内容——100 题 × 20 候选会产生几千次读取，必须缓存。"""
    return tuple(Path(path).read_text(encoding="utf-8").split("\n"))


def resolve_path(corpus_root: Path, doc: tuple) -> Path | None:
    """doc[5] 形如 `corpus/社会法/xxx.md`（相对 references/），给出真实路径。"""
    rel = str(doc[5])
    for cand in (corpus_root.parent / rel, corpus_root / rel.removeprefix("corpus/")):
        if cand.exists():
            return cand
    return None


def doc_segments(corpus_root: Path, doc: tuple) -> list[str]:
    """把一条条文拆成"款"级片段，并带上语境前缀（法名 + 条号 + 第N款）。"""
    path = resolve_path(corpus_root, doc)
    if path is None:
        return [f"{doc[0]} {doc[2]} {doc[4]}".strip()]
    lines = _file_lines(str(path))
    label = str(doc[2])
    start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(label) and ARTICLE_RE.match(s):
            start = i
            break
    if start is None:
        return [f"{doc[0]} {doc[2]} {doc[4]}".strip()]

    paragraphs: list[str] = []
    for line in lines[start:]:
        s = line.strip()
        if paragraphs and ARTICLE_RE.match(s):
            break
        if s and not s.startswith("#"):
            paragraphs.append(s)

    # 款 = 不以「（一）」开头的段落；项段落并入其所属的款
    clauses: list[str] = []
    for text in paragraphs:
        if ITEM_RE.match(text) and clauses:
            clauses[-1] += " " + text
        else:
            clauses.append(text)
    if len(clauses) <= 1:
        return [f"{doc[0]} {doc[2]} {clauses[0] if clauses else doc[4]}".strip()]
    head = f"{doc[0]} {doc[2]}"
    return [f"{head} 第{i}款 {text}".strip() for i, text in enumerate(clauses[:MAX_SEGMENTS], 1)]


def rerank(
    index: dict,
    corpus_root: Path,
    docs: list[tuple],
    query: str,
    base: str,
    keep: int,
    embed_texts,
) -> list[tuple]:
    """按"最相似的款"给候选条文重新打分并返回前 keep 条。

    模型不可用、或所有候选都拆不出款时，原样返回（不抛异常、不中断评测）。
    """
    if not docs:
        return []
    import numpy as np

    model = index["meta"].get("model")
    segments_per_doc = [doc_segments(corpus_root, d) for d in docs]
    flat = [seg for segs in segments_per_doc for seg in segs]
    try:
        vectors = np.asarray(embed_texts(model, [query] + flat, base=base), dtype=np.float32)
    except Exception:  # noqa: BLE001 - 重排是增强项，失败就退回原顺序
        return docs[:keep]

    def unit(v):
        n = float(np.linalg.norm(v)) or 1.0
        return v / n

    query_vec = unit(vectors[0])
    seg_vecs = [unit(v) for v in vectors[1:]]
    scores: list[tuple[float, int]] = []
    cursor = 0
    for i, segs in enumerate(segments_per_doc):
        best = max((float(query_vec @ seg_vecs[cursor + j]) for j in range(len(segs))), default=0.0)
        cursor += len(segs)
        scores.append((best, i))
    scores.sort(key=lambda x: -x[0])
    return [docs[i] for _s, i in scores[:keep]]
