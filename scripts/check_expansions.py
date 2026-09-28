#!/usr/bin/env python3
"""校验 references/query-expansions.json 里的扩展词在语料中确实存在。

扩展词写错（比如用了罪名而不是法条用语）不会报错，只会静默召回为 0，
所以每次新增条目后跑一次本脚本。

用法
  python scripts/check_expansions.py           # 列出每条扩展词的命中情况
  python scripts/check_expansions.py --quiet   # 只列未命中的
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验查询扩展表")
    parser.add_argument("--quiet", action="store_true", help="只显示未命中的条目")
    parser.add_argument("--corpus", default=None, help="语料目录")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    skill_dir = Path(__file__).resolve().parents[1]
    corpus_root = (
        Path(args.corpus).expanduser().resolve()
        if args.corpus
        else skill_dir / "references" / "corpus"
    )
    table_path = skill_dir / "references" / "query-expansions.json"
    if not table_path.exists():
        sys.exit(f"找不到 {table_path}")
    table = json.loads(table_path.read_text(encoding="utf-8"))

    texts = []
    for path in sorted(corpus_root.rglob("*.md")):
        texts.append((path.name, path.read_text(encoding="utf-8", errors="replace")))
    if not texts:
        sys.exit(f"{corpus_root} 下没有语料文件")
    print(f"语料文件 {len(texts)} 个，检查 {sum(1 for k in table if not k.startswith('_'))} 个口语键\n")

    missing: list[tuple[str, str]] = []
    for key, values in table.items():
        if key.startswith("_") or not isinstance(values, list):
            continue
        for value in values:
            hits = sum(1 for _, text in texts if value in text)
            if hits:
                if not args.quiet:
                    print(f"  OK   {key} → {value}（命中 {hits} 部）")
            else:
                missing.append((key, value))
                print(f"  未命中 {key} → {value}")

    if missing:
        print(f"\n有 {len(missing)} 条扩展词在语料中不存在，请改用条文里的实际表述：")
        for key, value in missing:
            print(f"  - {key} → {value}")
        return 1
    print("\n全部扩展词在语料中均可命中。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
