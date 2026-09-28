#!/usr/bin/env python3
"""把技能目录打包成可直接分发的 zip。

为什么不用 7-Zip / 资源管理器压缩：中文文件名必须以 UTF-8 写入**并置位 zip 的
UTF-8 标志（general purpose bit 11, 0x800）**。实测 7-Zip 写出的包不带该标志，
Windows 资源管理器/部分解压工具会按系统 ANSI 代码页（中文 Windows 即 GBK）解码，
结果解压出来的「宪法相关法」变成乱码目录名，甚至因两个不同的 UTF-8 名解码后撞车
而报「文件已存在」。Python 的 zipfile 对非 ASCII 名会正确置位，所以打包走它。

用法
  python scripts/pack_skill.py
  python scripts/pack_skill.py --out D:\\china-law-skill-full.zip
  python scripts/pack_skill.py --no-corpus            # 只打包脚本/文档，不含语料（约 40 KB）
  python scripts/pack_skill.py --with-vectors        # 连向量索引一起打包（约 103 MB，仅供本机备份）
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

SKIP_DIRS = {"__pycache__", ".git", "downloads", "work", "outputs"}
SKIP_FILES = {"vectors.npz", "vectors.meta.json", ".DS_Store"}
SKIP_SUFFIX = (".pyc", ".pyo")
ARC_ROOT = "china-law"          # 压缩包内的顶层目录名 = 技能目录名


def should_skip(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    if path.name in SKIP_FILES or path.name.endswith(SKIP_SUFFIX):
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    skill_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="打包 china-law 技能为可分发 zip")
    parser.add_argument("--out", default=str(skill_dir.parent / "china-law-skill-full.zip"))
    parser.add_argument("--with-vectors", action="store_true", help="连向量索引一起打包")
    parser.add_argument("--no-corpus", action="store_true", help="不含 references/corpus（只有脚本与文档）")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    out = Path(args.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    files, skipped, total = [], 0, 0
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skill_dir)
        keep_vectors = args.with_vectors and rel.name in {"vectors.npz", "vectors.meta.json"}
        in_corpus = rel.parts[:2] == ("references", "corpus")
        if (should_skip(rel) and not keep_vectors) or (args.no_corpus and in_corpus):
            skipped += 1
            continue
        files.append((path, Path(ARC_ROOT) / rel))
        total += path.stat().st_size

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, arc in files:
            zf.write(src, arc.as_posix())

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"已打包 {len(files)} 个文件（跳过 {skipped} 个：向量索引 / 缓存）")
    print(f"原始 {total / 1024 / 1024:.1f} MB → {out}（{size_mb:.1f} MB）")
    with zipfile.ZipFile(out) as zf:
        flags = [i.flag_bits & 0x800 for i in zf.infolist() if not i.filename.isascii()]
        longest = max((len(i.filename.encode("utf-8")) for i in zf.infolist()), default=0)
    print(f"非 ASCII 条目 {len(flags)} 个，全部带 UTF-8 标志: {all(flags)}；最长条目名 {longest} 字节")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(main())
