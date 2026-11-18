"""离线自检：不联网、不依赖 AstrBot，先把本地逻辑跑一遍。

在插件目录里执行：

    python selfcheck.py

它会检查路径白名单、敏感文件拦截、文件名清洗、接收索引这些纯本地逻辑。
全绿再去 AstrBot 里装，出问题就只可能出在网络/平台那一侧。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Windows 控制台默认 GBK，直接打印中文标点会炸，这里统一转 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

from transfer.paths import (  # noqa: E402
    PathDenied,
    fmt_size,
    list_dir,
    make_zip,
    normalize_roots,
    resolve_target,
    safe_name,
    sha256_file,
)
from transfer.store import FileStore  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [X ] {label} {detail}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ft_selfcheck_"))
    root = tmp / "allowed"
    outside = tmp / "outside"
    root.mkdir()
    outside.mkdir()

    (root / "报告.txt").write_text("hello", encoding="utf-8")
    (root / "big.bin").write_bytes(b"x" * 2048)
    (root / "sub").mkdir()
    (root / "sub" / "深一点.txt").write_text("deep", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "server.key").write_text("PRIVATE", encoding="utf-8")
    (outside / "secret.txt").write_text("nope", encoding="utf-8")

    roots = normalize_roots([str(root)])

    print("\n[1] 路径白名单")
    try:
        got = resolve_target(str(root / "报告.txt"), roots, allow_all=False)
        check("允许目录内文件可解析", got == (root / "报告.txt").resolve())
    except PathDenied as exc:
        check("允许目录内文件可解析", False, exc.reason)

    try:
        resolve_target(str(outside / "secret.txt"), roots, allow_all=False)
        check("目录外文件被拒绝", False, "居然通过了")
    except PathDenied:
        check("目录外文件被拒绝", True)

    try:
        resolve_target(str(root / ".." / "outside" / "secret.txt"), roots, allow_all=False)
        check(".. 穿越被拒绝", False, "居然通过了")
    except PathDenied:
        check(".. 穿越被拒绝", True)

    print("\n[2] 敏感文件拦截")
    for name in (".env", "server.key"):
        try:
            resolve_target(str(root / name), roots, allow_all=True)
            check(f"{name} 被拒绝", False, "居然通过了")
        except PathDenied:
            check(f"{name} 被拒绝", True)

    print("\n[3] 文件名清洗")
    cases = {
        "../../etc/passwd": ("_.._etc_passwd", True),
        "a/b\\c.txt": ("a_b_c.txt", True),
        "  report.pdf  ": ("report.pdf", True),
    }
    for raw, (expect, _) in cases.items():
        got = safe_name(raw)
        check(f"safe_name({raw!r}) -> {got!r}", got == expect, f"期望 {expect!r}")
    check("空名兜底", safe_name("") == "file")
    check("超长名截断", len(safe_name("啊" * 500)) <= 120)

    print("\n[4] 大小与时间格式化")
    check("fmt_size(0)", fmt_size(0) == "0 B")
    check("fmt_size(2048)", fmt_size(2048) == "2.0 KB", fmt_size(2048))
    check("fmt_size(1.5MB)", fmt_size(int(1.5 * 1024 * 1024)) == "1.5 MB")

    print("\n[5] 目录与打包")
    entries, truncated = list_dir(root, limit=50)
    check("list_dir 拿到条目", len(entries) >= 4, f"实际 {len(entries)}")
    check("list_dir 目录排前面", entries[0].is_dir() if entries else False)
    small, truncated2 = list_dir(root, limit=2)
    check("list_dir 截断生效", len(small) == 2 and truncated2 is True)

    zip_path, zip_size = make_zip([root / "sub"], tmp / "out.zip")
    check("make_zip 生成文件", zip_path.is_file() and zip_size > 0)

    print("\n[6] 哈希")
    digest = sha256_file(root / "报告.txt")
    check("sha256 长度", len(digest) == 64)
    check("sha256 稳定", digest == sha256_file(root / "报告.txt"))

    print("\n[7] 接收索引")
    store = FileStore(tmp / "data", keep=3)
    store.ensure()
    dest = store.dest_for("测试.txt")
    dest.write_text("x", encoding="utf-8")
    rec = store.add(dest, "测试.txt", source="群1 / 用户2")
    check("add 拿到 id", rec.id == "1", rec.id)
    check("get 按 id", store.get("#1") is not None and store.get("#1").name == "测试.txt")
    check("最近列表", len(store.recent(5)) == 1)

    for i in range(2, 6):
        d = store.dest_for(f"f{i}.txt")
        d.write_text("y", encoding="utf-8")
        store.add(d, f"f{i}.txt", source="test")
    check("keep 上限生效", len(store.load()) == 3, f"实际 {len(store.load())}")
    check("index.json 落盘", (tmp / "data" / "index.json").is_file())

    print("\n[8] 目录浏览序号" )
    check("序号解析辅助存在", hasattr(store, "get"))

    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    if FAIL:
        print("有失败项，先修 selfcheck 里报的，再去 AstrBot 里装。")
        return 1
    print("本地逻辑没问题，可以装进 AstrBot 了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
