"""可验证的检索审计状态与语料指纹。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Iterable


def _normalize(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).replace("\r\n", "\n").strip()


def _clause_payload(doc: tuple) -> dict[str, str]:
    return {
        "law": _normalize(doc[0]),
        "article": _normalize(doc[2]),
        "body": _normalize(doc[4]),
        "status": _normalize(doc[7]),
        "layer": _normalize(doc[8]),
        "file": _normalize(doc[5]),
    }


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def clause_key(doc: tuple) -> str:
    payload = _clause_payload(doc)
    return f"{payload['law']}/{payload['article']}"


def clause_hash(doc: tuple) -> str:
    return _digest(_clause_payload(doc))


def corpus_fingerprint(documents: Iterable[tuple]) -> str:
    rows = [
        {"key": clause_key(doc), "hash": clause_hash(doc)}
        for doc in documents
    ]
    rows.sort(key=lambda row: row["key"])
    return _digest(rows)


def metadata_fingerprint(documents: Iterable[tuple]) -> str:
    rows = []
    for doc in documents:
        rows.append({
            "key": clause_key(doc),
            "status": _normalize(doc[7]),
            "layer": _normalize(doc[8]),
        })
    rows.sort(key=lambda row: row["key"])
    return _digest(rows)


def fingerprint_metadata(documents: Iterable[tuple]) -> dict:
    docs = list(documents)
    return {
        "corpus_hash": corpus_fingerprint(docs),
        "metadata_hash": metadata_fingerprint(docs),
        "clause_hashes": {clause_key(doc): clause_hash(doc) for doc in docs},
        "corpus_count": len(docs),
    }


def compare_index(index_meta: dict | None, documents: Iterable[tuple]) -> dict:
    current = fingerprint_metadata(documents)
    meta = index_meta or {}
    old_hashes = meta.get("clause_hashes")
    changed = []
    if isinstance(old_hashes, dict):
        keys = set(current["clause_hashes"]) | set(old_hashes)
        changed = sorted(key for key in keys if current["clause_hashes"].get(key) != old_hashes.get(key))
    else:
        changed = sorted(current["clause_hashes"])
    valid = (
        bool(meta.get("corpus_hash"))
        and meta.get("corpus_hash") == current["corpus_hash"]
        and meta.get("metadata_hash") == current["metadata_hash"]
        and isinstance(old_hashes, dict)
        and not changed
    )
    return {
        **current,
        "status": "valid" if valid else "stale",
        "changed_clauses": changed,
    }
