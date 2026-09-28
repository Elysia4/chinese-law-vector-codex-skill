#!/usr/bin/env python3
"""检索 china-law 技能自带的离线法律语料。

用法
  python scripts/search_corpus.py 股东出资 期限              # 全文检索，多词默认 AND
  python scripts/search_corpus.py --any 竞业限制 保密         # 多词 OR
  python scripts/search_corpus.py --law 公司法 董事会决议      # 限定某部法律
  python scripts/search_corpus.py --law 民法典 --article 1260  # 按条文号输出整条
  python scripts/search_corpus.py --category 刑法 正当防卫     # 限定部门法
  python scripts/search_corpus.py --list 个人信息             # 只按法律名/元数据查
  python scripts/search_corpus.py --stats                    # 语料收录统计

条文号可以写中文（第八十八条）也可以写数字（88）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from fetch_corpus import live_hint, stale_note

# 容忍「第二百七十一 条」这类条号内混入空白的情况（docx 解析的现实）
ARTICLE_RE = re.compile(r"^第[零一二三四五六七八九十百千两]+\s*条")
DIGITS = "零一二三四五六七八九"
UNITS = ["", "十", "百", "千"]

LAYER_LABEL = {
    "current": "现行有效/尚未生效",
    "historical": "已修改/已废止",
    "decision": "修改废止决定",
    "unknown": "状态未确认",
}


def to_chinese(number: int) -> str:
    """1 → 一，110 → 一百一十，1260 → 一千二百六十。"""
    if number <= 0:
        return ""
    digits = str(number)
    length = len(digits)
    out = ""
    pending_zero = False
    for index, char in enumerate(digits):
        digit = int(char)
        unit = UNITS[length - 1 - index]
        if digit == 0:
            pending_zero = True
            continue
        if pending_zero and out:
            out += "零"
        pending_zero = False
        if not (digit == 1 and unit == "十" and index == 0):
            out += DIGITS[digit]
        out += unit
    return out


def normalize_article(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.isdigit():
        return f"第{to_chinese(int(raw))}条"
    # 已含"条"的原样使用，兼容"第二百二十四条之一"这类修正案插入条文
    if "条" in raw:
        return raw if raw.startswith("第") else f"第{raw}"
    return f"第{raw}条"


def normalize_title(title: str) -> str:
    return re.sub(r"^中华人民共和国", "", title or "").strip()


def load_manifest(corpus_root: Path) -> dict:
    manifest_path = corpus_root / "manifest.json"
    if not manifest_path.exists():
        sys.exit(f"找不到 {manifest_path}，请先运行 scripts/fetch_corpus.py 生成语料。")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def record_path(corpus_root: Path, record: dict) -> Path:
    return corpus_root / record["file"].removeprefix("corpus/")


def select_records(manifest: dict, args: argparse.Namespace) -> list[dict]:
    selected = []
    for record in manifest["laws"]:
        if record.get("layer", "current") not in args.layers:
            continue
        if args.law:
            key = normalize_title(args.law)
            if key not in record["title"] and args.law not in record["title"]:
                continue
        if args.category and args.category not in record["category"]:
            continue
        selected.append(record)
    return selected


def load_law(path: Path) -> tuple[list[str], int]:
    """返回（正文行, 正文首行的文件行号偏移），使输出行号与文件真实行号一致。"""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return lines[index + 1 :], index + 1
    return lines, 0


def searchable(lines: list[str]) -> str:
    return "\n".join(line for line in lines if not line.lstrip().startswith("> "))


def locate_article(lines: list[str], article: str) -> tuple[int, int] | None:
    start = next((i for i, line in enumerate(lines) if line.startswith(article)), None)
    if start is None:
        return None
    end = next(
        (i for i in range(start + 1, len(lines)) if ARTICLE_RE.match(lines[i])),
        len(lines),
    )
    return start, end


def enclosing_label(lines: list[str], index: int) -> str:
    for i in range(index, -1, -1):
        if ARTICLE_RE.match(lines[i]):
            return lines[i].split()[0]
        if lines[i].startswith("## "):
            return lines[i].lstrip("# ").strip()
    return ""


def clip(text: str, width: int) -> str:
    text = text.strip()
    return text if len(text) <= width else text[:width] + "…"


def print_header(record: dict, corpus_root: Path, args: argparse.Namespace) -> None:
    print(
        f"### {record['title']}"
        f"　（{record['category']}｜公布 {record['promulgated'] or '—'}"
        f"｜施行 {record['effective'] or '—'}｜{record['status']}）"
    )
    path = record_path(corpus_root, record)
    try:
        shown = path.relative_to(args.base_dir)
    except ValueError:
        shown = path
    print(f"文件：{shown}")
    print(f"官方全文：{record['official_url']}")


def run_list(records: list[dict], corpus_root: Path, args: argparse.Namespace) -> int:
    terms = args.terms
    selected = []
    for record in records:
        haystack = f"{record['title']} {record['category']} {record['issuer']}"
        if terms and not all(term in haystack for term in terms):
            continue
        selected.append(record)
    if not selected:
        print("没有匹配的法律。")
        return 1
    print(f"匹配 {len(selected)} 部法律：\n")
    print("| 法律 | 部门法 | 公布 | 施行 | 时效性 | 全文 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for record in selected[: args.limit_laws]:
        text_status = "有" if record["text_status"] == "full" else "仅元数据"
        print(
            f"| {record['title']} | {record['category']} | {record['promulgated'] or '—'} | "
            f"{record['effective'] or '—'} | {record['status']} | {text_status} |"
        )
    if len(selected) > args.limit_laws:
        print(f"\n（仅显示前 {args.limit_laws} 条，共 {len(selected)} 条）")
    return 0


def run_stats(manifest: dict, records: list[dict], args: argparse.Namespace) -> int:
    print(f"快照时间：{manifest.get('retrieved', '未知')}")
    print(f"数据来源：{manifest.get('text_source') or manifest.get('metadata_source', '未知')}")
    full = sum(1 for r in records if r.get("text_status") == "full")
    print(f"收录：{len(records)} 件（有全文 {full} 件，仅元数据 {len(records) - full} 件）")
    layers = manifest.get("by_layer") or {}
    if layers:
        label = {
            "current": "现行有效/尚未生效（默认检索）",
            "historical": "已修改/已废止（需 --include-repealed）",
            "decision": "修改/废止决定（需 --all-layers）",
            "unknown": "状态未确认",
        }
        print("分层：", "；".join(f"{label.get(k, k)} {v} 件" for k, v in layers.items()))
    print("当前检索层：", "、".join(LAYER_LABEL.get(x, x) for x in args.layers))
    print()
    by_category: dict[str, list[dict]] = {}
    for record in records:
        by_category.setdefault(record["category"], []).append(record)
    for category, group in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
        full = sum(1 for r in group if r["text_status"] == "full")
        print(f"  {category:<12} {len(group):>4} 件（全文 {full}）")
    missing = [r for r in records if r["text_status"] != "full"]
    if missing:
        print(f"\n仅元数据（{len(missing)} 件，需查官方链接）：")
        for record in missing:
            print(f"  - {record['title']}（{record['category']}，公布 {record['promulgated'] or '—'}）")
    return 0


def run_search(records: list[dict], corpus_root: Path, args: argparse.Namespace) -> int:
    terms = args.terms
    article = normalize_article(args.article) if args.article else None
    laws_shown = 0
    total_hits = 0
    metadata_only: list[dict] = []

    for record in records:
        if record["text_status"] != "full":
            metadata_only.append(record)
            continue
        path = record_path(corpus_root, record)
        if not path.exists():
            continue
        lines, offset = load_law(path)

        if article:
            located = locate_article(lines, article)
            if not located:
                continue
            start, end = located
            block = "\n".join(lines[start:end]).strip()
            if terms and not all(term in block for term in terms):
                continue
            print_header(record, corpus_root, args)
            print()
            print(block)
            print()
            laws_shown += 1
            total_hits += 1
            if laws_shown >= args.limit_laws:
                break
            continue

        if not terms:
            continue

        text = searchable(lines)
        matched = any(term in text for term in terms) if args.any else all(
            term in text for term in terms
        )
        if not matched:
            continue

        offsets = [i for i, line in enumerate(lines) if any(term in line for term in terms)]
        ranked = sorted(
            offsets,
            key=lambda i: (-sum(term in lines[i] for term in terms), i),
        )[: args.hits]
        ranked.sort()

        print_header(record, corpus_root, args)
        for index in ranked:
            label = enclosing_label(lines, index)
            prefix = f"L{index + offset + 1}".rjust(6)
            print(f"{prefix}  {clip(lines[index], args.width)}")
            if label:
                print(f"        所属：{label}")
        print()
        laws_shown += 1
        total_hits += len(ranked)
        if laws_shown >= args.limit_laws:
            print(f"（已到 --limit-laws={args.limit_laws}，可能还有更多命中）")
            break

    # 只在用户点名某部法律时提示"仅元数据"，全库检索时保持输出干净
    if metadata_only and args.law:
        print("以下匹配的法律本地语料只收录了元数据，没有条文全文（请查官方链接，不要凭记忆引用）：")
        for record in metadata_only[: args.limit_laws]:
            print(
                f"  - {record['title']}（{record['category']}｜公布 {record['promulgated'] or '—'}"
                f"｜施行 {record['effective'] or '—'}｜{record['status']}）"
            )
            print(f"    官方全文：{record['official_url']}")
        print()

    if laws_shown == 0:
        if metadata_only and args.law:
            return 1
        print("语料中没有匹配内容。")
        print("可尝试：换关键词、用 --any 放宽为 OR、--list 按法律名查找，或确认该法是否只收录了元数据。")
        caution = stale_note(args.corpus_root)
        if caution:
            print(caution)
        print()
        print(live_hint(" ".join(args.terms)))
        return 1
    print(f"共 {laws_shown} 部法律、{total_hits} 处命中。")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检索 china-law 技能的离线法律语料", add_help=True
    )
    parser.add_argument("terms", nargs="*", help="检索关键词，多个关键词默认 AND")
    parser.add_argument("--law", help="限定法律名称（可用简称，如 公司法）")
    parser.add_argument("--category", help="限定部门法，如 民法商法、刑法")
    parser.add_argument("--article", help="按条文号输出整条，如 88 或 第八十八条")
    parser.add_argument("--any", action="store_true", help="多关键词按 OR 匹配")
    parser.add_argument("--list", action="store_true", help="只按法律名与元数据检索")
    parser.add_argument("--stats", action="store_true", help="打印语料收录统计")
    parser.add_argument(
        "--include-repealed", action="store_true", help="同时检索已修改/已废止的历史版本"
    )
    parser.add_argument(
        "--all-layers", action="store_true", help="全部层都检索（含修改废止决定与未确认状态）"
    )
    parser.add_argument("--hits", type=int, default=3, help="每部法律最多显示几处命中")
    parser.add_argument("--limit-laws", type=int, default=8, help="最多显示几部法律")
    parser.add_argument("--width", type=int, default=160, help="单行输出宽度上限")
    parser.add_argument("--corpus", default=None, help="语料目录（默认技能内 references/corpus）")
    args = parser.parse_args(argv)

    skill_dir = Path(__file__).resolve().parents[1]
    args.base_dir = skill_dir
    if args.all_layers:
        args.layers = ("current", "historical", "decision", "unknown")
    elif args.include_repealed:
        args.layers = ("current", "historical")
    else:
        args.layers = ("current",)
    args.corpus_root = (
        Path(args.corpus).expanduser().resolve()
        if args.corpus
        else skill_dir / "references" / "corpus"
    )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    manifest = load_manifest(args.corpus_root)
    records = select_records(manifest, args)
    if not records:
        print(f"没有匹配的法律（--law / --category 过滤后为空）。已知 {manifest['count']} 部法律。")
        return 1
    if args.stats:
        return run_stats(manifest, records, args)
    if args.list:
        return run_list(records, args.corpus_root, args)
    return run_search(records, args.corpus_root, args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 仅影响控制台输出
        pass
    raise SystemExit(main())
