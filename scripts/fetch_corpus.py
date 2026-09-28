#!/usr/bin/env python3
"""重建 china-law 技能自带的离线法律语料。

⚠️ 定位变更：本脚本现在主要作为**共享库**被其他脚本 import（HTTP 工具、标题归一化、
官方清单拉取）。它自己构建的是**旧的 307 件法律语料**（正文取自第三方开源汇编、
无分层标记），运行会覆盖 `references/corpus/`。
要构建当前使用的完整语料（2473 件、官方 docx、带分层），请用：
    python scripts/build_from_downloads.py

数据来源
  元数据（权威）：国家法律法规数据库 https://flk.npc.gov.cn 的检索接口
  条文全文　　　：结构化开源法律库 https://github.com/LawRefBook/Laws

产物
  references/corpus/<部门法>/<法律全称>.md  每条法律一个文件，带 YAML 元数据头
  references/corpus/manifest.json           机器可读的收录清单
  references/law-index.md                   按部门法分组的人读目录

用法
  python scripts/fetch_corpus.py                # 宪法 + 现行有效法律（默认）
  python scripts/fetch_corpus.py --scope laws   # 只重建法律
  python scripts/fetch_corpus.py --check        # 只做覆盖率体检，不写文件
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FLK = "https://flk.npc.gov.cn"
LIST_API = f"{FLK}/law-search/search/list"
DETAIL_URL = f"{FLK}/detail?id="

REPO = "LawRefBook/Laws"
BRANCH = "master"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
TREE_API = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"

USER_AGENT = "china-law-skill/1.0 (offline corpus builder)"
INFO_MARK = "<!-- INFO END -->"

CONSTITUTION_CODE = 100
CATEGORIES = [
    (110, "宪法相关法"),
    (120, "民法商法"),
    (130, "行政法"),
    (140, "经济法"),
    (150, "社会法"),
    (155, "生态环境法"),
    (160, "刑法"),
    (170, "诉讼与非诉讼程序法"),
]
CATEGORY_CODES = dict(CATEGORIES)
CATEGORY_ORDER = ["宪法"] + [name for _, name in CATEGORIES]

# 国家法律法规数据库的时效性编码
STATUS_NAMES = {1: "已废止", 2: "已修改", 3: "现行有效", 4: "尚未生效", -1: "已失效"}
CURRENT = 3

# 民法典在源库中按编拆分，需要合并为一个文件
CIVIL_CODE_PARTS = [
    "总则",
    "物权编",
    "合同编",
    "人格权编",
    "婚姻家庭编",
    "继承编",
    "侵权责任编",
    "附则",
]
ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def http_get(url: str, data: bytes | None = None, timeout: int = 60, retries: int = 4) -> bytes:
    """带重试的 GET/POST。仅依赖标准库，避免宿主环境缺少第三方包。"""
    headers = {"User-Agent": USER_AGENT, "Referer": f"{FLK}/"}
    if data is not None:
        headers["Content-Type"] = "application/json;charset=utf-8"
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - 网络错误种类多，统一重试
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败：{url}（{last_error}）")


def http_json(url: str, payload: dict | None = None, timeout: int = 60) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    raw = http_get(url, data=body, timeout=timeout)
    return json.loads(raw.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- #
# 在线兜底检索（只取元数据，不取正文）
# --------------------------------------------------------------------------- #


def _live_request(
    text: str, limit: int, current_only: bool, timeout: int
) -> tuple[list[dict], int]:
    """向官方检索接口发一次单页请求，返回（原始条目, total）。"""
    payload = {
        "searchRange": 1,
        "searchType": 1,
        "searchContent": text,
        "sxrq": [],
        "gbrq": [],
        "gbrqYear": [],
        "sxx": [CURRENT] if current_only else [],   # 空数组＝全部时效性
        "flfgCodeId": [],
        "zdjgCodeId": [],
        "pageNum": 1,
        "pageSize": max(1, min(limit, 50)),
    }
    data = http_json(LIST_API, payload, timeout=timeout)
    rows = data.get("rows") or []
    items: list[dict] = []
    for row in rows:
        title = re.sub(r"<[^>]+>", "", row.get("title") or "")
        if not title:
            continue
        items.append(
            {
                "title": title,
                "status": STATUS_NAMES.get(row.get("sxx"), "时效性未标注"),
                "status_code": row.get("sxx"),
                "promulgated": row.get("gbrq") or "",
                "effective": row.get("sxrq") or "",
                "issuer": row.get("zdjgName") or "",
                "category": row.get("flxz") or "",
                "url": DETAIL_URL + (row.get("bbbs") or "") if row.get("bbbs") else "",
            }
        )
    return items, int(data.get("total") or len(items))


_TITLE_NOISE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")
# 「网络数据安全管理条例」这类名字里的通用尾巴，用来在整串匹配失败时降级成核心短语
_GENERIC_TAIL = re.compile(
    r"(暂行条例|暂行办法|暂行规定|实施细则|实施办法|管理办法|实施意见|若干规定"
    r"|条例|办法|规定|细则|决定|通知|规则|解释|批复|答复|意见)+$"
)


def _norm_title(text: str) -> str:
    return _TITLE_NOISE.sub("", text or "")


def _core_phrase(query: str) -> str:
    """去掉「…管理办法/暂行条例」这类尾巴，得到更有辨识度的核心短语。"""
    core = _norm_title(query)
    for _ in range(3):
        stripped = _GENERIC_TAIL.sub("", core)
        if stripped == core or len(stripped) < 4:
            break
        core = stripped
    return core


def _title_score(title: str, phrase: str, core: str) -> float:
    name = _norm_title(title)
    if phrase and name == phrase:
        return 4.0
    if core and name == core:
        return 3.0
    if core and name.startswith(core):
        return 2.5
    if core and core in name:
        return 2.0
    if phrase and phrase in name:
        return 1.5
    return 0.0


# 同名/近名条目撞在一起时按效力位阶排序：问「民营经济促进法」应该先给国家法律，
# 而不是《陕西省实施〈中华人民共和国民营经济促进法〉办法》
_CATEGORY_RANK = {
    "宪法": 0,
    "法律": 1,
    "法律解释": 2,
    "行政法规": 3,
    "司法解释": 4,
    "有关法律问题和重大问题的决定（部分）": 5,
    "修改、废止的决定": 6,
    "地方法规": 8,
    "法规性决定": 9,
}


def _category_rank(item: dict) -> int:
    return _CATEGORY_RANK.get(item.get("category") or "", 7)


def live_search(
    query: str,
    limit: int = 10,
    current_only: bool = False,
    timeout: int = 30,
) -> tuple[list[dict], int, str]:
    """在官方检索接口里实时查一次，返回（条目列表, 命中数, 说明）。

    两个现实约束（都实测过）：
      1. 接口是按**标题整串**匹配的；整串匹配不上时会直接把整库返回（total 变成全库条数），
         所以必须本地按标题过滤，不能信它的排序。
      2. 用户往往报的是全称（「…暂行办法」），而库里标题可能略有出入，所以整串匹配失败时
         降级为核心短语（去掉「管理办法/暂行条例」这类尾巴）再查一次。

    边界同样重要：**只定位法规、取元数据与官方链接，不取正文**。flk.npc.gov.cn 的
    robots.txt 明确禁止自动化工具采集网站数据，本项目所有正文都来自使用者人工
    "批量下载"导出的 docx；这里单次一页、只读检索接口，正文一律引导打开官方链接。
    """
    phrase = _norm_title(query)
    core = _core_phrase(query)
    attempts = [query] if phrase == core else [query, core]

    pool: dict[tuple[str, str], dict] = {}
    used_core = False
    for text in attempts:
        items, _ = _live_request(text, limit=50, current_only=current_only, timeout=timeout)
        for item in items:
            pool.setdefault((item["title"], item["promulgated"]), item)
        if any(_title_score(i["title"], phrase, core) > 0 for i in items):
            used_core = text != query
            break

    scored = [
        (_title_score(item["title"], phrase, core), item) for item in pool.values()
    ]
    matched = [(score, item) for score, item in scored if score > 0]
    # 依次稳定排序：效力位阶 → 标题长度（越短越贴近查询）→ 公布日期（新的在前）
    matched.sort(key=lambda pair: pair[1]["promulgated"] or "", reverse=True)
    matched.sort(key=lambda pair: len(_norm_title(pair[1]["title"])))
    matched.sort(key=lambda pair: _category_rank(pair[1]))
    matched.sort(key=lambda pair: -pair[0])

    if matched:
        note = f"全称没匹配上，改用核心短语「{core}」匹配" if used_core else ""
        return [item for _, item in matched[:limit]], len(matched), note
    return (
        [],
        0,
        "官方库没有名称含该关键词的条目。可能是名称不同、属部门规章/地方政府规章"
        "（这个接口只覆盖法律、行政法规、地方性法规、司法解释与各类决定），"
        "或该件尚未公开。请到 https://flk.npc.gov.cn 站内检索，或到制定机关官网查询。",
    )


def snapshot_info(corpus_root: Path) -> tuple[str, int]:
    """读本地语料的快照日期与件数（用于判断"是不是很久没更新"）。"""
    try:
        manifest = json.loads((corpus_root / "manifest.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "", 0
    return str(manifest.get("retrieved") or ""), int(manifest.get("count") or 0)


def snapshot_age_days(retrieved: str) -> int | None:
    """快照距今多少天；日期缺失或格式异常时返回 None。"""
    if not retrieved:
        return None
    try:
        return (dt.date.today() - dt.date.fromisoformat(retrieved)).days
    except ValueError:
        return None


STALE_DAYS = 180          # 超过这个天数就把"该更新语料了"提醒出来


def stale_note(corpus_root: Path) -> str:
    """本地快照放太久时的一句话提醒；不需要提醒时返回空串。"""
    retrieved, _ = snapshot_info(corpus_root)
    age = snapshot_age_days(retrieved)
    if age is None or age < STALE_DAYS:
        return ""
    return (
        f"⚠️ 本地语料是 {retrieved} 的快照（距今 {age} 天）。涉及近期立法/修订的问题，"
        f"先用 python scripts/search_live.py <关键词> 到官方库核对最新条目。"
    )


def live_hint(query: str) -> str:
    """本地零命中时的兜底提示（说清它只给条目与链接、不抓正文）。"""
    return (
        "本地语料没有命中。若你要找的是快照之后新立/新修的法，运行\n"
        f'  python scripts/search_live.py "{query}"\n'
        "到官方库实时查一次（只返回条目元数据与官方链接，不抓正文）。"
    )


def format_live_items(items: list[dict], total: int, note: str = "") -> list[str]:
    """把实时检索结果排成人读的多行文本（search_live 与 rank_search 共用）。"""
    if not items:
        return [note] if note else ["官方库没有匹配条目。"]
    lines = [f"官方库实时检索命中 {total} 条，显示前 {len(items)} 条："]
    if note:
        lines.append(f"（{note}）")
    for index, item in enumerate(items, start=1):
        lines.append("")
        lines.append(f"{index}. {item['title']}")
        lines.append(
            "   "
            + "　".join(
                x
                for x in (
                    f"状态：{item['status']}",
                    f"公布：{item['promulgated'] or '—'}",
                    f"施行：{item['effective'] or '—'}",
                    f"制定机关：{item['issuer'] or '—'}",
                    f"类目：{item['category'] or '—'}",
                )
            )
        )
        if item.get("url"):
            lines.append(f"   官方全文：{item['url']}")
    lines.append("")
    lines.append("（以上只是官方库的条目信息与链接，正文请打开官方链接查看。）")
    return lines


# --------------------------------------------------------------------------- #
# 权威清单（国家法律法规数据库）
# --------------------------------------------------------------------------- #


def flk_rows(code: int, statuses: tuple[int, ...] = (CURRENT,)) -> list[dict]:
    """取某个分类下的全部条目，自动翻页。"""
    payload = {
        "searchRange": 1,
        "searchType": 1,
        "searchContent": "",
        "sxrq": [],
        "gbrq": [],
        "gbrqYear": [],
        "sxx": list(statuses),
        "flfgCodeId": [code],
        "zdjgCodeId": [],
    }
    collected: list[dict] = []
    page = 1
    total = None
    while True:
        payload["pageNum"] = page
        payload["pageSize"] = 200
        data = http_json(LIST_API, payload)
        batch = data.get("rows") or []
        if total is None:
            total = int(data.get("total") or 0)
        collected.extend(batch)
        if not batch or len(collected) >= (total or 0):
            break
        page += 1
    return collected[: total or len(collected)]


def is_constitution_row(title: str) -> bool:
    """宪法只保留现行修正文本与历次修正案，跳过被取代的原始文本。"""
    return "修正文本" in title or "修正案" in title


def build_checklist(scope: str) -> list[dict]:
    items: list[dict] = []
    if scope in ("all", "constitution"):
        for row in flk_rows(CONSTITUTION_CODE):
            title = clean_title(row["title"])
            if not is_constitution_row(title):
                continue
            items.append(make_item(row, title, "宪法"))
    if scope in ("all", "laws"):
        for code, category in CATEGORIES:
            for row in flk_rows(code):
                items.append(make_item(row, clean_title(row["title"]), category))
    return items


def make_item(row: dict, title: str, category: str) -> dict:
    status = STATUS_NAMES.get(row.get("sxx"), f"未知({row.get('sxx')})")
    return {
        "title": title,
        "category": category,
        "promulgated": (row.get("gbrq") or "").strip(),
        "effective": (row.get("sxrq") or "").strip(),
        "status": status,
        "issuer": (row.get("zdjgName") or "").strip(),
        "code_id": row.get("flfgCodeId"),
        "bbbs": row.get("bbbs"),
        "official_url": DETAIL_URL + (row.get("bbbs") or ""),
    }


def clean_title(title: str) -> str:
    title = re.sub(r"<[^>]+>", "", title or "")
    return title.strip()


def norm_title(title: str) -> str:
    title = clean_title(title)
    title = re.sub(r"^中华人民共和国", "", title)
    title = re.sub(r"[（(][^（()）]*[)）]", "", title)
    return title.strip()


# --------------------------------------------------------------------------- #
# 全文来源（结构化开源库）
# --------------------------------------------------------------------------- #


def repo_tree() -> list[dict]:
    data = http_json(TREE_API, timeout=120)
    if "tree" not in data:
        raise RuntimeError(f"无法读取源库目录：{str(data)[:200]}")
    return data["tree"]


def repo_index(tree: list[dict]) -> dict[str, list[dict]]:
    """把源库文件按规范化法律名建索引，记录文件名中的日期和年份。"""
    wanted_dirs = set(CATEGORY_CODES.values()) | {"宪法", "民法典"}
    index: dict[str, list[dict]] = {}
    for node in tree:
        if node.get("type") != "blob" or not node["path"].endswith(".md"):
            continue
        parts = node["path"].split("/")
        if len(parts) != 2 or parts[0] not in wanted_dirs or parts[1].startswith("_"):
            continue
        stem = parts[1][:-3]
        date = year = None
        match = re.search(r"\((\d{4}-\d{2}-\d{2})\)$", stem)
        if match:
            date = match.group(1)
            stem = stem[: match.start()].strip()
        else:
            year_match = re.search(r"[（(](\d{4})年[)）]", stem)
            if year_match:
                year = year_match.group(1)
        index.setdefault(norm_title(stem), []).append(
            {"date": date, "year": year, "path": node["path"], "size": node.get("size") or 0}
        )
    return index


def pick_source(candidates: list[dict] | None, promulgated: str) -> dict | None:
    """优先取公布日期完全一致的版本，其次同年版本，再退化为最新版本。"""
    if not candidates:
        return None
    exact = [c for c in candidates if c["date"] and c["date"] == promulgated]
    if exact:
        return exact[0]
    same_year = [c for c in candidates if c["year"] and c["year"] == promulgated[:4]]
    if same_year:
        return same_year[0]
    dated = [c for c in candidates if c["date"]]
    if dated:
        return max(dated, key=lambda c: c["date"])
    return candidates[0]


def fetch_text(repo_path: str) -> str:
    url = RAW_BASE + urllib.parse.quote(repo_path)
    return http_get(url, timeout=120).decode("utf-8", "replace")


def split_info_block(text: str) -> tuple[str, str]:
    """拆出源文件的沿革说明（INFO 区）和正文。"""
    if INFO_MARK in text:
        head, body = text.split(INFO_MARK, 1)
        return head.strip(), body.strip()
    return "", text.strip()


def strip_leading_heading(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def merge_civil_code(index: dict[str, list[dict]]) -> str | None:
    """把民法典各编合并成单一全文。"""
    chunks: list[str] = []
    for part in CIVIL_CODE_PARTS:
        candidates = index.get(part)
        if not candidates:
            return None
        raw = fetch_text(candidates[0]["path"])
        _, body = split_info_block(raw)
        chunks.append(f"## {part}\n\n{strip_leading_heading(body)}")
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------- #
# 写出语料
# --------------------------------------------------------------------------- #


def safe_filename(title: str) -> str:
    return ILLEGAL_FILENAME_CHARS.sub("_", title).strip() + ".md"


def yaml_value(value: str) -> str:
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_law(item: dict, body: str | None, source_path: str | None, retrieved: str) -> str:
    lines = [
        "---",
        f"title: {yaml_value(item['title'])}",
        f"category: {yaml_value(item['category'])}",
        f"promulgated: {yaml_value(item['promulgated'] or '未知')}",
        f"effective: {yaml_value(item['effective'] or '未知')}",
        f"status: {yaml_value(item['status'])}",
        f"issuer: {yaml_value(item['issuer'] or '未知')}",
        f"official_url: {yaml_value(item['official_url'])}",
        f"text_source: {yaml_value(source_path or '未收录全文')}",
        f"retrieved: {yaml_value(retrieved)}",
        "---",
        "",
        f"# {item['title']}",
        "",
        (
            f"> 公布：{item['promulgated'] or '未知'}　施行：{item['effective'] or '未知'}"
            f"　时效性：{item['status']}　制定机关：{item['issuer'] or '未知'}"
        ),
        f"> 官方全文：{item['official_url']}",
        "",
    ]
    if body:
        lines.append(body)
    else:
        lines.append(
            "> ⚠️ 本地语料未收录该法律的条文全文。请通过上方官方链接核对官方文本，不要凭记忆引用条文。"
        )
    lines.append("")
    return "\n".join(lines)


def write_index(records: list[dict], retrieved: str) -> str:
    full = [r for r in records if r["text_status"] == "full"]
    lines = [
        "# 中华人民共和国现行有效法律目录",
        "",
        f"- 快照时间：{retrieved}",
        "- 元数据来源：国家法律法规数据库 https://flk.npc.gov.cn",
        "- 条文来源：结构化开源法律库 https://github.com/LawRefBook/Laws",
        f"- 收录：{len(records)} 件（全文 {len(full)} 件，仅元数据 {len(records) - len(full)} 件）",
        "",
        "检索优先用 `scripts/search_corpus.py`；本目录用于浏览与确认收录情况。",
        "",
    ]
    for category in CATEGORY_ORDER:
        group = sorted(
            (r for r in records if r["category"] == category), key=lambda r: r["title"]
        )
        if not group:
            continue
        lines += [f"## {category}（{len(group)}）", "", "| 法律 | 公布 | 施行 | 时效性 | 全文 |", "| --- | --- | --- | --- | --- |"]
        for record in group:
            if record["text_status"] == "full":
                link = f"[全文]({record['file']})"
            else:
                link = f"[仅元数据]({record['official_url']})"
            lines.append(
                f"| {record['title']} | {record['promulgated'] or '—'} | "
                f"{record['effective'] or '—'} | {record['status']} | {link} |"
            )
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重建 china-law 技能的离线法律语料")
    parser.add_argument(
        "--skill-dir",
        default=None,
        help="技能目录（默认取本脚本的上一级目录）",
    )
    parser.add_argument(
        "--scope",
        choices=("all", "laws", "constitution"),
        default="all",
        help="抓取范围：all=宪法+法律（默认），laws=仅法律，constitution=仅宪法",
    )
    parser.add_argument("--jobs", type=int, default=6, help="并发下载数（默认 6）")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比对权威清单与本地语料的覆盖率，不写任何文件",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    skill_dir = (
        Path(args.skill_dir).expanduser().resolve()
        if args.skill_dir
        else Path(__file__).resolve().parents[1]
    )
    corpus_root = skill_dir / "references" / "corpus"
    retrieved = dt.date.today().isoformat()

    print(f"[1/4] 读取权威清单（{args.scope}）…")
    items = build_checklist(args.scope)
    if not items:
        print("权威清单为空，终止。")
        return 1
    print(f"      权威清单：{len(items)} 件")

    print("[2/4] 读取全文来源目录…")
    index = repo_index(repo_tree())

    print("[3/4] 匹配全文…")
    plan: list[tuple[dict, dict | None]] = []
    for item in items:
        source = None
        if norm_title(item["title"]) == "民法典":
            if all(part in index for part in CIVIL_CODE_PARTS):
                source = {"path": "民法典/（合并各编）", "merged": True}
        if source is None:
            source = pick_source(index.get(norm_title(item["title"])), item["promulgated"])
        plan.append((item, source))

    matched = [p for p in plan if p[1]]
    missing = [p[0] for p in plan if not p[1]]
    print(f"      匹配到全文：{len(matched)} 件；仅元数据：{len(missing)} 件")
    for item in missing:
        print(f"        - {item['title']}（{item['category']}，公布 {item['promulgated'] or '—'}）")

    if args.check:
        print("[4/4] --check 模式：未写入任何文件。")
        return 0

    print(f"[4/4] 下载并写出语料 → {corpus_root}")

    def prepare(entry: tuple[dict, dict | None]) -> dict:
        item, source = entry
        body = None
        source_path = None
        if source:
            if source.get("merged"):
                body = merge_civil_code(index)
                if body:
                    source_path = source["path"]
            else:
                raw = fetch_text(source["path"])
                _, text = split_info_block(raw)
                body = strip_leading_heading(text)
                source_path = source["path"]
        record = dict(item)
        record["text_status"] = "full" if body else "metadata-only"
        record["chars"] = len(body or "")
        record["source_path"] = source_path or ""
        record["file"] = str(
            Path("corpus") / item["category"] / safe_filename(item["title"])
        ).replace("\\", "/")
        record["_body"] = body
        return record

    records: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for count, record in enumerate(pool.map(prepare, plan), start=1):
            records.append(record)
            if count % 25 == 0 or count == len(plan):
                print(f"      进度 {count}/{len(plan)}")

    for record in records:
        target = corpus_root / record["file"].removeprefix("corpus/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            render_law(record, record.pop("_body"), record["source_path"] or None, retrieved),
            encoding="utf-8",
        )

    records.sort(key=lambda r: (CATEGORY_ORDER.index(r["category"]), r["title"]))
    manifest = {
        "generated_by": "scripts/fetch_corpus.py",
        "retrieved": retrieved,
        "metadata_source": f"{FLK}",
        "text_source": f"https://github.com/{REPO}",
        "scope": args.scope,
        "count": len(records),
        "full_text_count": sum(1 for r in records if r["text_status"] == "full"),
        "laws": records,
    }
    corpus_root.mkdir(parents=True, exist_ok=True)
    (corpus_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (skill_dir / "references" / "law-index.md").write_text(
        write_index(records, retrieved), encoding="utf-8"
    )

    print(
        f"完成：{len(records)} 件法律（全文 {manifest['full_text_count']} 件，"
        f"仅元数据 {len(records) - manifest['full_text_count']} 件）"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 仅影响控制台输出
        pass
    raise SystemExit(main())
