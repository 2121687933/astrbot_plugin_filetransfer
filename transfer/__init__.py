"""文件传输的各个零件。

- ``paths``    路径安全、大小格式化、打包
- ``senders``  把文件送到 QQ / 其他平台
- ``receiver`` 把聊天里收到的文件落到服务器
- ``store``    接收目录与索引
"""

from .paths import (  # noqa: F401
    FileMeta,
    PathDenied,
    describe,
    fmt_size,
    fmt_time,
    list_dir,
    make_zip,
    normalize_roots,
    resolve_target,
    safe_name,
    sha256_file,
    unique_path,
)
from .store import FileStore, StoredFile  # noqa: F401

__all__ = [
    "FileMeta",
    "PathDenied",
    "describe",
    "fmt_size",
    "fmt_time",
    "list_dir",
    "make_zip",
    "normalize_roots",
    "resolve_target",
    "safe_name",
    "sha256_file",
    "unique_path",
    "FileStore",
    "StoredFile",
]
