#!/usr/bin/env python3
"""在线兜底检索：本地语料没收录、或可能已经过期时，去官方库实时查一次。

什么时候用它
  · 本地语料里搜不到（可能是快照之后新立/新修的法）
  · 用户质疑"这条现在还生效吗""是不是有新版本"
  · 本地快照已经放了很久（rank_search 会在超过 180 天时主动提醒）
  · 需要核对某部法规在官方库里的最新条目

它做什么、不做什么
  ✅ 调官方检索接口，给出条目名称、时效性、公布/施行日期、制定机关、官方链接
  ❌ **不抓正文**。这里只做单次一页的检索，正文请打开官方链接查看；要把正文抓到
     本地做离线检索，跑 `python scripts/fetch_downloads.py` 再按 README「更新维护」
     重建语料。

用法
  python scripts/search_live.py 民营经济促进法
  python scripts/search_live.py 数据安全 条例 --limit 5
  python scripts/search_live.py --current-only 网络数据安全管理条例
  python scripts/search_live.py --json 人工智能法
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_corpus import (  # noqa: E402
    format_live_items,
    live_search,
    snapshot_age_days,
    snapshot_info,
)

DEFAULT_CORPUS = Path(__file__).resolve().parents[1] / "references" / "corpus"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="官方库实时检索（兜底，不抓正文）")
    parser.add_argument("query", nargs="+", help="关键词，如「民营经济促进法」")
    parser.add_argument("--limit", type=int, default=10, help="最多返回几条（默认 10）")
    parser.add_argument("--current-only", action="store_true", help="只要现行有效/尚未生效")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--timeout", type=int, default=30, help="单次请求超时秒数")
    parser.add_argument("--corpus", default=None, help="本地语料目录（仅用于报告快照日期）")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    corpus_root = Path(args.corpus).expanduser().resolve() if args.corpus else DEFAULT_CORPUS
    retrieved, count = snapshot_info(corpus_root)
    age = snapshot_age_days(retrieved)
    query = " ".join(args.query)

    try:
        items, total, note = live_search(
            query, limit=args.limit, current_only=args.current_only, timeout=args.timeout
        )
    except Exception as exc:  # noqa: BLE001 - 网络失败要给人话，不要抛栈
        if args.json:
            print(json.dumps({"error": str(exc), "query": query}, ensure_ascii=False, indent=1))
        else:
            print(f"查询官方库失败：{exc}")
            print("网络不可用时无法兜底；本地语料仍可正常检索。")
        return 1

    if args.json:
        print(
            json.dumps(
                {
                    "query": query,
                    "local_snapshot": retrieved,
                    "local_count": count,
                    "snapshot_age_days": age,
                    "total": total,
                    "items": items,
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        return 0

    snapshot_note = f"本地语料快照：{retrieved or '未知'}"
    if age is not None:
        snapshot_note += f"（距今 {age} 天）"
    print(f"官方检索：{query}　｜　{snapshot_note}")
    print()
    for line in format_live_items(items, total, note):
        print(line)
    if not items:
        return 0
    print()
    print("提醒：")
    print("  · 以上只是官方库的条目信息与链接，正文请打开官方链接查看（本工具不抓取正文）。")
    print("  · 本地语料若未收录该件，按 README「更新维护」更新后即可离线检索全文，")
    print("    判断「新法是否已生效」时也请以官方链接里的时效性为准。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
