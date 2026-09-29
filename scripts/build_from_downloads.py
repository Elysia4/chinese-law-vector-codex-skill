#!/usr/bin/env python3
"""从官方 docx 构建语料（来源：国家法律法规数据库导出的 docx）。

正文怎么来：默认用 `scripts/fetch_downloads.py` 自动抓取官方 docx（走官网公开
接口，取回的就是官网"批量下载"给出的那批文件与文件名）；也支持把人工用"批量
下载"导出的 zip 放进 downloads/。两条路径产物一致，本脚本只做本地解析与结构化。

输入（--source 可重复指定；默认取技能目录下的 downloads/ 三个子目录）
  downloads/法律语料、downloads/行政法规、downloads/司法解释
  目录里是官方 docx 的 zip（由 scripts/fetch_downloads.py 爬取），内含若干 <标题>_<YYYYMMDD>.docx

处理流程
  1. 解包所有 zip，读出每个文件的字节
  2. 用（标题 + 公布日期）匹配官方清单（含全部状态）→ 得到类目/状态/官方链接
  3. 分层：current（现行有效+尚未生效）/ historical（已修改+已废止）/ decision（修改决定）/ unknown
  4. 正文优先级：docx 原生解析 → 老式 .doc 用开源库补 → 都没有则只留元数据
  5. 输出 references/corpus/<类目>/<标题>(YYYY-MM-DD).md + manifest.json + law-index.md

用法
  python scripts/build_from_downloads.py --dry-run          # 只统计，不写文件
  python scripts/build_from_downloads.py --limit 10         # 小样本试跑
  python scripts/build_from_downloads.py                    # 全量构建
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_corpus import http_get, http_json, norm_title  # noqa: E402  复用带重试的 HTTP 层

FLK = "https://flk.npc.gov.cn"
LIST_API = f"{FLK}/law-search/search/list"
DETAIL_URL = f"{FLK}/detail?id="
REPO_RAW = "https://raw.githubusercontent.com/LawRefBook/Laws/master/"
REPO_TREE = "https://api.github.com/repos/LawRefBook/Laws/git/trees/master?recursive=1"

DEFAULT_SOURCES = [
    "downloads/行政法规",
    "downloads/法律语料",
    "downloads/司法解释",
]

# 类目定义：codeId → 目录名（顺序即目录展示顺序）
CATEGORIES = [
    (100, "宪法"),
    (110, "宪法相关法"),
    (120, "民法商法"),
    (130, "行政法"),
    (140, "经济法"),
    (150, "社会法"),
    (155, "生态环境法"),
    (160, "刑法"),
    (170, "诉讼与非诉讼程序法"),
    (180, "法律解释"),
    (190, "有关法律问题的决定"),
    (195, "宪法修正案"),
    (200, "法律修改废止决定"),
    (210, "行政法规"),
    (215, "行政法规修改废止决定"),
    (220, "监察法规"),
    (320, "高法司法解释"),
    (330, "高检司法解释"),
    (340, "联合司法解释"),
    (350, "司法解释修改废止决定"),
]
CATEGORY_ORDER = [name for _, name in CATEGORIES]
# -1 是官方接口对「已失效」的取值（实测出现在有关法律问题的决定类目里）
STATUS_NAME = {1: "已废止", 2: "已修改", 3: "现行有效", 4: "尚未生效", -1: "已失效（官方标注）"}
LAYER_OF_STATUS = {1: "historical", 2: "historical", 3: "current", 4: "current", -1: "historical"}
LAYER_NAME = {
    "current": "现行有效/尚未生效（默认检索）",
    "historical": "已修改/已废止（历史版本，需显式开启）",
    "decision": "修改/废止决定及官方未标时效性的决定（默认不检索）",
    "unknown": "状态未确认",
}
DECISION_PAT = re.compile(r"关于(修改|废止|修改和废止)|修改、废止的决定|^.*关于修改《")
ILLEGAL = re.compile(r'[\\/:*?"<>|]')
# 官方在标题/文件名尾部给已失效的件加「（失效）」。配对是拿"标题+公布日期"做键的，
# 所以两边必须用同一套规范化：文件名侧由 split_name() 剥掉，官方标题侧在
# fetch_official() 里剥掉，否则这些件永远配不上（实测 58 件全部落空）。
VOID_PAT = re.compile(r"[（(](失效|已失效)[)）]\s*$")
# Windows 资源管理器的 zip 处理器在条目路径超过约 260 字节时会把整个包判为无效
# （实测 258 字节可读、270 字节打不开）。这里留余量，按 UTF-8 字节数截断文件名。
PACKAGE_ENTRY_PREFIX = "china-law/references/corpus/"
ENTRY_PATH_CAP = 255


def safe_stem(title: str, date: str | None, category: str) -> str:
    """生成长度可控的文件名：超长标题截断 + 短哈希保唯一（完整标题保留在元数据里）。"""
    base = f"{ILLEGAL.sub('_', title)}({date or '无日期'})"
    prefix_bytes = len((PACKAGE_ENTRY_PREFIX + category + "/").encode("utf-8"))
    budget = ENTRY_PATH_CAP - prefix_bytes - len(".md")
    if budget < 24 or len(base.encode("utf-8")) <= budget:
        return base
    digest = f"{zlib.crc32(base.encode('utf-8')):08x}"[:6]
    keep = max(6, budget - len(f"-{digest}"))
    out = ""
    for ch in base:
        if len((out + ch).encode("utf-8")) > keep:
            break
        out += ch
    return f"{out.rstrip('_')}-{digest}"


# --------------------------------------------------------------------------- #
# 官方清单（全部状态）
# --------------------------------------------------------------------------- #


def _fetch_category(code: int) -> tuple[list[dict], int | None]:
    """抓一个类目的全部条目（不分状态），返回（行, total）。"""
    rows: list[dict] = []
    total: int | None = None
    page, empty_retry = 1, 0
    while page <= 30:
        payload = {
            "searchRange": 1,
            "searchType": 1,
            "searchContent": "",
            "sxrq": [],
            "gbrq": [],
            "gbrqYear": [],
            "sxx": [],  # 空数组＝全部状态
            "flfgCodeId": [code],
            "zdjgCodeId": [],
            "pageNum": page,
            "pageSize": 200,
        }
        data = None
        for attempt in range(4):
            try:
                data = http_json(LIST_API, payload)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"      ⚠ 第 {page} 页请求失败（第 {attempt + 1} 次）：{exc}")
                time.sleep(1.5 * (attempt + 1))
        if data is None:
            break
        total = data.get("total") or total
        batch = data.get("rows") or []
        if not batch:
            if total and len(rows) < total and empty_retry < 3:
                empty_retry += 1
                time.sleep(1.0)
                continue
            break
        empty_retry = 0
        rows.extend(batch)
        if total and len(rows) >= total:
            break
        page += 1
    return rows, total


def fetch_official() -> dict[tuple[str, str], dict]:
    """返回 {(标题, 公布日期YYYYMMDD): 官方条目}，覆盖全部状态。

    必须防官方接口的分页抖动：它的排序不稳定，同一份数据两次抓取会出现「第 3 页
    与第 4 页重叠、另一条被挤掉」——实测连抓三次「行政法规」，后两次都丢了
    《麻醉药品和精神药品管理条例(20160206)》（行数仍是 833，所以只看行数发现不了）。
    后果是两次构建的产出不一致、个别法规状态被降级。

    对策：每个类目至少抓两次，按（标题+公布日期）取并集；去重后数量仍不足 total
    就继续补抓，最后仍不足会显式告警。分页抖动只影响超过一页的类目（行政法规、
    高法司法解释），其余类目一次即可。
    """
    out: dict[tuple[str, str], dict] = {}
    for code, name in CATEGORIES:
        merged: dict[tuple[str, str], dict] = {}
        total: int | None = None
        for attempt in range(4):
            rows, total = _fetch_category(code)
            for row in rows:
                title = re.sub(r"<[^>]+>", "", row.get("title") or "")
                title = VOID_PAT.sub("", title).strip()
                date = (row.get("gbrq") or "").replace("-", "")
                # 不能因为缺公布日期就丢掉：「有关法律问题的决定」里有 81 件
                # （全国人大及其常委会的决定、决议）官方就是没有 gbrq 字段，
                # 官网给它们的文件名是「标题_.docx」。丢掉的话既抓不到、
                # 也永远配不上（旧语料里它们只能以"状态未确认"混着）。
                if title:
                    merged.setdefault((title, date), row)
            # 单页类目（total ≤ pageSize）不存在分页抖动，抓到齐就收工；
            # 多页类目至少抓两次：单次抓取可能因为分页重叠而缺条目。
            if total and len(merged) >= total and (attempt >= 1 or total <= 200):
                break
            if total and len(merged) < total:
                print(f"      类目 {name}：第 {attempt + 1} 次去重后 {len(merged)}/{total}，补抓…")
        if total and len(merged) < total:
            print(f"      ⚠ 类目 {name} 去重后仍缺 {total - len(merged)} 条，官方接口可能异常")
        for (title, date), row in merged.items():
            out[(title, date)] = {
                "category": name,
                "status_code": row.get("sxx"),
                "status": STATUS_NAME.get(row.get("sxx"), "未知"),
                # 有些件官方就是没有公布日期（「有关法律问题的决定」81 件），
                # 统一成空串，避免下游拿到 None 后 .replace() 崩掉。
                "promulgated": row.get("gbrq") or "",
                "effective": row.get("sxrq"),
                "issuer": row.get("zdjgName") or "",
                "bbbs": row.get("bbbs") or "",
                "code_id": code,
            }
        print(f"      类目 {name:<16} {len(merged):>5} 条（含历史版本）")
    return out


def official_by_title(official: dict) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = defaultdict(list)
    for (title, date), meta in official.items():
        idx[title].append({**meta, "date": date})
    return idx


# --------------------------------------------------------------------------- #
# 读取下载的 zip
# --------------------------------------------------------------------------- #


def read_downloads(sources: list[str]) -> list[dict]:
    """读出所有 zip 里的文件，返回 [{name, ext, bytes, zip, source}]。"""
    files = []
    for src in sources:
        root = Path(src)
        if not root.exists():
            print(f"  ⚠ 跳过不存在的目录：{src}")
            continue
        zips = sorted(root.glob("*.zip"))
        print(f"  {root.name}: {len(zips)} 个 zip")
        for zp in zips:
            with zipfile.ZipFile(zp) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    ext = Path(info.filename).suffix.lower()
                    files.append(
                        {
                            "name": info.filename,
                            "ext": ext,
                            "bytes": zf.read(info),
                            "zip": zp.name,
                            "source": root.name,
                        }
                    )
    return files


def split_name(filename: str) -> tuple[str, str | None, bool]:
    """把 标题_YYYYMMDD.docx 拆成（标题, 日期, 是否已失效）。

    文件名里的 + 是官网上「、」的替换；官方批量下载会给已失效的件在标题后加
    「（失效）」（实测 48 件），这个标记不能留在标题里（引用时会出现
    《…决定（失效）》这种怪写法），但必须转成状态、剥离默认检索层。
    """
    stem = Path(filename).stem
    m = re.match(r"^(.*)_(\d{8})$", stem)
    if m:
        title, date = m.group(1), m.group(2)
    else:
        title, date = stem, None
    title = title.replace("+", "、")
    title = re.sub(r"、{2,}", "、", title)          # ++ 回退后可能留下重复顿号
    title = title.strip().strip("、_ 　")            # 去掉结尾多余的 _ 和空白
    voided = bool(VOID_PAT.search(title))
    if voided:
        title = VOID_PAT.sub("", title).strip()
    return title, date, voided


# --------------------------------------------------------------------------- #
# docx / pdf 解析
# --------------------------------------------------------------------------- #


# 按文档顺序匹配 文本 / 段内换行 / 制表符
DOCX_TOKEN = re.compile(r"<w:t(?: [^>]*)?>(.*?)</w:t>|<w:br\s*/>|<w:tab\s*/>", re.S)
# 官方文档里偶有零宽字符（实测 10 件），落在条文中间会污染检索与引用
INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)


def docx_paragraphs(data: bytes) -> list[str]:
    """从 docx 抽取段落文本。

    两个必须守住的细节（都踩过）：
    1. `<w:t>` 要严格匹配，写成 `<w:t[^>]*>` 会误吞 `<w:tabs>` 等标签，把 XML 属性
       泄漏进正文。
    2. 必须**按文档顺序**处理 `<w:t>` 与 `<w:br/>`。很多公文用段内换行分隔条文——
       实测《飞行基本规则》把 121 条塞进 33 个 `<w:p>`；若把换行统一追加到段尾，
       多条会粘成一行、条文边界丢失，按条检索直接失效（实测影响 641 个文件）。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", "ignore")
    paras: list[str] = []
    for block in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        buf: list[str] = []
        for match in DOCX_TOKEN.finditer(block):
            token = match.group(0)
            if token.startswith("<w:t"):
                buf.append(match.group(1) or "")
            elif token.startswith("<w:br"):
                buf.append("\n")
            else:
                buf.append("\t")
        text = "".join(buf)
        for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
            text = text.replace(a, b)
        text = text.translate(INVISIBLE)
        for part in text.split("\n"):
            part = part.replace("\u3000", " ").strip()
            if part:
                paras.append(part)
    return paras


