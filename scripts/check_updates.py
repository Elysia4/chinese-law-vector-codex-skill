#!/usr/bin/env python3
"""检查本地语料是否落后于国家法律法规数据库的现行有效清单。

只联网读元数据，不下载全文、不改任何文件。用它回答一个问题：
"我这套语料该重建了吗？"

用法
  python scripts/check_updates.py           # 人读报告
  python scripts/check_updates.py --quiet   # 只在有变化时输出（适合定时任务）
  python scripts/check_updates.py --json    # 机器可读

发现变化后照提示走两步：重建语料 → 重建向量索引。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_corpus import build_checklist  # noqa: E402
from rank_search import load_manifest  # noqa: E402  不依赖 numpy，启动更快


def local_index(manifest: dict) -> dict[str, dict]:
    """只比对默认检索层（现行有效 + 尚未生效）；历史版本不在官方"现行"清单里，属于预期内。"""
    return {
        record["title"]: record
        for record in manifest["laws"]
        if record.get("layer", "current") == "current"
    }


def remote_index() -> dict[str, dict]:
    """官方清单取"现行有效 + 尚未生效"，与语料 current 层同口径。"""
    from fetch_corpus import CONSTITUTION_CODE, CATEGORIES, CURRENT, flk_rows, make_item, clean_title

    items = []
    for row in flk_rows(CONSTITUTION_CODE, statuses=(CURRENT, 4)):
        title = clean_title(row["title"])
        if "修正文本" in title or "修正案" in title:
            items.append(make_item(row, title, "宪法"))
    for code, category in CATEGORIES:
        for row in flk_rows(code, statuses=(CURRENT, 4)):
            items.append(make_item(row, clean_title(row["title"]), category))
    return {item["title"]: item for item in items}


def diff(local: dict[str, dict], remote: dict[str, dict]) -> dict[str, list]:
    added = [remote[t] for t in sorted(set(remote) - set(local))]
    gone = [local[t] for t in sorted(set(local) - set(remote))]
    changed = []
    for title in sorted(set(local) & set(remote)):
        old, new = local[title], remote[title]
        if (old.get("promulgated") or "") != (new.get("promulgated") or "") or (
            old.get("effective") or ""
        ) != (new.get("effective") or ""):
            changed.append((old, new))
    return {"added": added, "gone": gone, "changed": changed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查语料是否落后于官方现行有效清单")
    parser.add_argument("--quiet", action="store_true", help="只在有变化时输出")
    parser.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON")
    parser.add_argument("--corpus", default=None, help="语料目录")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    skill_dir = Path(__file__).resolve().parents[1]
    corpus_root = (
        Path(args.corpus).expanduser().resolve()
        if args.corpus
        else skill_dir / "references" / "corpus"
    )
    manifest = load_manifest(corpus_root)
    snapshot = manifest.get("retrieved", "未知")

    try:
        remote = remote_index()
    except Exception as exc:  # noqa: BLE001 - 联网失败要说清楚，不能假装没事
        print(f"无法连接国家法律法规数据库，本次未做检查：{exc}")
        return 2

    local = local_index(manifest)
    result = diff(local, remote)
    total_changes = len(result["added"]) + len(result["gone"]) + len(result["changed"])

    if args.as_json:
        payload = {
            "snapshot": snapshot,
            "local_count": len(local),
            "remote_count": len(remote),
            "changes": total_changes,
            "added": [x["title"] for x in result["added"]],
            "gone": [x["title"] for x in result["gone"]],
            "changed": [
                {"title": new["title"], "local": old.get("promulgated"),
                 "remote": new.get("promulgated")}
                for old, new in result["changed"]
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 1 if total_changes else 0

    if total_changes == 0:
        if not args.quiet:
            print(f"语料是最新的：本地 {len(local)} 件 / 官方现行有效 {len(remote)} 件，无差异。")
            print(f"本地快照时间：{snapshot}")
        return 0

    print(f"发现 {total_changes} 处变化（本地快照 {snapshot}）：\n")
    if result["added"]:
        print(f"■ 官方已有、本地没有（{len(result['added'])} 件，可能是新通过的法律）")
        for item in result["added"]:
            print(f"    {item['title']}｜公布 {item['promulgated'] or '—'}｜{item['category']}")
    if result["gone"]:
        print(f"\n■ 本地有、官方现行有效清单里已没有（{len(result['gone'])} 件，可能已废止或被法典取代）")
        for item in result["gone"]:
            print(f"    {item['title']}｜原公布 {item['promulgated'] or '—'}")
    if result["changed"]:
        print(f"\n■ 版本变化（{len(result['changed'])} 件，官方已出新版本）")
        for old, new in result["changed"]:
            print(f"    {new['title']}：本地 {old.get('promulgated') or '—'} → 官方 {new.get('promulgated') or '—'}")

    print("\n照下面两步重建（先语料、后索引，顺序不能反）：")
    print("  python scripts/fetch_corpus.py")
    print("  python scripts/build_vectors.py --model qwen3-embedding:0.6b")
    print("\n注意：向量是按“文件#行号”挂到条文上的，重建语料后必须重建索引。")
    print("忘了重建也不会给错排序——rank_search.py 会检测到不匹配并退回纯 BM25，同时提示。")
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
