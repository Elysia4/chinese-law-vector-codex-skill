#!/usr/bin/env python3
"""抓取**地方法规的元数据**（正文不入库，需要时再按需抓取）。

为什么只抓元数据：地方性法规有 2.8 万件、正文约 700MB，全量进本地语料会把
仓库和使用门槛一起撑爆。而绝大多数提问只涉及少数几件——所以本地只留一张
"地图"（标题/地域/机关/日期/时效性/官方链接），真正要读正文时再按需抓那几件
（实测一次约 1 秒，见 fetch_downloads.py 的下载链路）。

⚠️ 元数据只有标题，**没有正文**——所以这一层能回答的是"有没有这部法规、谁来定
的、还有效吗、链接在哪"，以及按"地域 + 标题词"定位候选。指望用语义向量直接命中
"押金"这种只出现在正文里的词，是做不到的（官方检索接口本身也只支持标题检索）。

产物
  references/local-regulations.jsonl  每行一件，UTF-8，可直接灌进向量库/关系库

用法
  python scripts/fetch_local_meta.py                 # 全量抓取（8 个类目，约 2.8 万件）
  python scripts/fetch_local_meta.py --limit 500     # 试跑
  python scripts/fetch_local_meta.py --out <path>    # 换输出位置
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch_corpus import LIMITER, http_json  # noqa: E402

FLK = "https://flk.npc.gov.cn"
LIST_API = f"{FLK}/law-search/search/list"
DETAIL_URL = f"{FLK}/detail?id="

# 官方的地方法规类目（枚举接口 enumData 的叶子节点）
LOCAL_CATEGORIES = {
    230: "地方性法规",
    260: "自治条例",
    270: "单行条例",
    290: "经济特区法规",
    295: "浦东新区法规",
    300: "海南自由贸易港法规",
    305: "法规性决定",
    310: "地方法规修改废止决定",
}
# 制定机关 codeId → 省级行政区（同样来自 enumData 的 zdjgfl 子树）
REGION_OF_ISSUER = {
    170: "北京", 180: "天津", 190: "河北", 200: "山西", 210: "内蒙古",
    220: "辽宁", 230: "吉林", 240: "黑龙江", 250: "上海", 260: "江苏",
    270: "浙江", 280: "安徽", 290: "福建", 300: "江西", 310: "山东",
    320: "河南", 330: "湖北", 340: "湖南", 350: "广东", 360: "广西",
    370: "海南", 380: "重庆", 390: "四川", 400: "贵州", 410: "云南",
    420: "西藏", 430: "陕西", 440: "甘肃", 450: "青海", 460: "宁夏",
    470: "新疆",
}
STATUS_NAMES = {1: "已废止", 2: "已修改", 3: "现行有效", 4: "尚未生效", -1: "已失效"}

# 制定机关名 → 属地：去掉机关后缀，剩下的就是行政区名（湘潭市/上海市/巴音郭楞蒙古自治州）
ISSUER_TAIL = re.compile(
    r"(人民代表大会|人民代表大会常务委员会|人大常务委员会|人大常委会|人民政府|"
    r"办公厅|办公室|管理委员会|管理局|委员会|人民政府法制办)+$"
)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "references" / "local-regulations.jsonl"


def fetch_category(code: int, timeout: int) -> list[dict]:
    """抓一个地方法规类目的全部条目（不分时效性）。"""
    rows: list[dict] = []
    seen: set[str] = set()
    total = None
    page = 1
    while page <= 200:
        payload = {
            "searchRange": 1,
            "searchType": 1,
            "searchContent": "",
            "sxrq": [],
            "gbrq": [],
            "gbrqYear": [],
            "sxx": [],
            "flfgCodeId": [code],
            "zdjgCodeId": [],
            "pageNum": page,
            "pageSize": 200,
        }
        data = http_json(LIST_API, payload, timeout=timeout)
        total = data.get("total") or total
        batch = data.get("rows") or []
        if not batch:
            break
        for row in batch:
            key = row.get("bbbs") or ""
            if key and key not in seen:
                seen.add(key)
                rows.append(row)
        if total and len(rows) >= total:
            break
        page += 1
    if total and len(rows) < total:
        print(f"      ⚠ 去重后 {len(rows)}/{total}，官方分页可能有抖动")
    return rows


def issuer_region(name: str) -> str:
    """从制定机关名里提取属地。

    列表接口对市级机关一律返回 zdjgCodeId=9999（省级才给真实码），所以属地只能
    从名字里取。好处是匹配更直接：用户说"湘潭"，拿属地名做子串匹配就命中了。
    """
    text = (name or "").strip()
    for _ in range(3):
        stripped = ISSUER_TAIL.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return text


def to_record(row: dict, category: str) -> dict:
    title = re.sub(r"<[^>]+>", "", row.get("title") or "").strip()
    bbbs = row.get("bbbs") or ""
    issuer = row.get("zdjgName") or ""
    return {
        "bbbs": bbbs,
        "title": title,
        "category": category,
        "region": issuer_region(issuer),
        "province": REGION_OF_ISSUER.get(row.get("zdjgCodeId"), ""),
        "issuer": issuer,
        "promulgated": row.get("gbrq") or "",
        "effective": row.get("sxrq") or "",
        "status": STATUS_NAMES.get(row.get("sxx"), "时效性未标注"),
        "status_code": row.get("sxx"),
        "official_url": DETAIL_URL + bbbs if bbbs else "",
        "text_status": "metadata-only",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取地方法规元数据（不含正文）")
    parser.add_argument("--out", default=None, help=f"输出 JSONL（默认 {DEFAULT_OUT}）")
    parser.add_argument("--limit", type=int, default=0, help="最多抓多少条（试跑）")
    parser.add_argument("--timeout", type=int, default=60, help="单次请求超时秒数")
    parser.add_argument("--min-interval", type=float, default=0.35, help="自适应节流起始间隔（秒）")
    parser.add_argument("--max-interval", type=float, default=120.0, help="被限流时放慢到的上限（秒）")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    out_path = Path(args.out).expanduser().resolve() if args.out else DEFAULT_OUT
    LIMITER.min_interval = args.min_interval
    LIMITER.max_interval = args.max_interval
    LIMITER.state_path = out_path.parent / ".rate_state.json"
    LIMITER._load()
    print(f"自适应节流：当前间隔 {LIMITER.interval:.2f}s（区间 {args.min_interval:.2f}–{args.max_interval:.2f}s）")

    records: list[dict] = []
    started = time.time()
    for code, name in LOCAL_CATEGORIES.items():
        rows = fetch_category(code, args.timeout)
        new = [to_record(r, name) for r in rows if r.get("bbbs")]
        records.extend(new)
        print(f"  {name:<16} {len(new):>6} 件")
        if args.limit and len(records) >= args.limit:
            records = records[: args.limit]
            print(f"      --limit 生效，只保留前 {len(records)} 件")
            break

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    from collections import Counter

    print(f"\n完成：{len(records)} 件，用时 {time.time() - started:.0f}s → {out_path}")
    print(f"  类目: {dict(Counter(r['category'] for r in records))}")
    print(f"  时效性: {dict(Counter(r['status'] for r in records))}")
    print(f"  地域: {dict(Counter(r['region'] or '未标注' for r in records).most_common(6))}")
    print("提醒：这一层只有元数据（标题/机关/日期/链接），正文需要时再按需抓取。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