def pdf_paragraphs(data: bytes) -> list[str]:
    """用自带的 pdfplumber 抽取 PDF 文本（仅用于少数只有老式 .doc 的条目）。"""
    import pdfplumber

    lines = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                line = line.replace("\u3000", " ").strip()
                if line:
                    lines.append(line)
    return lines


HEAD_PATTERNS = [
    (re.compile(r"法释〔\d{4}〕\d+号"), "doc_number"),
    (re.compile(r"中华人民共和国(国务院令|主席令)第\s*\d+\s*号"), "doc_number"),
    (re.compile(r"^（.*?(通过|修正|修订|发布).*?）$"), "history"),
]
# 注意：docx 里偶尔出现「第二百七十一 条」这种条号内混入空格的情况
# （实测 2473 件里出现 1 件、共 2 处）。正则必须容忍，否则该条会与上一条粘连，
# 导致条文边界丢失、按条检索失效。
ARTICLE_PAT = re.compile(r"^第[零一二三四五六七八九十一百千万两]+\s*条")
# 「第一分编 通则」这类分编标题必须一起认，否则目录块会在分编处断开（民法典、生态环境法典）
CHAPTER_PAT = re.compile(r"^(第[零一二三四五六七八九十一百千万两]+\s*(?:分编|[编章节篇]))[\s　]*(.*)$")
TOC_PAT = re.compile(r"^目\s*录$")
# 公文标题偶尔被拆成两段，例如「…关于惩治虚开、」+「伪造和…犯罪的决定」
TITLE_TAIL = re.compile(
    r"(法|条例|规则|规定|办法|解释|决议|决定|细则|准则|标准|章程|纲要|计划|方案"
    r"|通知|答复|批复|意见|报告|名单|目录|文本|修正案|法典|基本法)[)）]?$"
)
# 司法解释/检察规则的文件头：「公告」「最高人民法院」「2013年11月18日」「高检发释字〔2013〕3号」…
HEADER_ONLY = re.compile(
    r"^(公\s*[告吿]$"
    r"|中华人民共和国(?:最高人民法院|最高人民检察院|国务院|主席)$"
    r"|最高人民法院$|最高人民检察院$"
    r"|\d{4}年\d{1,2}月\d{1,2}日$"
    r"|第[一二三四五六七八九十百\d]+号$"
    r"|法释〔\d{4}〕\d+号$|高检发释字〔\d{4}〕\d+号$"
    r"|《[^》]*》已(于|由|经|在))"
)
# 文号必须**整段就是文号**才算文件头。早先用 search() 判断，正文里引用
# 「（法释〔2015〕11号）作如下修改：」的段落会被当成文号整段丢掉
# （实测《最高人民法院关于修改…的决定》只剩最后一句）。
DOC_NUMBER_HEAD = re.compile(
    r"^(法释〔\d{4}〕\d+号"
    r"|高检发释字〔\d{4}〕\d+号"
    r"|中华人民共和国(?:国务院令|主席令)第\s*[一二三四五六七八九十百\d]+\s*号)$"
)
DOC_NUMBER_IN_PAREN = re.compile(r"中华人民共和国(?:国务院令|主席令)第\s*\d+\s*号")
# 官方文本里日期两种写法都有：阿拉伯数字（2019年7月1日）和中文数字（一九九三年一月一日），
# 动词也有「自/于」两种（实测《铁路交通事故应急救援和调查处理条例》写「本条例于2007年9月1日起施行」）
_CN_DIGITS = {"〇": "0", "○": "0", "零": "0", "一": "1", "二": "2", "两": "2", "三": "3",
              "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
_CN_NUM = "0-9〇○零一二三四五六七八九十"
EFFECTIVE_PAT = re.compile(
    rf"(?:自|于)?\s*([{_CN_NUM}]{{2,4}})\s*年\s*([{_CN_NUM}]{{1,3}})\s*月\s*([{_CN_NUM}]{{1,3}})\s*日起施行"
)
EFFECTIVE_FROM_PUB = re.compile(r"自(公布|发布)之日起施行")


def _to_int(text: str) -> int | None:
    """把「1993」「一九九三」「二十」这类数字转成 int。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    translated = "".join(_CN_DIGITS.get(ch, ch) for ch in text)
    if translated.isdigit():
        return int(translated)
    simple = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if "十" in text and set(text) <= set(simple) | {"十"}:
        head, _, tail = text.partition("十")
        return (simple.get(head, 1) if head else 1) * 10 + (simple.get(tail, 0) if tail else 0)
    return simple.get(text)


def backfill_effective(paras: list[str] | None, promulgated: str) -> str:
    """官方清单没给施行日期时，从正文补。

    官方接口对 399 件行政法规的 sxrq 返回 null（实测《政府投资条例》2019-04-14
    公布、2019-07-01 施行，接口里 sxrq 为空），但正文附则通常写明施行日期。
    取最后一次出现——附则那句才是本文的施行日期。
    """
    if not paras:
        return ""
    text = "\n".join(paras)
    for m in reversed(list(EFFECTIVE_PAT.finditer(text))):
        year, month, day = (_to_int(m.group(1)), _to_int(m.group(2)), _to_int(m.group(3)))
        if year and month and day:
            return f"{year:04d}-{month:02d}-{day:02d}"
    if promulgated and EFFECTIVE_FROM_PUB.search(text):
        return promulgated
    return ""


def _norm_line(text: str) -> str:
    """比较标题时忽略所有空白（全角/半角空格、换行）。"""
    return re.sub(r"[\s\u3000]+", "", text)


def _opens_paren(text: str) -> bool:
    """括号有全角「（」也有半角「(」（实测《环境保护税法实施条例》用半角）。"""
    return bool(text) and text[0] in "（("


def _closes_paren(text: str) -> bool:
    return bool(text) and text.rstrip()[-1] in "）)"


# 沿革行的特征：括号紧跟日期（「(1990年4月4日…通过)」「（一九九三年一月一日…」）。
# 不能只判「以括号开头」：实测《保护海底电缆规定》正文以「(一) 保护海底电缆是…」
# 开头、又没有配对闭合括号，只看开头会把整篇正文当成未闭合的沿革行丢掉。
HISTORY_HEAD = re.compile(r"^[（(]\s*(?:\d{4}|[〇○零一二三四五六七八九两]{2,4})\s*年")
# 正文首行也可能是「（一）」「（1）」这类序号，属正文
PAREN_MARKER = re.compile(r"^[（(][^）)]{0,3}[）)]")


def _is_history_line(text: str) -> bool:
    """判断是不是「（…通过/公布…）」这类沿革行。"""
    if not _opens_paren(text) or PAREN_MARKER.match(text):
        return False
    return bool(HISTORY_HEAD.match(text)) or bool(HEAD_PATTERNS[2][0].match(text))


def _is_title_continuation(paras: list[str], i: int) -> bool:
    """判断 paras[i] 是否是 paras[i-1] 那段被拆开的标题的续行。"""
    if i <= 0 or i >= len(paras):
        return False
    return bool(TITLE_TAIL.search(paras[i])) and not TITLE_TAIL.search(paras[i - 1])


def strip_toc(paras: list[str], start: int) -> int:
    """返回正文起点：跳过卷首目录。

    目录有两种形态，都要处理：
      1. 带「目录」标记（2473 件里 422 件有）；
      2. 没有标记，文件头之后直接罗列章节标题（如《人民检察院民事诉讼监督规则（试行）》）。

    目录条目不止「第一章 …」一种，还有「一、管辖」「附件一 …」这类不匹配章节正则的行，
    所以判据是：章节标题，或（有「目录」标记时的）25 字以内短行；碰到「序言/前言/引言」
    小标题、长段落或条文，就认为目录结束、正文开始。

    正文起点取两个候选里最早的：
      · 目录块结束后的第一个正文段落（编号型目录、带序言的文件）
      · 目录第一行标题在正文里的第二次出现（法典类目录末尾与正文首个标题粘连时用它，
        否则会把正文开头的「第一编/第一章」标题当成目录吃掉）
    """
    if start >= len(paras):
        return start
    has_marker = bool(TOC_PAT.match(paras[start]))
    if has_marker:
        toc_start = start + 1
    else:
        j = start
        while j < len(paras) and CHAPTER_PAT.match(paras[j]):
            j += 1
        # 正文里的 编/分编/章/节 嵌套最长实测 3 行，连续 5 行以上才算无标记目录
        if j - start < 5:
            return start
        toc_start = start

    k = toc_start
    while k < len(paras):
        line = paras[k]
        if _norm_line(line) in {"序言", "前言", "引言"}:
            # 「序言」既可能是目录的第一条，也可能是正文的标题：看下一段——
            # 后面还是章节标题/目录短行，说明这是目录里的那一条；后面直接是长段落，
            # 才是正文的序言标题（宪法、香港/澳门基本法、民族团结进步促进法…）。
            nxt = paras[k + 1] if k + 1 < len(paras) else ""
            if CHAPTER_PAT.match(nxt) or (
                has_marker and nxt and len(nxt) <= 25 and not ARTICLE_PAT.match(nxt)
            ):
                k += 1
                continue
            break
        if CHAPTER_PAT.match(line):
            k += 1
            continue
        if has_marker and len(line) <= 25 and not ARTICLE_PAT.match(line):
            k += 1
            continue
        break
    toc_end = k

    first_title = next((idx for idx in range(toc_start, toc_end) if CHAPTER_PAT.match(paras[idx])), None)
    repeat = None
    if first_title is not None:
        for idx in range(first_title + 1, len(paras)):
            if _norm_line(paras[idx]) == _norm_line(paras[first_title]):
                repeat = idx
                break
    first_article = next(
        (idx for idx in range(toc_start, len(paras)) if ARTICLE_PAT.match(paras[idx])), None
    )
    candidates = [toc_end]
    if repeat is not None:
        candidates.append(repeat)
    if first_article is not None:
        candidates.append(first_article)
    return min(candidates)


def structure(paras: list[str], doc_title: str = "") -> dict:
    """把段落切成 元信息 + 正文（保留编/章/节层级，剔除目录段）。

    正文起点必须靠「认得出文件头」来定，不能靠「数够 11 段就开正文」——后者会把
    散文型文件（修改决定、刑法修正案、废止决定）开头的段落整段丢掉，实测 282 件
    （例如刑法修正案（十二）丢掉「一、在刑法第一百六十五条中增加一款…」）。
    """
    title = paras[0] if paras else ""
    history, doc_number = "", ""
    title_key = _norm_line(doc_title) if doc_title else ""

    # 公文标题常被排成多段（「中华人民共和国环境保护税法」+「实施条例」、
    # 「…关于批准《…修正案》的决定（…）」跨 3 段）。判据：把开头几段拼起来，
    # 只要还是文件名标题的前缀，就继续算标题。
    scan_start = 1
    if title_key:
        acc = _norm_line(paras[0]) if paras else ""
        k = 1
        while k < 6 and k < len(paras) and len(acc) < len(title_key):
            nxt = _norm_line(paras[k])
            if nxt and title_key.startswith(acc + nxt):
                acc += nxt
                k += 1
                continue
            break
        scan_start = k

    # 「目录」标记：它之前的段落都是标题页/公告/文号等文件头
    toc_idx = next((i for i in range(scan_start, min(len(paras), 40)) if TOC_PAT.match(paras[i])), None)

    # 文件头扫描必须在「有目录」时也跑：早先遇到目录就直接跳去 strip_toc，
    # 结果 422 件带目录的法规全丢了「沿革」与「文号」（连同施行日期推定的来源）。
    body_start = scan_start
    paren_open = False              # 被换行拆开的「（…通过 …）」沿革行
    header_limit = toc_idx if toc_idx is not None else min(len(paras), scan_start + 19)
    for i in range(scan_start, header_limit):
        p = paras[i]
        if paren_open:
            # 沿革行被排版拆成多段（宪法、部分司法解释的「（…通过 根据…修正…）」跨 2-3 段）
            history = f"{history} {p.strip()}".strip()
            if _closes_paren(p):
                paren_open = False
            body_start = i + 1
            continue
        if _is_title_continuation(paras, i):
            body_start = i + 1
            continue
        if _is_history_line(p):
            if not history:
                history = p.strip()
            # 沿革行里可能夹着令号（「2019年4月14日中华人民共和国国务院令第712号公布…」）
            m = DOC_NUMBER_IN_PAREN.search(p)
            if m and not doc_number:
                doc_number = m.group(0)
            paren_open = not _closes_paren(p)
            body_start = i + 1
            continue
        # 文号只从「整段就是文号」的段落取。早先把任何文件头段落都塞进 doc_number，
        # 结果一票司法解释的文号变成「公　告」或「2019年11月27日」，
        # 真正的 法释〔2019〕17号 反而被丢掉。
        if DOC_NUMBER_HEAD.match(p):
            doc_number = doc_number or p.strip()
            body_start = i + 1
            continue
        # 「公告」标题行、公告正文（《…》已于…现予公布…）、落款日期、单位署名、
        # 以及重复出现的标题行：都算文件头，但不产出文号
        if HEADER_ONLY.match(p) or (title_key and _norm_line(p) == title_key):
            body_start = i + 1
            continue
        body_start = i
        if toc_idx is None:
            break
        # 有「目录」标记时，标记之前的段落一律是文件头——公告正文、落款日期、
        # 单位署名这些容易漏判的行不能在这里中断扫描（否则后面的法释文号、
        # 沿革行都取不到，实测《…刑事诉讼法》的解释(20121220) 丢了整段沿革）。
        continue

    body_start = strip_toc(paras, toc_idx if toc_idx is not None else body_start)

    body = paras[body_start:] if body_start > 0 else paras[1:]

    articles = [p for p in body if ARTICLE_PAT.match(p)]
    chapters = [p for p in body if CHAPTER_PAT.match(p)]
    return {
        "title": title,
        "history": history,
        "doc_number": doc_number,
        "body": body,
        "article_count": len(articles),
        "chapter_count": len(chapters),
    }


# --------------------------------------------------------------------------- #
# 开源库补充（仅用于老式 .doc）
# --------------------------------------------------------------------------- #


def repo_supplement_index() -> dict[str, str]:
    """返回 {归一化标题: 仓库路径}，用于给老式 .doc 找替代正文。"""
    data = http_json(REPO_TREE, timeout=120)
    idx = {}
    for node in data.get("tree") or []:
        if node.get("type") != "blob":
            continue
        path = node["path"]
        if not path.endswith(".md") or path.split("/")[0] not in (
            "司法解释", "法律解释", "宪法相关法", "民法商法", "行政法",
            "经济法", "社会法", "刑法", "诉讼与非诉讼程序法", "宪法", "民法典",
        ):
            continue
        if Path(path).name.startswith("_"):
            continue
        stem = Path(path).stem
        stem = re.sub(r"\(\d{4}-\d{2}-\d{2}\)$", "", stem)
        key = re.sub(r"[^\u4e00-\u9fff]", "", stem)
        if key:
            idx.setdefault(key, path)
    return idx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从手工下载的官方文档构建语料")
    parser.add_argument("--source", action="append", default=None, help="下载目录，可重复")
    parser.add_argument("--skill-dir", default=None, help="技能目录（默认脚本上一级）")
    parser.add_argument("--out", default=None, help="输出语料目录（默认 references/corpus）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件（试跑用）")
    parser.add_argument("--no-repo", action="store_true", help="不用开源库补老式 .doc")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    skill_dir = (
        Path(args.skill_dir).expanduser().resolve()
        if args.skill_dir
        else Path(__file__).resolve().parents[1]
    )
    out_root = Path(args.out).resolve() if args.out else skill_dir / "references" / "corpus"
    # 不指定 --source 时，按技能目录下的 downloads/ 约定查找
    sources = args.source or [str(skill_dir / s) for s in DEFAULT_SOURCES]

    print("[1/5] 读取官方清单（全部状态）…")
    official = fetch_official()
    print(f"      官方条目 {len(official)} 个（标题+公布日期 唯一键）")

    print("[2/5] 解包下载文件…")
    files = read_downloads(sources)
    print(f"      共 {len(files)} 个文件")
    if not files:
        print("\n没有找到任何下载文件。两种做法：")
        print("  1) 跑 scripts/fetch_downloads.py 自动抓取，或把官网批量下载的 zip 放进 downloads/")
        print("     （三个子目录：法律语料 / 行政法规 / 司法解释）")
        print('  2) 或用 --source 指定目录，可重复：')
        print('     python scripts/build_from_downloads.py --source "D:\\我的下载\\法律语料"')
        return 1
    if args.limit:
        files = files[: args.limit]
        print(f"      --limit 生效，只处理前 {len(files)} 个")

    print("[3/5] 匹配官方状态并分层…")
    by_title = official_by_title(official)
    # 官网生成文件名时把标题里的空格和「、」统一写成「+」；split_name() 还原成
    # 「、」后与官方标题里的空格对不上（实测 9 件司法解释/决议）。再兜一层
    # "去掉分隔符"的规范化键，两边都规范再比，避免这类排版差异把件漏掉。

    def _norm_key(text: str) -> str:
        return re.sub(r"[\s、·]+", "", text)

    by_norm: dict[str, list[dict]] = defaultdict(list)
    for (_title, _date), _meta in official.items():
        by_norm[_norm_key(_title)].append({**_meta, "date": _date})

    records, unmatched = [], []
    for f in files:
        title, date, voided = split_name(f["name"])
        meta = official.get((title, date)) if date else None
        if meta is None:
            # 未能按（标题+公布日期）精确命中时，不能简单把"官方最新条目"的元数据套到这个
            # 文件上——那会让旧版继承新版的状态（实测把 2016 版标成"现行有效"，旧文本
            # 因此混进默认检索层）。改为比较日期来判断版本关系：
            #   文件日期 > 官方最新 → 多为含"打包修正"的合并文本（官方 gbrq 只记独立公布日）
            #   文件日期 < 官方最新 → 该文件是被取代的旧版
            cands = sorted(
                by_title.get(title) or by_norm.get(_norm_key(title)) or [],
                key=lambda x: x["date"],
                reverse=True,
            )
            newest = cands[0] if cands else None
            stamp = f"{date[:4]}-{date[4:6]}-{date[6:]}" if date else ""
            if newest and date and date > newest["date"]:
                meta = {
                    **newest,
                    "status": "现行有效（合并文本，含官方清单未单列的后续修正）",
                    "promulgated": stamp,
                    "effective": "",
                    "status_code": 3,
                }
            elif newest and date and date < newest["date"]:
                meta = {
                    **newest,
                    "status": "已修改（官方清单已有更新版本）",
                    "promulgated": stamp,
                    "effective": "",
                    "status_code": 2,
                }
            else:
                meta = newest
        if meta is None:
            unmatched.append(f)
        if meta is None:
            # 官方清单未收录（主要是全国人大大会作出的一次性决定，flk 只收"部分"）：
            # 保留可检索，但状态明确标注为未确认，避免被当成现行有效引用。
            layer = "current"
        elif meta["status_code"] is None:
            # 官方的「修改、废止的决定」没有时效性字段，按决定归类
            layer = "decision"
        else:
            layer = LAYER_OF_STATUS.get(meta["status_code"], "unknown")
        if DECISION_PAT.search(title):
            layer = "decision"
        if voided:
            # 官方在标题里直接标了「（失效）」：一律按历史层处理，防止失效文本
            # 混进默认检索层被当成现行有效引用
            layer = "historical"
        records.append(
            {
                "title": title,
                "date": date or ((meta or {}).get("promulgated") or "").replace("-", ""),
                "ext": f["ext"],
                "data": f["bytes"],
                "zip": f["zip"],
                "meta": meta,
                "layer": layer,
                "voided": voided,
            }
        )
    print(f"      分层统计: {dict(Counter(r['layer'] for r in records))}")
    print(f"      未匹配官方条目: {len(unmatched)}")

    print("[4/5] 解析正文…")
    # 开源库只用于给老式 .doc 补正文，是可选增强：GitHub 偶发 SSL/限流时不能
    # 让整个构建挂掉，退化成「仅元数据」即可（实测遇到过 UNEXPECTED_EOF_WHILE_READING）
    repo_idx: dict[str, str] = {}
    if not args.no_repo:
        try:
            repo_idx = repo_supplement_index()
        except Exception as exc:  # noqa: BLE001
            print(f"      ⚠ 开源库补充索引获取失败：{exc}")
            print("        老式 .doc 将只保留元数据；可用 --no-repo 跳过，或稍后重跑")
    print(f"      开源库补充索引 {len(repo_idx)} 个标题")
    parsed = Counter()
    for r in records:
        paragraphs, source_kind = None, None
        if r["ext"] == ".docx":
            try:
                paragraphs = docx_paragraphs(r["data"])
                source_kind = "官方 docx"
            except Exception as exc:  # noqa: BLE001
                print(f"      ⚠ docx 解析失败 {r['title'][:24]}: {exc}")
        if paragraphs is None and r["ext"] in (".doc", ".ofd"):
            key = re.sub(r"[^\u4e00-\u9fff]", "", r["title"])
            path = repo_idx.get(key) or next(
                (v for k, v in repo_idx.items() if key and (key in k or k in key)), None
            )
            if path:
                try:
                    # 必须用带重试的 http_get：裸 urlopen 遇到网络抖动会失败或读到截断内容，
                    # 结果是同一份输入构建出不同的语料（实测条文总数在 86,054 / 86,208 之间跳）
                    raw = http_get(REPO_RAW + urllib.parse.quote(path), timeout=60).decode(
                        "utf-8", "replace"
                    )
                    paragraphs = [ln.strip() for ln in raw.splitlines() if ln.strip()]
                    source_kind = f"开源库补充（{path}）"
                except Exception as exc:  # noqa: BLE001
                    print(f"      ⚠ 补充正文失败 {path}: {exc}")
        r["paragraphs"] = paragraphs
        r["source_kind"] = source_kind
        parsed[source_kind or "无正文（仅元数据）"] += 1
    print(f"      正文来源: {dict(parsed)}")

    if args.dry_run:
        print("[5/5] --dry-run：未写任何文件。")
        print("\n样例（前 5 条）:")
        for r in records[:5]:
            st = structure(r["paragraphs"], r["title"]) if r["paragraphs"] else {}
            print(
                f"  {r['title'][:26]:<28} {r['date']} {r['layer']:<10} "
                f"条文 {st.get('article_count', 0):>4}  章 {st.get('chapter_count', 0):>3}  "
                f"{r['source_kind'] or '仅元数据'}"
            )
        return 0

    print(f"[5/5] 写出语料 → {out_root}")
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_records = []
    used_names: Counter = Counter()
    for r in records:
        st = structure(r["paragraphs"], r["title"]) if r["paragraphs"] else {
            "title": r["title"], "history": "", "doc_number": "", "body": [],
            "article_count": 0, "chapter_count": 0,
        }
        meta = r["meta"] or {}
        if meta and meta.get("category"):
            category = meta["category"]
        elif re.search(r"全国人民代表大会|常务委员会", r["title"]):
            # flk 只收录"部分"有关法律问题的决定，大会作出的一次性决定常不在清单里
            category = "有关法律问题的决定"
        elif "最高人民法院" in r["title"] or "最高人民检察院" in r["title"]:
            category = "高法司法解释" if "最高人民法院" in r["title"] else "高检司法解释"
        else:
            category = "未分类"
        status_label = meta.get("status") or "未标注（官方库未收录该件状态）"
        if r["voided"]:
            status_label = "已失效（官方标题标注失效）"
        elif r["layer"] == "decision" and meta.get("status"):
            # 决定层里既有真正的修改/废止决定，也有官方没单列时效性的「有关法律问题的决定」，
            # 早先一律写成「修改/废止决定」是错的（实测 101 件被误标）
            status_label = (
                "修改/废止决定"
                if DECISION_PAT.search(r["title"]) or category.endswith("修改废止决定")
                else "时效性未标注（官方库未单列，按决定层处理）"
            )
        effective = meta.get("effective") or ""
        effective_source = "official" if effective else ""
        if not effective:
            effective = backfill_effective(r["paragraphs"], meta.get("promulgated") or "")
            effective_source = "text" if effective else ""
        # 合并文本（经多次修订的现行版本）在官方清单里只有最后一次修订的公布日期，
        # 正文附则写的却是原文本的施行日期，于是会出现「施行早于公布」。这是真实情况，
        # 不是解析错误——保留该日期，但标明它是从正文推定的，避免被当作官方元数据引用。
        derived_effective = effective_source == "text"
        stem = safe_stem(r["title"], r["date"], category)
        used_names[stem] += 1
        if used_names[stem] > 1:
            stem += f"-{used_names[stem]}"
        rel = Path(category) / f"{stem}.md"
        target = out_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)

        fm = [
            "---",
            f'title: "{r["title"]}"',
            f'category: "{category}"',
            f'layer: "{r["layer"]}"',
            f'status: "{status_label}"',
            f'promulgated: "{meta.get("promulgated") or ""}"',
            f'effective: "{effective}"',
            f'effective_source: "{effective_source or "unknown"}"',
            f'issuer: "{meta.get("issuer") or ""}"',
            f'doc_number: "{st.get("doc_number") or ""}"',
            f'source: "{r["source_kind"] or "仅元数据（未取得正文）"}"',
            f'official_url: "{DETAIL_URL + meta["bbbs"] if meta.get("bbbs") else ""}"',
            f'record_date: "{r["date"] or ""}"',
            "---",
            "",
            f"# {r['title']}",
            "",
        ]
        info = "　".join(
            x for x in (
                f"【{status_label}】",
                f"公布：{meta.get('promulgated') or '—'}",
                f"施行：{(effective + '（正文推定）') if derived_effective else (effective or '—')}",
                f"制定机关：{meta.get('issuer') or '—'}",
                f"文号：{st.get('doc_number') or '—'}",
            )
        )
        fm += [f"> {info}"]
        if st.get("history"):
            fm += [f"> 沿革：{st['history']}"]
        if derived_effective and meta.get("promulgated") and effective < meta["promulgated"]:
            fm += [
                "> 提示：施行日期取自正文附则（官方清单未提供）；该件是经多次修订的合并文本，"
                "附则日期为原文本的施行日，不等于本次修订的公布日期。"
            ]
        if r["layer"] != "current":
            fm += [
                "> ⚠️ 本文件不是现行有效版本，仅供了解沿革或处理旧案；引用前务必核对现行文本。"
            ]
        fm += [""]
        body_md = []
        for p in st["body"]:
            m = CHAPTER_PAT.match(p)
            if m:
                body_md.append(f"\n## {p}\n")
            else:
                body_md.append(p + "\n")
        fm += body_md if body_md else ["（未取得正文，请通过官方链接查阅。）"]
        target.write_text("\n".join(fm), encoding="utf-8")

        manifest_records.append(
            {
                "title": r["title"],
                "category": category,
                "layer": r["layer"],
                "status": status_label,
                "promulgated": meta.get("promulgated") or "",
                "effective": effective,
                "effective_source": effective_source or "unknown",
                "issuer": meta.get("issuer") or "",
                "official_url": DETAIL_URL + meta["bbbs"] if meta.get("bbbs") else "",
                "file": str(Path("corpus") / rel).replace("\\", "/"),
                "text_status": "full" if r["paragraphs"] else "metadata-only",
                "source": r["source_kind"] or "",
                "article_count": st["article_count"],
                "chars": sum(len(p) for p in st["body"]),
            }
        )

    manifest_records.sort(key=lambda x: (CATEGORY_ORDER.index(x["category"]) if x["category"] in CATEGORY_ORDER else 99, -int(x["promulgated"].replace("-", "") or 0)))
    manifest = {
        "generated_by": "scripts/build_from_downloads.py",
        "retrieved": time.strftime("%Y-%m-%d"),
        "text_source": "国家法律法规数据库官方 docx（scripts/fetch_downloads.py 爬取），本地解析",
        "count": len(manifest_records),
        "by_layer": dict(Counter(r["layer"] for r in manifest_records)),
        "by_status": dict(Counter(r["status"] for r in manifest_records)),
        "laws": manifest_records,
    }
    (out_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # 清理旧语料残留：本脚本只写不删，一旦改了类目归属或配对逻辑，上一版的文件
    # 会留在盘上变成重复条目（实测 11 个：9 件联合司法解释过去因配对失败被塞进
    # 「高法司法解释」、2 件决议的类目也变了）。只清 corpus/<类目>/ 下的 .md，
    # corpus 根目录的文件（law-index.md 之类）不动。
    keep = {Path(r["file"]).relative_to("corpus").as_posix() for r in manifest_records}
    stale = [
        p
        for p in out_root.rglob("*.md")
        if len(p.relative_to(out_root).parts) >= 2 and p.relative_to(out_root).as_posix() not in keep
    ]
    for p in stale:
        p.unlink()
    if stale:
        print(f"  清理旧语料残留 {len(stale)} 个文件")
        for p in stale[:10]:
            print(f"    - {p.relative_to(out_root).as_posix()}")
    for d in sorted((d for d in out_root.rglob("*") if d.is_dir()), key=lambda x: -len(x.parts)):
        try:
            d.rmdir()  # 类目整体搬走后留下的空目录
        except OSError:
            pass
    print(f"完成：{len(manifest_records)} 件")
    print(f"  分层: {manifest['by_layer']}")
    print(f"  状态: {manifest['by_status']}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
