#!/usr/bin/env python3
"""把口语问题改写成"法条用语"的检索表述，供多路召回使用。

为什么需要：30 题实测里那 6 道未命中题，失败形态高度一致——口语词与目标法条
**没有任何字面重叠**：

    押金 ↔ 违约责任 / 全面履行
    过了几年 ↔ 诉讼时效期间为三年
    转账赚手续费 ↔ 帮助信息网络犯罪活动
    杀熟 ↔ 自动化决策的差别待遇
    拖欠工资 ↔ 未及时足额支付劳动报酬

这类缺口"多读几条"救不回来（@20→@50 完全持平），"手工喂同义词"也救不回来
（现有扩展表全表只值 +3.3 点）。要么改查询，要么换表示——这个脚本做前者。

设计要点
  · **关闭思考模式（think=false）**：实测同一问题 21.4s → 1.4s，且输出更干净
    （开着思考模式会把推理过程一起吐出来，JSON 常被淹没）；
  · 只要求 JSON 数组，解析失败就退化成按行/按标点切分——绝不因为模型不听话
    而中断整轮评测；
  · 结果按问题缓存，重复评测不再重复调用模型。

用法
  python scripts/query_rewrite.py 公司拖欠我三个月工资
  python scripts/query_rewrite.py --n 5 房东不退我押金 --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rank_search import DEFAULT_OLLAMA, http_json  # noqa: E402

DEFAULT_MODEL = "Qwen3-4B-Q4_K_M.gguf:latest"
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "downloads" / ".rewrite_cache.json"

PROMPT = (
    "用户问：{q}\n"
    "请给出 {n} 条用于检索中国法律法规的表述，尽量使用法条中的正式用语"
    "（例如把「押金」「杀熟」「过了几年」换成法律术语）。\n"
    "只输出 JSON 数组，例如 [\"表述一\",\"表述二\"]，不要任何解释、不要 Markdown。"
)


def parse_list(text: str, n: int) -> list[str]:
    """从模型输出里取出表述列表；解析失败就退化成按行切分，不抛异常。"""
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            items = json.loads(text[start : end + 1])
            if isinstance(items, list):
                return [str(x).strip() for x in items if str(x).strip()][:n]
        except ValueError:
            pass
    parts = re.split(r"[\n；;]+", text)
    return [p.strip(" \t-\"'、,，0123456789.）)") for p in parts if p.strip()][:n]


def rewrite(
    question: str,
    n: int = 3,
    model: str = DEFAULT_MODEL,
    base: str = DEFAULT_OLLAMA,
    timeout: int = 120,
) -> list[str]:
    """把口语问题改写成 n 条法条用语表述（失败返回空列表，由调用方决定降级）。"""
    payload = {
        "model": model,
        "prompt": PROMPT.format(q=question, n=n),
        "stream": False,
        "think": False,  # 关键：关掉思考模式，21.4s → 1.4s
        "options": {"temperature": 0, "num_predict": 300},
    }
    try:
        data = http_json(base + "/api/generate", payload, timeout=timeout, retries=2)
    except Exception:  # noqa: BLE001 - 模型不可用时安静降级，不中断检索
        return []
    return [x for x in parse_list(data.get("response") or "", n) if x != question]


def load_cache(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(path: Path, cache: dict[str, list[str]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="口语问题 → 法条用语表述（多路召回用）")
    parser.add_argument("question", nargs="+", help="用户的口语问题")
    parser.add_argument("--n", type=int, default=3, help="生成几条（默认 3）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama 模型名")
    parser.add_argument("--base", default=DEFAULT_OLLAMA, help="Ollama 地址")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    question = " ".join(args.question)
    items = rewrite(question, n=args.n, model=args.model, base=args.base)
    if args.json:
        print(json.dumps({"question": question, "rewrites": items}, ensure_ascii=False, indent=1))
    elif items:
        print(f"原问题：{question}")
        for i, text in enumerate(items, 1):
            print(f"  {i}. {text}")
    else:
        print(f"改写失败（模型不可用或输出无法解析）：{question}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
