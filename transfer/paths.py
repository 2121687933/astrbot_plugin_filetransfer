"""路径安全与文件元信息。

这个模块只做纯本地判断，不碰网络、也不认识 AstrBot，
所以 ``selfcheck.py`` 可以在没有装 AstrBot 的机器上单独跑它。

设计原则：默认拒绝。
没被显式放进白名单的目录一律读不到；就算在白名单里，
命中敏感名单（密钥、.env、authorized_keys 之类）也照样拒绝。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# 单段文件名最长保留字符数（防止生成超长文件名把文件系统搞崩）
MAX_FILENAME_LEN = 120

# 按文件名匹配的敏感名单（不区分大小写）
SENSITIVE_NAME_PATTERNS: Tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "authorized_keys",
    "known_hosts",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    "*secret*",
    "*credential*",
    "*password*",
    "*token*",
)

# 路径里出现这些目录名就直接拒绝（不管文件本身叫什么）
SENSITIVE_DIR_NAMES = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
        ".docker",
        ".config",
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
    }
)

# 默认不让人乱翻的扩展名（可被配置里的 allow_exts 覆盖）
RISKY_EXTS = frozenset(
    {
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".dylib",
        ".exe",
        ".bat",
        ".cmd",
        ".ps1",
        ".com",
        ".scr",
        ".vbs",
    }
)


class PathDenied(Exception):
    """路径被安全策略拒绝。``reason`` 直接可以发给用户看。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class FileMeta:
    """发给 QQ 之前整理出来的文件信息。"""

    path: Path
    name: str
    size: int
    mtime: float
    is_dir: bool = False

    @property
    def size_text(self) -> str:
        return fmt_size(self.size)


def fmt_size(num: float) -> str:
    """把字节数变成人看得懂的字符串。"""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def fmt_time(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return "-"


def normalize_roots(raw_roots: Optional[Iterable[str]]) -> List[Path]:
    """把配置里写的目录列表解析成绝对路径列表，不存在的直接丢掉。"""
    out: List[Path] = []
    seen = set()
    for item in raw_roots or []:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            path = Path(text).expanduser()
            if not path.is_absolute():
                # 相对路径按当前工作目录解析，并明确告警式地保留
                path = Path.cwd() / path
            real = path.resolve(strict=False)
        except (OSError, ValueError, RuntimeError):
            continue
        key = str(real)
        if key in seen:
            continue
        seen.add(key)
        out.append(real)
    return out


def _hit_sensitive(path: Path) -> Optional[str]:
    """检查路径是否命中敏感名单，命中返回原因。"""
    name = path.name.lower()
    for pattern in SENSITIVE_NAME_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return f"命中敏感文件名规则 {pattern}"
    for part in path.parts:
        if part.lower() in SENSITIVE_DIR_NAMES:
            return f"路径里含敏感目录 {part}"
    return None


def resolve_target(raw: str, roots: Sequence[Path], allow_all: bool = False) -> Path:
    """把用户给的路径字符串解析成真实绝对路径，并做白名单校验。

    抛 ``PathDenied`` 表示不安全或不允许，异常里带着能直接回给用户的原因。
    """
    text = (raw or "").strip().strip("'\"")
    if not text:
        raise PathDenied("路径是空的。")

    if text.startswith("~"):
        text = os.path.expanduser(text)

    try:
        candidate = Path(text)
    except (OSError, ValueError) as exc:  # 极端非法字符
        raise PathDenied(f"路径不合法：{exc}") from exc

    if not candidate.is_absolute():
        # 相对路径基准：白名单第一个根目录；没有根目录就不允许相对路径
        if not roots:
            raise PathDenied("没有配置可访问目录，相对路径没法解析。请用绝对路径。")
        candidate = roots[0] / candidate

    try:
        real = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathDenied(f"路径解析失败：{exc}") from exc

    hit = _hit_sensitive(real)
    if hit:
        raise PathDenied(f"这个文件不给传：{hit}。")

    if allow_all:
        return real

    for root in roots:
        try:
            real.relative_to(root)
            return real
        except ValueError:
            continue

    raise PathDenied(
        "这个路径不在允许目录里。管理员可以在插件配置的 allow_dirs 里加目录。"
    )


def safe_name(name: str) -> str:
    """把任意字符串洗成一个安全的文件名（不含路径分隔符、不含控制字符）。"""
    raw = str(name or "").strip()
    # 去掉路径分隔符和各种奇怪空白
    raw = raw.replace("\\", "_").replace("/", "_")
    cleaned = []
    for ch in raw:
        if ch in "\r\n\t":
            cleaned.append("_")
        elif ord(ch) < 32 or ord(ch) == 127:
            continue
        else:
            cleaned.append(ch)
    out = "".join(cleaned).strip(" .")
    if not out:
        out = "file"
    if len(out) > MAX_FILENAME_LEN:
        stem = Path(out).stem[: MAX_FILENAME_LEN - 16]
        suffix = Path(out).suffix[:16]
        out = f"{stem}{suffix}"
    return out


def unique_path(directory: Path, name: str) -> Path:
    """在目录里给 name 找一个不重名的落点，重名就加 (1)(2)..."""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    for i in range(1, 1000):
        candidate = directory / f"{stem}({i}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-{int(time.time())}{suffix}"


def describe(path: Path) -> FileMeta:
    """收集文件元信息。"""
    is_dir = path.is_dir()
    size = 0
    mtime = 0.0
    try:
        if is_dir:
            # 目录不递归统计大小，太慢，先给个 0，打包时才真正算
            mtime = path.stat().st_mtime
        else:
            st = path.stat()
            size = st.st_size
            mtime = st.st_mtime
    except OSError:
        pass
    return FileMeta(path=path, name=path.name or str(path), size=size, mtime=mtime, is_dir=is_dir)


def sha256_file(path: Path, chunk: int = 1 << 20, limit: Optional[int] = None) -> str:
    """算文件 sha256，limit 限制最多读多少字节（防止大文件卡住）。"""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
            total += len(block)
            if limit is not None and total >= limit:
                break
    return digest.hexdigest()


def make_zip(paths: Sequence[Path], out_path: Path) -> Tuple[Path, int]:
    """把若干文件/目录打成一个 zip，返回 (zip 路径, 压缩后大小)。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for item in paths:
            if item.is_dir():
                base = item.parent
                for sub in item.rglob("*"):
                    if not sub.is_file():
                        continue
                    try:
                        zf.write(sub, sub.relative_to(base).as_posix())
                    except (OSError, ValueError):
                        continue
            elif item.is_file():
                try:
                    zf.write(item, item.name)
                except (OSError, ValueError):
                    continue
    return out_path, out_path.stat().st_size if out_path.exists() else 0


def list_dir(path: Path, limit: int = 200) -> Tuple[List[Path], bool]:
    """列目录，目录在前文件在后，返回 (条目, 是否被截断)。"""
    try:
        entries = list(path.iterdir())
    except (OSError, PermissionError):
        return [], False
    dirs = [p for p in entries if p.is_dir()]
    files = [p for p in entries if not p.is_dir()]
    dirs.sort(key=lambda p: p.name.lower())
    files.sort(key=lambda p: p.name.lower())
    merged = dirs + files
    truncated = len(merged) > limit
    return merged[:limit], truncated
