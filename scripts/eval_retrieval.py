#!/usr/bin/env python3
"""检索质量评测：把「我觉得还不够准」变成可比较的数字。

读 eval/queries.jsonl（每题带 must_include 法条），跑一遍真实检索管线
（直接调 rank_search 的 load_documents / build_terms / search，不另写一套，
否则测的就不是线上那条路），输出 recall@k 与命中位次。

用法
  python scripts/eval_retrieval.py                  # 默认 BM25-only
  python scripts/eval_retrieval.py --top 20 --miss   # 看更多位次 + 打印未命中题目
  python scripts/eval_retrieval.py --layers current historical   # 换检索层
  python scripts/eval_retrieval.py --json            # 机器可读

读数的三条规矩（写死在输出里，避免事后找理由）
  1. recall@k 看的是**绝对水平**：如果 BM25-only 已经 95%，那"向量提升很小"
     说明题目太容易，而不是向量没用。
  2. 分题型看：单条映射题（口语问法→一条法条）与概念/跨法题的可比性不同，
     后者才是语义检索的强项场。
  3. 先验证评测集自身：must_include 里的（法律全称+条号）必须能在语料里找到，
     否则算"无效题"单独报出来，不计进分母。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rank_search as R  # noqa: E402  复用真实检索管线，不另写一套
import clause_rerank as CR  # noqa: E402  候选级款拆分重排
from query_rewrite import DEFAULT_CACHE, load_cache, rewrite, save_cache  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
CORPUS_ROOT = SKILL_DIR / "references" / "corpus"
EVAL_PATH = SKILL_DIR / "eval" / "queries.jsonl"
HIT_KS = (1, 3, 5, 8, 10, 20, 50)


def load_eval(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        row.setdefault("acceptable", [])
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索质量评测（recall@k / 命中位次）")
    parser.add_argument("--eval", default=str(EVAL_PATH), help="评测集 JSONL")
    parser.add_argument("--top", type=int, default=50, help="每个问题取前多少条（默认 50，覆盖 recall@50）")
    parser.add_argument("--layers", nargs="+", default=["current"], help="检索层（默认 current）")
    parser.add_argument("--no-expansions", action="store_true", help="关掉 query-expansions 扩展表，只跑原始词项")
    parser.add_argument("--vectors", action="store_true", help="启用向量索引并做 RRF 融合（需先建好索引）")
    parser.add_argument("--rewrite", action="store_true", help="启用查询改写（多路召回 + 跨查询 RRF）")
    parser.add_argument("--rewrite-n", type=int, default=3, help="每题生成几条改写（默认 3）")
    parser.add_argument("--rewrite-model", default=None, help="改写用的 Ollama 模型")
    parser.add_argument(
        "--rewrite-by-id", default=None,
        help="用 id→改写列表 的 JSON 文件替代模型改写（对照实验：比较不同改写来源）",
    )
    parser.add_argument("--clause-rerank", action="store_true", help="对候选做款级重排（需向量索引）")
    parser.add_argument("--rerank-pool", type=int, default=20, help="重排前的候选池大小（默认 20）")
    parser.add_argument(
        "--rerank-mode", choices=("fuse", "replace"), default="fuse",
        help="fuse=原顺序与新顺序做 RRF（稳）；replace=直接用重排顺序（激进）",
    )
    parser.add_argument("--miss", action="store_true", help="打印未命中题目")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    manifest = R.load_manifest(CORPUS_ROOT)
    documents = R.load_documents(CORPUS_ROOT, manifest, None, tuple(args.layers))
    expansions = {} if args.no_expansions else R.load_expansions(SKILL_DIR)
    entries = load_eval(Path(args.eval).expanduser())

    # 与 rank_search 主流程一致：索引缺失或覆盖不足就退回纯 BM25，并如实标注
    index = R.load_vector_index(CORPUS_ROOT) if args.vectors else None
    vector_note = ""
    if args.vectors:
        if index is None:
            vector_note = "索引不存在，退回纯 BM25"
            index = None
        else:
            keys = {R.doc_key(doc) for doc in documents}
            covered = sum(1 for k in keys if k in set(index["keys"])) / max(1, len(keys))
            if covered < 0.99:
                vector_note = f"索引覆盖率仅 {covered:.0%}，退回纯 BM25"
                index = None
            else:
                vector_note = f"索引覆盖 {covered:.0%}，模型 {index['meta'].get('model')}"

    # 评测集自检：参考答案必须真实存在于语料里
    available = {(doc[0], doc[2]) for doc in documents}
    invalid: list[str] = []
    for row in entries:
        for ref in row["must_include"]:
            if (ref["law"], ref["article"]) not in available:
                invalid.append(f"{row['id']}: {ref['law']} {ref['article']}")

    per_query: list[dict] = []
    cache = load_cache(DEFAULT_CACHE) if args.rewrite else {}
    by_id = (
        json.loads(Path(args.rewrite_by_id).expanduser().read_text(encoding="utf-8"))
        if args.rewrite_by_id else None
    )
    rewritten = 0
    for row in entries:
        queries = [row["question"]]
        if args.rewrite:
            if by_id is not None:
                cache[row["question"]] = by_id.get(row["id"], [])
            elif row["question"] not in cache:
                kwargs = {"n": args.rewrite_n}
                if args.rewrite_model:
                    kwargs["model"] = args.rewrite_model
                cache[row["question"]] = rewrite(row["question"], **kwargs)
                rewritten += 1
            queries += cache[row["question"]]

        lists: list[list[tuple]] = []
        for text in queries:
            terms, _ = R.build_terms(text, expansions)
            bm25_ranked = R.search(documents, terms, False)
            if index is not None:
                hits = R.vector_ranking(index, text, R.DEFAULT_OLLAMA)
                lists.append([doc for _b, _v, _c, doc in R.fuse(documents, bm25_ranked, hits)])
            else:
                lists.append([doc for _score, doc in bm25_ranked])

        pool = max(args.top, args.rerank_pool) if args.clause_rerank else args.top
        if len(lists) == 1:
            ranked = lists[0][:pool]
        else:
            # 跨查询 RRF：每条召回路各自贡献 1/(60+排名)
            scores: dict[str, float] = {}
            by_key: dict[str, tuple] = {}
            for one in lists:
                for rank, doc in enumerate(one[: max(pool, 100)], start=1):
                    key = R.doc_key(doc)
                    by_key[key] = doc
                    scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
            ranked = [by_key[k] for k, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:pool]

        if args.clause_rerank and index is not None:
            # 只重排候选池头部（默认前 20），尾部保持原顺序——重排是"精确化"手段，
            # 对长尾做重排只是引入噪声，还更慢。
            head = ranked[: args.rerank_pool]
            tail = ranked[args.rerank_pool :]
            reranked = CR.rerank(
                index, CORPUS_ROOT, head, row["question"], R.DEFAULT_OLLAMA,
                len(head), R.embed_texts,
            )
            if args.rerank_mode == "replace":
                ranked = reranked + tail
            else:
                # 两路 RRF：原召回顺序 + 款级重排顺序。替换式会把"碰巧有一款字面
                # 相近"的错条文一起提上来（实测开发集 @8 掉 10 点），融合能保住原顺序。
                fused: dict[str, float] = {}
                pool_by_key: dict[str, tuple] = {}
                for src in (head, reranked):
                    for rank, doc in enumerate(src, start=1):
                        key = R.doc_key(doc)
                        pool_by_key[key] = doc
                        fused[key] = fused.get(key, 0.0) + 1.0 / (60 + rank)
                ranked = [pool_by_key[k] for k, _ in sorted(fused.items(), key=lambda kv: -kv[1])] + tail
        must = [(r["law"], r["article"]) for r in row["must_include"]]
        acceptable = {(r["law"], r["article"]) for r in row["acceptable"]}
        positions = []
        for law, article in must:
            pos = next((i + 1 for i, doc in enumerate(ranked) if (doc[0], doc[2]) == (law, article)), None)
            positions.append(pos)
        # 必含条文的第一个命中位次；acceptable 也计入"命中"（不额外加分，只是不漏报错）
        first = min([p for p in positions if p] or [None]) if any(positions) else None
        if first is None:
            first = next(
                (i + 1 for i, doc in enumerate(ranked) if (doc[0], doc[2]) in acceptable), None
            )
        per_query.append(
            {
                "id": row["id"],
                "kind": row.get("kind", ""),
                "question": row["question"],
                "must": len(must),
                "hit": sum(1 for p in positions if p),
                "positions": positions,
                "first": first,
            }
        )

    valid = [q for q in per_query if q["must"]]
    all_positions = [p for q in valid for p in q["positions"]]
    if index is not None:
        config_name = "BM25 + 向量 + 查询改写（多路 RRF）" if args.rewrite else "BM25 + 向量（RRF）"
    elif args.rewrite:
        config_name = "BM25 + 查询改写（多路 RRF）"
    elif args.no_expansions:
        config_name = "BM25（无扩展表）"
    else:
        config_name = "BM25-only"
    if args.clause_rerank and index is not None:
        config_name += f" + 款级重排（{args.rerank_mode}）"
    if args.rewrite and by_id is None:
        save_cache(DEFAULT_CACHE, cache)
    report = {
        "配置": config_name,
        "向量说明": vector_note,
        "改写": (
            f"外部改写文件（{len(by_id)} 条），覆盖 {sum(1 for r in entries if cache.get(r['question']))}/{len(entries)} 题"
            if by_id is not None else
            (f"每题 {args.rewrite_n} 条，本次新生成 {rewritten} 题（其余读缓存）" if args.rewrite else "未启用")
        ),
        "层": list(args.layers),
        "语料条数": len(documents),
        "题目数": len(per_query),
        "必含条文总数": len(all_positions),
        "无效题": invalid,
        # 条文级：全部必含条文里，有多少条进了前 k 名
        "recall@k（条文级）": {
            f"@{k}": round(sum(1 for p in all_positions if p and p <= k) / max(1, len(all_positions)), 4)
            for k in HIT_KS
        },
        # 题目级：有多少题至少命中一条必含条文
        "题目命中率@k": {
            f"@{k}": round(
                sum(1 for q in valid if q["first"] is not None and q["first"] <= k) / max(1, len(valid)), 4
            )
            for k in HIT_KS
        },
        # 题目级：必含条文全部进前 k 的题目比例（多必含题目才与上面不同）
        "题目全中率@k": {
            f"@{k}": round(
                sum(1 for q in valid if all(p and p <= k for p in q["positions"])) / max(1, len(valid)), 4
            )
            for k in HIT_KS
        },
        "MRR@8": round(
            sum(1.0 / q["first"] for q in valid if q["first"] is not None and q["first"] <= 8)
            / max(1, len(valid)),
            3,
        ),
        "MRR@20": round(
            sum(1.0 / q["first"] for q in valid if q["first"] is not None and q["first"] <= 20)
            / max(1, len(valid)),
            3,
        ),
        "按题型（至少命中一条@8）": {
            kind: f"{sum(1 for q in valid if q['kind'] == kind and q['first'] is not None and q['first'] <= 8)}/{total}"
            for kind, total in Counter(q["kind"] for q in valid).items()
        },
        "命中位次分布": dict(Counter(
            ("未命中" if q["first"] is None else f"第{q['first']}位") for q in valid
        ).most_common()),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    print(
        f"配置：{report['配置']}　层：{'/'.join(args.layers)}　"
        f"语料：{report['语料条数']} 条　题目：{report['题目数']}　必含条文：{report['必含条文总数']}"
    )
    if report.get("向量说明"):
        print(f"向量：{report['向量说明']}")
    if args.rewrite:
        print(f"改写：{report['改写']}")
    if args.clause_rerank:
        print(f"重排：款级 · {args.rerank_mode} 模式（候选池 {args.rerank_pool}）")
    if invalid:
        print(f"\n⚠ 无效题 {len(invalid)} 条（参考答案不在语料里，未计入分母）：")
        for line in invalid:
            print("   -", line)
    print("\nrecall@k（条文级：必含条文的命中比例）：")
    for k, v in report["recall@k（条文级）"].items():
        print(f"   {k:<4} {v * 100:5.1f}%")
    print("\n题目命中率@k（至少命中一条）：")
    for k, v in report["题目命中率@k"].items():
        print(f"   {k:<4} {v * 100:5.1f}%")
    print("\n题目全中率@k（必含条文全进前 k）：")
    for k, v in report["题目全中率@k"].items():
        print(f"   {k:<4} {v * 100:5.1f}%")
    print(f"\nMRR@8：{report['MRR@8']}")
    print("\n按题型（至少命中一条@8）：")
    for kind, v in report["按题型（至少命中一条@8）"].items():
        print(f"   {kind:<8} {v}")
    print("\n命中位次分布：", report["命中位次分布"])
    if args.miss:
        print("\n未命中题目：")
        for q in valid:
            if q["first"] is None:
                print(f"   ✗ {q['id']:<13} {q['question'][:42]}（必含 {q['must']} 条，命中 0）")
            elif q["hit"] < q["must"]:
                print(f"   ~ {q['id']:<13} {q['question'][:42]}（必含 {q['must']} 条，仅命中 {q['hit']}，首位第 {q['first']}）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
