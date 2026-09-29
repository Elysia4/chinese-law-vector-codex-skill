#!/usr/bin/env python3
"""条文级相关性检索：BM25 + 中文 n-gram + 口语/罪名查询扩展。

解决的是"我不知道该用法条里的哪个词"这件事：把 23126 条条文切出来，
用 BM25 打分排序，并用 references/query-expansions.json 把口语、罪名
映射成法条用语（如"被辞退"→"解除劳动合同"）补进召回。

注意：这是词面检索的强化，不是向量语义检索。真正的语义检索需要 embedding
模型或接口，本脚本不依赖任何第三方库，也不需要建索引。

用法
  python scripts/rank_search.py 公司可以因为员工吐槽就开除吗
  python scripts/rank_search.py 帮信罪
  python scripts/rank_search.py --law 民法典 房东不退押金
  python scripts/rank_search.py --top 5 高空抛物砸到人谁赔
  python scripts/rank_search.py --verbose 大数据杀熟

多路召回（推荐：由主模型把口语问题改写成法条用语后一并传入）
  python scripts/rank_search.py "房东不退我押金" \
      --extra-query "租赁合同约定的保证金未依法返还" --extra-query "出租人拒绝退还保证金构成违约"

  · 为什么要改写：用户说人话、法条说法言法语，词面对不上就召不回（"押金"↔"保证金返还"、
    "过了几年"↔"诉讼时效"）。实测两套各 100 题：主模型改写 @5 89%（不改写只有 32%），
    且优于本地 4B 小模型（82%），还不需要用户装任何额外模型。
  · 弱的改写会倒扣分：本地 4B 改写把 @1 从 41% 拖到 36%，所以改写要用法条用词、不要复述口语。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fetch_corpus import format_live_items, live_hint, live_search, stale_note

# 容忍「第二百七十一 条」这类条号内混入空白的情况（docx 解析的现实）
ARTICLE_RE = re.compile(r"^第[零一二三四五六七八九十一百千万两]+\s*条")
HEADING_RE = re.compile(r"^(#{2,3})\s*(.+?)\s*$")
# 民法典/生态环境法典这类法典有「第一分编 通则」一级，必须单独认，
# 否则分编会被当成编一级、把章标题挤掉
CHAPTER_RE = re.compile(r"^第[零一二三四五六七八九十一百千万两]+\s*[章节]")
BOOK_RE = re.compile(r"^第[零一二三四五六七八九十一百千万两]+\s*(?:分编|[编篇])")
SPLIT_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
STOP_CHARS = set(
    "的了吗呢吧啊呀嘛么哦噢哟着过被把给让向从对以及就是有能可会要想之其此该等"
    "因为在就和与或而但也都很太更最只又再还是不各全同并且若如则由于到得地"
    "请 问怎什谁哪为个些这那我你他她它们"
)

K1 = 1.2
B = 0.6
WEIGHT_PHRASE = 1.0
WEIGHT_QUADGRAM = 0.85
WEIGHT_TRIGRAM = 0.65
WEIGHT_BIGRAM = 0.45
WEIGHT_EXPANSION = 0.8
MAX_DF_RATIO = 0.05  # 出现频率过高的词没有区分度，不参与打分
MIN_DF_BY_SIZE = {2: 8, 3: 4}  # 越短的 n-gram 越容易是切词噪声
DEFAULT_MIN_DF = 2  # 4 字以上基本是法律术语（竞业限制、高空抛物）
# 语料被 --law 收窄到几十上百条时，按比例算出的阈值会失去意义（5% 只剩 5 条，
# 连"解除劳动合同"这种关键词都会被当成高频词丢掉）。这两个下限保证小众语料仍可检索。
MIN_DF_CEILING = 50
MIN_DF_FLOOR_RATIO = 0.02

PHRASE = "phrase"
GRAM = "gram"
EXPANSION = "expansion"

LAYER_LABEL = {
    "current": "现行有效/尚未生效",
    "historical": "已修改/已废止",
    "decision": "修改废止决定",
    "unknown": "状态未确认",
}

RRF_K = 60
VECTOR_CANDIDATES = 400
DEFAULT_OLLAMA = "http://127.0.0.1:11434"
# 语料里最长条文 1873 字符，4096 上下文足够且不会截断。不限制的话 Ollama 会按显存
# 自动选上下文（实测给 0.6B 模型分了 32768，占 5.78GB 显存；限制后降到 2.37GB）。
EMBED_NUM_CTX = 4096
# 用完多久卸载嵌入模型。默认 60 秒：连续追问时模型是热的（约 0.2 秒），
# 停手一分钟后自动卸载，把 2.37GB 显存还给系统（冷启动加载约 19 秒）。
# 想改：设环境变量 CHINA_LAW_KEEP_ALIVE，如 "5m" 或 0（请求返回即卸载）。
EMBED_KEEP_ALIVE = os.environ.get("CHINA_LAW_KEEP_ALIVE", "60s")
# Ollama 没在跑时要不要自动拉起。默认开；设 CHINA_LAW_AUTOSTART_OLLAMA=0 可关闭。
OLLAMA_AUTOSTART = os.environ.get("CHINA_LAW_AUTOSTART_OLLAMA", "1").lower() not in (
    "0",
    "false",
    "no",
)


def load_manifest(corpus_root: Path) -> dict:
    manifest_path = corpus_root / "manifest.json"
    if not manifest_path.exists():
        sys.exit(f"找不到 {manifest_path}，请先运行 scripts/fetch_corpus.py 生成语料。")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def load_expansions(skill_dir: Path) -> dict[str, list[str]]:
    path = skill_dir / "references" / "query-expansions.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, list)}


def http_json(
    url: str, payload: dict | None = None, timeout: int = 120, retries: int = 2
) -> dict:
    """访问本地 Ollama。必须绕过系统代理，否则 127.0.0.1 会被丢进代理。"""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                pass
            last = Exception(f"HTTP {exc.code} {detail[:200]}")
            time.sleep(1.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001 - 网络/服务错误统一重试
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求 Ollama 失败：{url}（{last}）")


def embed_texts(
    model: str, texts: list[str], base: str = DEFAULT_OLLAMA
) -> list[list[float]]:
    """优先用 /api/embed 批量接口，老版本回退到 /api/embeddings。"""
    keep_alive = EMBED_KEEP_ALIVE
    if keep_alive.isdigit():
        keep_alive = int(keep_alive)
    try:
        data = http_json(
            f"{base}/api/embed",
            {
                "model": model,
                "input": texts,
                "options": {"num_ctx": EMBED_NUM_CTX},
                "keep_alive": keep_alive,
            },
        )
        vectors = data.get("embeddings")
        if vectors:
            return vectors
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    out: list[list[float]] = []
    for text in texts:
        data = http_json(
            f"{base}/api/embeddings",
            {
                "model": model,
                "prompt": text,
                "options": {"num_ctx": EMBED_NUM_CTX},
                "keep_alive": keep_alive,
            },
        )
        out.append(data["embedding"])
    return out


def ollama_available(base: str = DEFAULT_OLLAMA, timeout: float = 0.6) -> bool:
    """快速探活。不探的话，Ollama 没开时每次查询都要白等约 4.5 秒重试退避。"""
    try:
        http_json(f"{base}/api/tags", timeout=timeout, retries=1)
        return True
    except Exception:  # noqa: BLE001 - 探活失败就是不可用
        return False


def find_ollama_exe() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA") or ""
    for candidate in (
        os.path.join(local, "Programs", "Ollama", "ollama.exe") if local else "",
        r"C:\Program Files\Ollama\ollama.exe",
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def ollama_models_dir() -> str | None:
    """模型目录：进程环境优先，其次读用户注册表（setx 写进去的值）。"""
    value = os.environ.get("OLLAMA_MODELS")
    if value:
        return value
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, "OLLAMA_MODELS")[0]
    except Exception:  # noqa: BLE001 - 读不到就让 Ollama 用默认目录
        return None


def start_ollama(base: str = DEFAULT_OLLAMA, wait_seconds: float = 25.0) -> tuple[bool, str]:
    """把本地 Ollama 服务拉起来，返回（是否就绪, 说明）。"""
    exe = find_ollama_exe()
    if not exe:
        return False, "找不到 ollama 可执行文件"
    env = os.environ.copy()
    # 关键：必须显式传模型目录。否则服务会用默认目录，既找不到 D 盘那份模型，
    # 还可能顺手把模型重新下载到 C 盘。
    models = ollama_models_dir()
    if models:
        env["OLLAMA_MODELS"] = models
    host = base.split("//", 1)[-1].rstrip("/")
    if host and host != "127.0.0.1:11434":
        env["OLLAMA_HOST"] = host
    # 静默启动：CREATE_NO_WINDOW 与 DETACHED_PROCESS 互斥（同时设置时前者被忽略，
    # 会露出黑色控制台窗口），所以只用 CREATE_NO_WINDOW，再配 STARTF_USESHOWWINDOW
    # + SW_HIDE 兜一层。CREATE_BREAKAWAY_FROM_JOB 用来摆脱调用方所在的作业对象，
    # 否则父进程退出时子进程可能被一起回收。
    flags = 0
    startupinfo = None
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    def spawn(creation_flags: int) -> None:
        subprocess.Popen(
            [exe, "serve"],
            env=env,
            creationflags=creation_flags,
            startupinfo=startupinfo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    try:
        spawn(flags)
    except OSError:
        # 调用方的作业对象不允许 breakaway，退一步：去掉那个标志再试
        try:
            spawn(flags & ~0x01000000)
        except Exception as exc:  # noqa: BLE001
            return False, f"拉起失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"拉起失败：{exc}"
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if ollama_available(base, timeout=0.8):
            return True, "已自动启动"
        time.sleep(0.5)
    return False, f"拉起后 {wait_seconds:.0f} 秒内仍未就绪"


def doc_key(doc: tuple) -> str:
    return f"{doc[5]}#{doc[6]}"


def load_vector_index(corpus_root: Path):
    """读取 build_vectors.py 生成的向量索引，返回键、已归一化向量与元数据。"""
    npz_path = corpus_root / "vectors.npz"
    meta_path = corpus_root / "vectors.meta.json"
    if not npz_path.exists() or not meta_path.exists():
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    data = np.load(npz_path, allow_pickle=False)
    vectors = data["vectors"].astype(np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return {
        "keys": [str(key) for key in data["keys"]],
        "vectors": vectors / norms,
        "meta": json.loads(meta_path.read_text(encoding="utf-8")),
    }


def vector_ranking(index: dict, query: str, base: str) -> list[tuple[str, float]]:
    """返回 [(条文键, 余弦相似度)]，按相似度降序。"""
    import numpy as np

    model = index["meta"].get("model")
    query_vector = np.asarray(embed_texts(model, [query], base=base)[0], dtype=np.float32)
    norm = float(np.linalg.norm(query_vector)) or 1.0
    query_vector = query_vector / norm
    if query_vector.shape[0] != index["vectors"].shape[1]:
        raise RuntimeError(
            f"查询向量维度 {query_vector.shape[0]} 与索引 {index['vectors'].shape[1]} 不一致，"
            "请用同一模型重建索引"
        )
    similarities = index["vectors"] @ query_vector
    order = np.argsort(-similarities)[:VECTOR_CANDIDATES]
    return [(index["keys"][int(i)], float(similarities[int(i)])) for i in order]


def fuse(
    documents: list[tuple],
    bm25_ranked: list[tuple[float, tuple]],
    vector_hits: list[tuple[str, float]],
) -> list[tuple[float, float | None, float, tuple]]:
    """把 BM25 与向量结果按 RRF 融合，返回 [(bm25 分, 向量分, 综合分, 条文)]。"""
    by_key = {doc_key(doc): doc for doc in documents}
    bm25_score = {doc_key(doc): score for score, doc in bm25_ranked}
    bm25_rank = {doc_key(doc): rank for rank, (_, doc) in enumerate(bm25_ranked, start=1)}
    vector_score = {key: score for key, score in vector_hits if key in by_key}
    vector_rank = {
        key: rank for rank, (key, _) in enumerate(vector_hits, start=1) if key in by_key
    }

    combined: list[tuple[float, float | None, float, tuple]] = []
    for key, doc in by_key.items():
        score = bm25_score.get(key, 0.0)
        similarity = vector_score.get(key)
        if not score and similarity is None:
            continue
        rrf = 0.0
        if key in bm25_rank:
            rrf += 1.0 / (RRF_K + bm25_rank[key])
        if key in vector_rank:
            rrf += 1.0 / (RRF_K + vector_rank[key])
        combined.append((score, similarity, rrf, doc))
    combined.sort(key=lambda item: (-item[2], item[3][0], item[3][2]))
    return combined


def body_offset(lines: list[str]) -> int:
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return index + 1
    return 0


def load_documents(
    corpus_root: Path,
    manifest: dict,
    law_filter: str,
    layers: tuple[str, ...] = ("current",),
) -> list[tuple]:
    """切出条文单元：(法律名, 类目, 条文号, 章节, 正文, 文件, 行号, 状态, 层)。

    layers 默认只含 current（现行有效 + 尚未生效）。历史版本（已修改/已废止）与
    修改废止决定默认不参与检索——否则问"合同怎么解除"会同时命中民法典和已废止的合同法。
    """
    documents: list[tuple] = []
    for record in manifest["laws"]:
        if record["text_status"] != "full":
            continue
        if record.get("layer", "current") not in layers:
            continue
        title = record["title"]
        if law_filter:
            if law_filter not in title and law_filter not in re.sub(r"^中华人民共和国", "", title):
                continue
        path = corpus_root / record["file"].removeprefix("corpus/")
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        offset = body_offset(all_lines)
        lines = all_lines[offset:]
        book = chapter = ""
        current: tuple[int, str] | None = None
        buffer: list[str] = []

        def flush() -> None:
            if current is None:
                return
            start, label = current
            body = " ".join(part.strip() for part in buffer if part.strip()).strip()
            documents.append(
                (title, record["category"], label, " ".join(x for x in (book, chapter) if x),
                 body, record["file"], start + offset + 1,
                 record.get("status", ""), record.get("layer", "current"))
            )

        for index, line in enumerate(lines):
            heading = HEADING_RE.match(line)
            if heading:
                flush()
                current, buffer = None, []
                label = heading.group(2)
                if CHAPTER_RE.match(label):
                    chapter = label
                elif BOOK_RE.match(label):
                    # 进入新的编/分编：清掉上一编的章，否则检索结果里的
                    # 「编 > 章」层级会串编
                    book, chapter = label, ""
                else:
                    book = label
                continue
            if ARTICLE_RE.match(line):
                flush()
                current = (index, line.split()[0] if line.split() else line.strip())
                buffer = [line]
                continue
            if current is not None:
                buffer.append(line)
        flush()
    return documents


def build_terms(
    query: str, expansions: dict[str, list[str]]
) -> tuple[dict[str, tuple[float, str, int]], list[str]]:
    """把查询拆成加权词项，并叠加口语/罪名扩展。"""
    terms: dict[str, tuple[float, str, int]] = {}

    def add(term: str, weight: float, kind: str, min_df: int = 1) -> None:
        current = terms.get(term)
        if current is None or weight > current[0]:
            terms[term] = (weight, kind, min_df)

    segments = [seg for seg in SPLIT_RE.split(query) if seg]
    for segment in segments:
        if len(segment) <= 16:
            add(segment, WEIGHT_PHRASE, PHRASE)
        for size, weight in (
            (4, WEIGHT_QUADGRAM),
            (3, WEIGHT_TRIGRAM),
            (2, WEIGHT_BIGRAM),
        ):
            min_df = MIN_DF_BY_SIZE.get(size, DEFAULT_MIN_DF)
            for index in range(len(segment) - size + 1):
                gram = segment[index : index + size]
                if any(char in STOP_CHARS for char in gram):
                    continue
                add(gram, weight, GRAM, min_df)

    added: list[str] = []
    joined = "".join(segments)
    for key, values in expansions.items():
        if key in joined:
            for value in values:
                before = terms.get(value)
                add(value, WEIGHT_EXPANSION, EXPANSION, 1)
                # 口语词本身也被切成 n-gram 时会以低权重占位，这里要把它升级为扩展词
                if before is None or before[0] < WEIGHT_EXPANSION:
                    added.append(value)
    return terms, added


def search(
    documents: list[tuple], terms: dict[str, tuple[float, str, int]], verbose: bool
) -> list[tuple[float, tuple]]:
    total = len(documents)
    if not total:
        return []
    lengths = [len(doc[4]) for doc in documents]
    average = sum(lengths) / total
    scores = [0.0] * total
    stats: list[tuple[str, float, int, str, int]] = []

    for term, (weight, kind, min_df) in terms.items():
        postings: list[tuple[int, int]] = []
        for index, doc in enumerate(documents):
            count = doc[4].count(term)
            if count:
                postings.append((index, count))
        df = len(postings)
        floor = min_df if kind == GRAM else 1
        floor = min(floor, max(1, int(MIN_DF_FLOOR_RATIO * total)))
        ceiling = MAX_DF_RATIO * (0.5 if kind == PHRASE else 1.0) * total
        ceiling = max(ceiling, MIN_DF_CEILING)
        if df < floor or df > ceiling:
            stats.append((term, weight, df, kind, floor))
            continue
        stats.append((term, weight, df, kind, floor))
        idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
        for index, count in postings:
            length = lengths[index]
            denominator = count + K1 * (1 - B + B * length / average)
            scores[index] += weight * idf * (count * (K1 + 1)) / denominator
            if term in documents[index][0]:
                scores[index] += weight * idf * 1.5

    if verbose:
        print(f"查询词项 {len(stats)} 个（df=命中的条文数；df 过低或过高的会被丢弃）：")
        for term, weight, df, kind, floor in sorted(
            stats, key=lambda item: (-item[1], item[0])
        )[:40]:
            ceiling = MAX_DF_RATIO * (0.5 if kind == PHRASE else 1.0) * total
            ceiling = max(ceiling, MIN_DF_CEILING)
            used = "用" if floor <= df <= ceiling else "弃"
            print(f"  {used} {kind:<9} w={weight:<5} df={df:<6} {term}")
        print()

    ranked = [(scores[index], documents[index]) for index in range(total) if scores[index] > 0]
    ranked.sort(key=lambda item: (-item[0], item[1][0], item[1][2]))
    return ranked


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="条文级相关性检索（BM25 + 中文 n-gram + 口语扩展）"
    )
    parser.add_argument("query", nargs="+", help="自然语言问题或关键词")
    parser.add_argument("--law", default=None, help="限定法律名称（可用简称）")
    # 默认 20：实测（30 题）读取窗口取前 8 条时正确条文命中 63.3%，放宽到前 20 条是
    # 80.0%——多读十几条是目前最便宜的一次召回提升，所以直接把它设成默认值。
    parser.add_argument("--top", type=int, default=20, help="返回条数上限（默认 20）")
    parser.add_argument(
        "--extra-query", action="append", default=None,
        help="把口语问题改写成的法条用语表述（可重复传）。与原问题一起做多路召回再 RRF 融合",
    )
    parser.add_argument("--chars", type=int, default=500, help="每条条文显示的字数上限")
    parser.add_argument("--verbose", action="store_true", help="显示词项与命中统计（排查用）")
    parser.add_argument("--no-vector", action="store_true", help="只用 BM25，不做向量融合")
    parser.add_argument(
        "--include-repealed", action="store_true", help="同时检索已修改/已废止的历史版本"
    )
    parser.add_argument(
        "--all-layers", action="store_true", help="全部层都检索（含修改废止决定与未确认状态）"
    )
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA, help="Ollama 地址（混合检索用）")
    parser.add_argument("--corpus", default=None, help="语料目录（默认技能内 references/corpus）")
    parser.add_argument(
        "--live",
        action="store_true",
        help="本地零命中时自动到官方库实时查一次（只给条目与链接，不抓正文）",
    )
    args = parser.parse_args(argv)
    skill_dir = Path(__file__).resolve().parents[1]
    args.skill_dir = skill_dir
    args.corpus_root = (
        Path(args.corpus).expanduser().resolve()
        if args.corpus
        else skill_dir / "references" / "corpus"
    )
    args.text = " ".join(args.query)
    if args.all_layers:
        args.layers = ("current", "historical", "decision", "unknown")
    elif args.include_repealed:
        args.layers = ("current", "historical")
    else:
        args.layers = ("current",)
    return args


def print_live(query: str) -> None:
    """零命中时到官方库实时查一次：只给条目与官方链接，不抓正文。"""
    print()
    print(f"—— 在线兜底：到官方库实时检索「{query}」——")
    try:
        items, total, note = live_search(query, limit=5)
    except Exception as exc:  # noqa: BLE001 - 网络失败不应该让本地检索崩掉
        print(f"查询官方库失败：{exc}")
        print("（网络不可用时只能靠本地语料；可稍后用 scripts/search_live.py 重试）")
        return
    for line in format_live_items(items, total, note):
        print(line)
    print("提示：正文请打开官方链接查看；本地未收录时按 README「更新维护」更新后即可离线检索。")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    manifest = load_manifest(args.corpus_root)
    expansions = load_expansions(args.skill_dir)
    documents = load_documents(args.corpus_root, manifest, args.law or "", args.layers)
    if not documents:
        print(f"没有可检索的条文（--law {args.law} / 检索层 {args.layers} 过滤后为空）。")
        return 1

    terms, added = build_terms(args.text, expansions)
    bm25_ranked = search(documents, terms, args.verbose)

    print(f'查询："{args.text}"')
    if args.layers != ("current",):
        print(
            "检索层："
            + "、".join(LAYER_LABEL.get(x, x) for x in args.layers)
            + "（含历史版本，引用前务必核对状态）"
        )
    if added:
        print(f"口语/罪名扩展：{'、'.join(added)}（权重 {WEIGHT_EXPANSION}，低于直接命中）")

    vector_hits: list[tuple[str, float]] = []
    index = None if args.no_vector else load_vector_index(args.corpus_root)
    if args.no_vector:
        note = "仅 BM25（已用 --no-vector 关闭向量检索）"
    elif index is not None:
        # 语料重建过但索引没重建时，旧向量会挂到错误的条文上，这里直接退回 BM25。
        # 注意方向：--law 会把 documents 收窄成子集，所以要看"当前条文有多少能在索引里找到"。
        index_keys = set(index["keys"])
        current_keys = [doc_key(doc) for doc in documents]
        covered = sum(1 for key in current_keys if key in index_keys) / max(1, len(current_keys))
        # 正常情况覆盖率是 100%，所以阈值可以卡得很紧，用 99% 换取灵敏度
        if covered < 0.99:
            # 覆盖率不足有两种原因，给的建议完全不同：
            #   a) 索引是默认只对现行层建的，而这次开了 --include-repealed / --all-layers
            #   b) 语料变了但索引没重建
            default_keys = [
                doc_key(doc)
                for doc in load_documents(args.corpus_root, manifest, args.law or "", ("current",))
            ]
            default_covered = sum(1 for key in default_keys if key in index_keys) / max(1, len(default_keys))
            if default_covered >= 0.99:
                note = (
                    f"仅 BM25（向量索引只覆盖现行层，本次检索含历史层，只有 {covered:.0%} 的条文有向量；"
                    "要连历史层一起做语义检索请先运行 python scripts/build_vectors.py --all-layers）"
                )
            else:
                note = (
                    f"仅 BM25（索引与语料不匹配，当前条文只有 {covered:.0%} 在索引中；"
                    "重建语料后请重新运行 python scripts/build_vectors.py）"
                )
        else:
            alive = ollama_available(args.ollama_url)
            hint = ""
            if not alive and OLLAMA_AUTOSTART:
                ready, detail = start_ollama(args.ollama_url)
                alive = ready
                hint = f"，{detail}"
            if alive:
                try:
                    vector_hits = vector_ranking(index, args.text, args.ollama_url)
                    note = f"BM25 + 向量（{index['meta'].get('model')}{hint}）"
                except Exception as exc:  # noqa: BLE001 - 向量不可用不应阻断检索
                    note = f"仅 BM25（向量检索失败：{str(exc)[:100]}）"
            else:
                note = (
                    f"仅 BM25（Ollama 未运行{hint}；"
                    "可用 python scripts/build_vectors.py --check 诊断）"
                )
    else:
        note = "仅 BM25（未建向量索引，可用 python scripts/build_vectors.py 建立）"
    print(f"检索方式：{note}")

    # 语料太久没更新就提醒一句——用户「忘了更新」是最常见的过期来源
    caution = stale_note(args.corpus_root)
    if caution:
        print(caution)

    # 多路召回：主模型把口语问题改写成多条法条用语表述后一并传入（--extra-query）。
    # 每一路各自做 BM25(+向量) 融合，再把各路按 RRF 合并。两套各 100 题实测：
    # 主模型改写优于本地小模型改写（@1 +24 点、@5 +7 点），且用户端不需要任何额外模型
    # ——详见 eval/results.md 与 docs/技术选型输入-2026-09-29.md。
    extra_queries = [
        q.strip() for q in (args.extra_query or []) if q.strip() and q.strip() != args.text
    ]
    merged_mode = bool(extra_queries) and (bool(bm25_ranked) or bool(vector_hits))
    if merged_mode:
        routes: list[tuple[str, list, list]] = [(args.text, bm25_ranked, vector_hits)]
        for extra in extra_queries:
            extra_terms, _extra_added = build_terms(extra, expansions)
            extra_bm25 = search(documents, extra_terms, False)
            extra_vec: list[tuple[str, float]] = []
            if vector_hits:  # 主路用上了向量，说明向量可用，改写路也一起走向量
                try:
                    extra_vec = vector_ranking(index, extra, args.ollama_url)
                except Exception:  # noqa: BLE001 - 单路失败不影响整次检索
                    extra_vec = []
            routes.append((extra, extra_bm25, extra_vec))
        merged_scores: dict[str, float] = {}
        merged_docs: dict[str, tuple] = {}
        for _query, route_bm25, route_vec in routes:
            ordered = (
                [doc for _b, _v, _c, doc in fuse(documents, route_bm25, route_vec)]
                if route_vec
                else [doc for _s, doc in route_bm25]
            )
            for rank, doc in enumerate(ordered[:100], start=1):
                key = doc_key(doc)
                merged_docs[key] = doc
                merged_scores[key] = merged_scores.get(key, 0.0) + 1.0 / (60 + rank)
        results = [
            (score, None, 0.0, merged_docs[key])
            for key, score in sorted(merged_scores.items(), key=lambda kv: -kv[1])
        ]
        order_note = f"多路召回 RRF（{len(routes)} 路：原问题 + {len(extra_queries)} 条改写）"

    if not merged_mode and not bm25_ranked and not vector_hits:
        print(f"\n在 {len(documents)} 条条文中没有命中任何词项。")
        print("可尝试：换成法条用词、用 --law 限定法律、加 --verbose 看哪些词没命中，")
        print("或改用 scripts/search_corpus.py --list 按法律名查找。")
        if args.live:
            print_live(args.text)
        else:
            print()
            print(live_hint(args.text))
        return 1

    if merged_mode:
        pass  # results 已在多路分支里算好
    elif vector_hits:
        results = fuse(documents, bm25_ranked, vector_hits)
        order_note = "BM25 与向量 RRF 融合排序"
    else:
        results = [(score, None, 0.0, doc) for score, doc in bm25_ranked]
        order_note = "BM25 相关性排序"

    if merged_mode:
        print(f"\n多路召回：原问题 + {len(extra_queries)} 条改写")
        for extra in extra_queries:
            print(f"  · {extra}")
    print(f"\n命中 {len(results)} 条 / 可检索条文 {len(documents)} 条（{order_note}）：\n")
    for order, (score, similarity, _, doc) in enumerate(results[: args.top], start=1):
        title, category, article, chapter, body, file, line, status, layer = doc
        signal = f"多路 RRF {score:.4f}" if merged_mode else f"BM25 {score:.1f}"
        if similarity is not None:
            signal += f" ｜ 向量 {similarity:.3f}"
        flag = "" if layer == "current" else "⚠️ "
        print(f"{order}. {flag}《{title}》{article}　{signal}　（{category}｜{status}）")
        if chapter:
            print(f"   {chapter}")
        print(f"   {file} 行 {line}")
        text = body if len(body) <= args.chars else body[: args.chars] + "…"
        print(f"   {text}")
        print()
    if len(results) > args.top:
        print(f"（另有 {len(results) - args.top} 条命中未显示，用 --top 调整）")
    print("提示：以上是相关性排序而非结论，别只看第 1 条；引用前请用 Read 打开文件核对完整条文，")
    print("      并以官方数据库 https://flk.npc.gov.cn 的最新文本为准。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - 仅影响控制台输出
        pass
    raise SystemExit(main())
