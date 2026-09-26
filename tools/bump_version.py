"""
版本号推进工具 —— 把「该动哪一位」这件事变成可执行的命令，避免手改出错。

规则（与 Yung 约定，2026-09-27）：
    patch  同一功能的迭代 / 修补       0.6.0 -> 0.6.1     ← 绝大多数情况用这个
    minor  新增功能或新模块            0.6.1 -> 0.7.0
    major  不兼容变更                  0.7.0 -> 1.0.0

注意**版本号只向前走**：已经推送并产出镜像的版本不可回改，
否则使用者无法判断哪个更新。写错方向时只能通过继续递增来修正。

用法：
    python tools/bump_version.py patch
    python tools/bump_version.py minor --dry-run
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(ROOT, "VERSION")

KINDS = ("patch", "minor", "major")


def parse_version(text: str):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", (text or "").strip())
    if not m:
        raise ValueError(f"VERSION 内容不是 MAJOR.MINOR.PATCH 形式：{text!r}")
    return tuple(int(x) for x in m.groups())


def bump(current, kind: str):
    major, minor, patch = current
    if kind == "major":
        return (major + 1, 0, 0)
    if kind == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="推进 FnPulse 版本号")
    ap.add_argument("kind", choices=KINDS,
                    help="patch=同一功能迭代（默认语义）；minor=新功能；major=不兼容变更")
    ap.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = ap.parse_args()

    if not os.path.exists(VERSION_FILE):
        print(f"找不到 {VERSION_FILE}")
        return 1

    with open(VERSION_FILE, encoding="utf-8") as fh:
        raw = fh.read()
    current = parse_version(raw)
    nxt = bump(current, args.kind)

    old = ".".join(map(str, current))
    new = ".".join(map(str, nxt))
    print(f"{args.kind}: {old} -> {new}")

    if args.dry_run:
        return 0

    with open(VERSION_FILE, "w", encoding="utf-8", newline="") as fh:
        fh.write(new + "\n")
    print(f"已写入 {VERSION_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
