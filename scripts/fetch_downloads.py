#!/usr/bin/env python3
"""自动抓取国家法律法规数据库的官方 docx，产出与官网「批量下载」等价的 zip。

抓的是本站前端自己调用的同一组公开接口，取回的是官方文件与官方文件名
（`<标题>_<YYYYMMDD>.docx`），后接 `build_from_downloads.py` 即可重建语料。

流程
  1. `build_from_downloads.fetch_official()` 拉官方清单（类目/状态/公布日期/bbbs）
  2. `POST /law-search/download/batch` 批量换取签名下载链接（每批 --batch 条）
  3. GET 签名链接取回 docx 字节
  4. 按「法律语料 / 行政法规 / 司法解释」并入 downloads/<组>/国家法律法规数据库_<组>.zip

用法
  python scripts/fetch_downloads.py --dry-run           # 只统计清单，不下载
  python scripts/fetch_downloads.py --limit 5           # 小样本试跑
  python scripts/fetch_downloads.py                     # 全量抓取（可重复运行）
  python scripts/fetch_downloads.py --groups 行政法规    # 只抓一类

两点提醒
  · 默认跳过已抓过的条目（记在 downloads/.fetch_state.json），加 --force 重抓全部；
  · `--delay` 默认 0.2 秒，别调到接近 0——对站方友好，也不容易被限流。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_from_downloads import CATEGORIES, fetch_official  # noqa: E402
from fetch_corpus import LIMITER, http_get, http_json  # noqa: E402

FLK = "https://flk.npc.gov.cn"
BATCH_API = f"{FLK}/law-search/download/batch"

# 官方类目 codeId → 三个下载目录。分组只决定 zip 落在哪个子目录，
# 具体类目/状态由 build_from_downloads.py 按官方清单重新标注。
GROUPS = {
    "法律语料": {100, 110, 120, 130, 140, 150, 155, 160, 170, 180, 190, 195, 200, 220},
    "行政法规": {210, 215},
    "司法解释": {320, 330, 340, 350},
    # 地方法规约 2.8 万件（地方性法规 23475 + 修改废止决定 1958 + 单行条例 1471
    # + 经济特区法规 800 + 法规性决定 409 + 自治条例 222 + 海南自贸港 54 + 浦东 30），
    # 是国家层面的 11 倍，默认不抓，用 --all 或 --groups 地方法规 显式打开。
    "地方法规": {230, 260, 270, 290, 295, 300, 305, 310},
}
GROUP_ORDER = ["法律语料", "行政法规", "司法解释", "地方法规"]
DEFAULT_GROUPS = ("法律语料", "行政法规", "司法解释")
CODE_TO_GROUP = {code: name for name, codes in GROUPS.items() for code in codes}
LOCAL_GROUPS = ("地方法规",)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "downloads"
STATE_NAME = ".fetch_state.json"
CHECKLIST_NAME = ".official_checklist.json"


# --------------------------------------------------------------------------- #
# 官方接口
# --------------------------------------------------------------------------- #


def group_of(code_id: int | None) -> str:
    return CODE_TO_GROUP.get(code_id, "法律语料")


def official_filename(url: str, url_in: str) -> str | None:
    """从签名链接里取回官方文件名（<标题>_<YYYYMMDD>.docx）。

    公共链接把文件名放在 `response-content-disposition` 里且是二次编码的，
    所以要解两次；内网链接直接带 `fileName=` 参数，作为兜底。
    """
    plain = urllib.parse.unquote(url or "")
    match = re.search(r'filename="([^"]+)"', plain)
    if match:
        return urllib.parse.unquote(match.group(1)).strip()
    inner = urllib.parse.unquote(url_in or "")
    match = re.search(r"[?&]fileName=([^&]+)", inner)
    return urllib.parse.unquote(match.group(1)).strip() if match else None


def request_links(entries: list[dict], timeout: int) -> list[dict]:
    """批量换取下载链接，返回的数组顺序与 entries 一致。"""
    payload = [{"bbbs": meta["bbbs"], "format": "docx"} for meta in entries]
    data = http_json(BATCH_API, payload, timeout=timeout)
    if data.get("code") != 200:
        raise RuntimeError(data.get("msg") or f"接口返回 code={data.get('code')}")
    rows = data.get("data")
    if isinstance(rows, dict):
        rows = [rows]
    return rows or []


# --------------------------------------------------------------------------- #
# 断点续抓
# --------------------------------------------------------------------------- #


def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(f"  ⚠ 断点记录损坏，忽略：{path}")
        return {}


def save_state(path: Path, state: dict[str, str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# 写 zip
# --------------------------------------------------------------------------- #


def merge_zip(zip_path: Path, entries: dict[str, bytes]) -> int:
    """把 {文件名: 字节} 并入 zip（同名覆盖），先写临时文件再原子替换。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[str, bytes] = {}
    if zip_path.exists():
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    merged[info.filename] = zf.read(info)
    merged.update(entries)
    tmp = zip_path.with_suffix(zip_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in merged.items():
            zf.writestr(name, data)
    tmp.replace(zip_path)
    return len(merged)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def load_checklist_cache(path: Path, max_age_days: float = 7.0) -> dict | None:
    """读官方清单缓存；过期或损坏就返回 None（重新抓）。"""
    if not path.exists():
        return None
    age_days = (time.time() - path.stat().st_mtime) / 86400
    if age_days > max_age_days:
        print(f"  清单缓存已过期（{age_days:.1f} 天），重新抓取")
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        official = {(row["title"], row["date"]): row["meta"] for row in raw}
    except (OSError, ValueError, KeyError, TypeError):
        print("  清单缓存损坏，重新抓取")
        return None
    print(f"  复用清单缓存（{age_days:.1f} 天前，{len(official)} 条）：{path.name}")
    return official


def save_checklist_cache(path: Path, official: dict) -> None:
    rows = [{"title": t, "date": d, "meta": m} for (t, d), m in official.items()]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def build_checklist(groups: set[str], cache_path: Path, refresh: bool) -> list[dict]:
    """官方清单 → 待抓列表（只保留有 bbbs 的条目）。"""
    official = None if refresh else load_checklist_cache(cache_path)
    if official is None:
        print("拉取官方清单…")
        official = fetch_official()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            save_checklist_cache(cache_path, official)
        except OSError as exc:
            print(f"  ⚠ 清单缓存写入失败（不影响抓取）：{exc}")
    items = []
    for (title, date), meta in official.items():
        if not meta.get("bbbs"):
            continue
        group = group_of(meta.get("code_id"))
        if group not in groups:
            continue
        items.append(
            {
                "title": title,
                "date": date,
                "bbbs": meta["bbbs"],
                "group": group,
                "category": meta.get("category", ""),
                "status": meta.get("status", ""),
            }
        )
    items.sort(key=lambda item: (GROUP_ORDER.index(item["group"]), item["category"], item["date"]))
    return items


def fetch_one(item: dict, timeout: int, delay: float) -> tuple[str, bytes]:
    """单条兜底：换链接 + 取字节。"""
    rows = request_links([item], timeout=timeout)
    if not rows:
        raise RuntimeError("接口未返回下载链接")
    time.sleep(delay)
    row = rows[0]
    name = official_filename(row.get("url", ""), row.get("urlIn", ""))
    if not name:
        raise RuntimeError("官方文件名解析失败")
    return name, http_get(row["url"], timeout=timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="爬取官方库 docx")
    parser.add_argument("--out", default=None, help=f"输出目录（默认 {DEFAULT_OUT}）")
    parser.add_argument("--groups", default=None, help="只抓指定分组，逗号分隔：" + "/".join(GROUP_ORDER))
    parser.add_argument("--all", action="store_true", help="连地方法规一起抓（约 2.8 万件，耗时数小时、体积上 GB）")
    parser.add_argument("--limit", type=int, default=0, help="最多抓多少条（试跑用，默认全部）")
    parser.add_argument("--batch", type=int, default=50, help="每批向接口提交多少条（默认 50）")
    parser.add_argument("--delay", type=float, default=0.2, help="每次请求后的等待秒数（默认 0.2）")
    parser.add_argument("--min-interval", type=float, default=0.35, help="自适应节流的起始/最快请求间隔（秒）")
    parser.add_argument("--max-interval", type=float, default=120.0, help="被限流时允许放慢到的最大间隔（秒）")
    parser.add_argument("--retry-rounds", type=int, default=2, help="主流程结束后补抓失败条目的轮数（默认 2）")
    parser.add_argument("--retry-pause", type=float, default=120.0, help="两轮补抓之间的等待秒数（默认 120）")
    parser.add_argument("--timeout", type=int, default=60, help="单次请求超时秒数")
    parser.add_argument("--dry-run", action="store_true", help="只统计清单，不下载")
    parser.add_argument("--force", action="store_true", help="忽略断点记录，重抓全部")
    parser.add_argument("--refresh-list", action="store_true", help="忽略清单缓存，重新拉官方清单")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    out_root = Path(args.out).expanduser().resolve() if args.out else DEFAULT_OUT
    groups = set(GROUP_ORDER) if args.all else set(DEFAULT_GROUPS)
    if args.groups:
        groups = {g.strip() for g in args.groups.split(",") if g.strip()}
        unknown = groups - set(GROUP_ORDER)
        if unknown:
            print(f"未知分组：{'、'.join(sorted(unknown))}（可选：{'、'.join(GROUP_ORDER)}）")
            return 2

    # 自适应节流：被 WAF 拦就翻倍退让，连续成功再缓慢收回；间隔学到的值会记在
    # downloads/.rate_state.json，重跑时从那里起步，不重新踩一遍限流的坑。
    LIMITER.min_interval = args.min_interval
    LIMITER.max_interval = args.max_interval
    LIMITER.state_path = out_root / ".rate_state.json"
    LIMITER._load()
    print(f"自适应节流：当前间隔 {LIMITER.interval:.2f}s（区间 {args.min_interval:.2f}–{args.max_interval:.2f}s）")

    items = build_checklist(groups, out_root / CHECKLIST_NAME, args.refresh_list)
    per_group = {name: sum(1 for item in items if item["group"] == name) for name in GROUP_ORDER}
    print()
    for name in GROUP_ORDER:
        if name in groups:
            print(f"  {name:<6} {per_group[name]:>5} 条")
    print(f"  合计   {len(items):>5} 条（含历史版本与修改废止决定）")

    state_path = out_root / STATE_NAME
    state = {} if args.force else load_state(state_path)
    pending = [item for item in items if args.force or item["bbbs"] not in state]
    if args.limit:
        pending = pending[: args.limit]
    skipped = len(items) - len(pending)
    if skipped and not args.limit:
        print(f"  已抓过，跳过 {skipped} 条（--force 可重抓）")

    if args.dry_run:
        if pending:
            rows = request_links(pending[:1], timeout=args.timeout)
            sample = official_filename(rows[0].get("url", ""), rows[0].get("urlIn", "")) if rows else None
            print(f"\n（--dry-run 不下载）示例：{pending[0]['title']} → {sample}")
        return 0

    if not pending:
        print("\n没有需要抓取的条目。")
        return 0

    print(f"\n开始抓取 {len(pending)} 条，输出到 {out_root}")
    buffers: dict[str, dict[str, bytes]] = {name: {} for name in GROUP_ORDER}
    failures: list[tuple[str, dict, str]] = []
    counter = {"done": 0}

    def flush() -> None:
        """每批落盘一次，中途被杀也不丢已抓的。"""
        out_root.mkdir(parents=True, exist_ok=True)
        for group, entries in buffers.items():
            if entries:
                path = out_root / group / f"国家法律法规数据库_{group}.zip"
                total = merge_zip(path, entries)
                buffers[group] = {}
                print(f"  ✓ {group}：本批写入 {len(entries)} 条，zip 累计 {total} 条")
        save_state(state_path, state)

    def process(chunk: list[dict]) -> None:
        try:
            rows = request_links(chunk, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001 - 整批失败就退化成逐条，不中断全程
            print(f"      ⚠ 本批换链接失败（{exc}），改为逐条重试")
            rows = []
        time.sleep(args.delay)

        for index, item in enumerate(chunk):
            row = rows[index] if index < len(rows) else None
            name = official_filename(row.get("url", ""), row.get("urlIn", "")) if row else None
            try:
                if not row or not name:
                    name, data = fetch_one(item, args.timeout, args.delay)
                else:
                    data = http_get(row["url"], timeout=args.timeout)
                    time.sleep(args.delay)
            except Exception as exc:  # noqa: BLE001
                failures.append((f"{item['title']}（{item['date']}）", item, str(exc)))
                continue
            buffers[item["group"]][name] = data
            state[item["bbbs"]] = name
            counter["done"] += 1
            if counter["done"] % 50 == 0:
                print(f"  … 已抓 {counter['done']}/{len(pending)}")
        flush()

    for start in range(0, len(pending), max(1, args.batch)):
        process(pending[start : start + max(1, args.batch)])

    # 失败补抓：限流是间歇性的，隔开一段时间再试能救回大部分
    for round_no in range(1, max(0, args.retry_rounds) + 1):
        if not failures:
            break
        retry_items = [item for _, item, _ in failures]
        failures = []
        print(f"\n第 {round_no} 轮补抓：{len(retry_items)} 条（先等 {args.retry_pause:.0f}s 让限流窗口过去）")
        time.sleep(args.retry_pause)
        for start in range(0, len(retry_items), max(1, args.batch)):
            process(retry_items[start : start + max(1, args.batch)])

    flush()
    print(f"\n完成：成功 {counter['done']} 条，失败 {len(failures)} 条")
    for title, _item, reason in failures[:20]:
        print(f"  ✗ {title}：{reason}")
    if len(failures) > 20:
        print(f"  … 另有 {len(failures) - 20} 条失败（多为官网未提供 docx 的老式文件）")
    print("\n下一步：python scripts/build_from_downloads.py --dry-run 检查配对，再去掉 --dry-run 重建语料。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
