#!/usr/bin/env python3
"""用本地 Ollama 的嵌入模型给全部条文建向量索引，供混合检索使用。

产物（都在 references/corpus/ 下）
  vectors.npz        条文向量矩阵（float16）+ 对应的条文键
  vectors.meta.json  模型名、维度、条数、生成时间

用法
  python scripts/build_vectors.py --check                        # 先确认服务与模型是否就绪
  python scripts/build_vectors.py --model qwen3-embedding:0.6b   # 建索引，可中断续跑
  python scripts/build_vectors.py --model bge-m3                # 换模型（会覆盖旧索引）

中文检索推荐 qwen3-embedding:0.6b（约 639MB，1024 维）或 bge-m3（约 1158MB，1024 维）。
注意：Ollama 里的生成模型（如 Qwen3-4B）不能当嵌入模型用，必须拉专用嵌入模型。
如果模型目录不在默认位置，启动服务时要显式带上环境变量指向真正的 models 目录，
例如 OLLAMA_MODELS=<你的模型目录> ollama serve，否则会去默认目录找不到模型。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rank_search import (  # noqa: E402  复用 HTTP 层与条文切分逻辑
    DEFAULT_OLLAMA,
    embed_texts,
    http_json,
    load_documents,
    load_manifest,
)

def ollama_status(base: str) -> dict | None:
    try:
        return http_json(f"{base}/api/tags", timeout=15, retries=1)
    except Exception:  # noqa: BLE001
        return None


def embed(model: str, texts: list[str], base: str) -> list[list[float]]:
    """调用 Ollama 取向量；把"模型不支持嵌入"翻译成可操作的提示。"""
    try:
        return embed_texts(model, texts, base=base)
    except RuntimeError as exc:
        message = str(exc)
        if "embedding" in message.lower():
            raise RuntimeError(
                f"{message}\n"
                "Ollama 里的生成模型（如 Qwen3-4B）不能当嵌入模型用，请拉一个专用嵌入模型：\n"
                "  ollama pull qwen3-embedding:0.6b"
            ) from None
        raise


def doc_key(doc: tuple) -> str:
    return f"{doc[5]}#{doc[6]}"


def doc_text(doc: tuple) -> str:
    # 按下标取值：条文元组以后可能继续加字段，解包会脆
    return f"{doc[0]} {doc[2]} {doc[3]} {doc[4]}".strip()


def load_existing(corpus_root: Path):
    npz_path = corpus_root / "vectors.npz"
    meta_path = corpus_root / "vectors.meta.json"
    if not npz_path.exists() or not meta_path.exists():
        return None
    data = np.load(npz_path, allow_pickle=False)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return list(data["keys"]), data["vectors"], meta


def save(corpus_root: Path, keys: list[str], matrix: np.ndarray, meta: dict) -> None:
    np.savez_compressed(
        corpus_root / "vectors.npz",
        keys=np.array(keys, dtype="U"),
        vectors=matrix.astype(np.float16),
    )
    (corpus_root / "vectors.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def check(base: str, model: str) -> int:
    status = ollama_status(base)
    if status is None:
        print(f"Ollama 不可达（{base}）。启动方式：桌面应用打开 Ollama，或")
        print("  确认 OLLAMA_MODELS 指向你实际的模型目录，再启动 ollama serve")
        return 1
    names = [m["name"] for m in status.get("models") or []]
    print(f"Ollama 就绪，版本 {status.get('version', '未知')}，已安装 {len(names)} 个模型：")
    for name in names:
        print(f"  - {name}")
    target = next(
        (n for n in names if n == model or n.split(":")[0] == model.split(":")[0]), None
    )
    if not target:
        print(f"\n没有找到嵌入模型 {model}。先拉取：")
        print(f"  ollama pull {model}")
        return 1
    try:
        probe = embed(target, ["中华人民共和国劳动合同法 第二十三条 竞业限制"], base)
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1
    print(f"\n模型 {target} 可用，向量维度 {len(probe[0])}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 Ollama 建条文向量索引")
    parser.add_argument("--model", default="qwen3-embedding:0.6b", help="Ollama 嵌入模型名")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA, help="Ollama 地址")
    parser.add_argument("--batch", type=int, default=64, help="每批嵌入条数")
    parser.add_argument("--check", action="store_true", help="只检查服务与模型是否就绪")
    parser.add_argument("--rebuild", action="store_true", help="忽略已有索引，从头重建")
    parser.add_argument(
        "--all-layers",
        action="store_true",
        help="连历史版本与修改废止决定一起建索引（默认只建现行层，约多 30%% 条）",
    )
    parser.add_argument("--corpus", default=None, help="语料目录")
    parser.add_argument("--law", default=None, help="只嵌入某部法律（调试用）")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    skill_dir = Path(__file__).resolve().parents[1]
    corpus_root = (
        Path(args.corpus).expanduser().resolve()
        if args.corpus
        else skill_dir / "references" / "corpus"
    )
    if args.check:
        return check(args.ollama_url, args.model)

    manifest = load_manifest(corpus_root)
    layers = ("current", "historical", "decision", "unknown") if args.all_layers else ("current",)
    documents = load_documents(corpus_root, manifest, args.law or "", layers)
    keys = [doc_key(doc) for doc in documents]
    texts = [doc_text(doc) for doc in documents]
    print(f"待嵌入条文 {len(texts)} 条，模型 {args.model}（检索层：{'全部' if args.all_layers else '现行层'}）")

    start = 0
    pieces: list[np.ndarray] = []
    previous = None if args.rebuild else load_existing(corpus_root)
    if previous:
        old_keys, old_vectors, old_meta = previous
        same_model = old_meta.get("model") == args.model
        prefix_ok = old_keys == keys[: len(old_keys)]
        if same_model and prefix_ok and len(old_keys) < len(keys):
            start = len(old_keys)
            pieces.append(old_vectors)
            print(f"检测到已完成 {start} 条，从第 {start + 1} 条继续（--rebuild 可重来）")
        elif same_model and prefix_ok:
            print("索引已是最新，无需重建。")
            return 0
        else:
            print("已有索引与当前语料或模型不一致，重新构建。")

    started = time.time()
    dimension = None
    for offset in range(start, len(texts), args.batch):
        batch = texts[offset : offset + args.batch]
        vectors = embed(args.model, batch, args.ollama_url)
        array = np.asarray(vectors, dtype=np.float32)
        if dimension is None:
            dimension = array.shape[1]
        pieces.append(array)
        done = offset + len(batch)
        elapsed = time.time() - started
        rate = (done - start) / elapsed if elapsed else 0
        eta = (len(texts) - done) / rate / 60 if rate else 0
        print(f"  进度 {done}/{len(texts)}　{rate:.1f} 条/秒　预计剩余 {eta:.1f} 分钟", flush=True)
        if done % (args.batch * 10) < args.batch:
            save(
                corpus_root,
                keys[:done],
                np.vstack(pieces),
                {"model": args.model, "dim": dimension, "count": done, "partial": True},
            )

    matrix = np.vstack(pieces)
    meta = {
        "model": args.model,
        "dim": int(matrix.shape[1]),
        "count": int(matrix.shape[0]),
        "partial": False,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ollama_url": args.ollama_url,
        "corpus_count": len(keys),
        "doc_text": "法律名 + 条文号 + 章节 + 正文",
    }
    save(corpus_root, keys, matrix, meta)
    print(f"\n完成：{matrix.shape[0]} 条 × {matrix.shape[1]} 维，已写入 vectors.npz")
    print("现在 rank_search.py 会自动做 BM25 + 向量混合检索。")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
