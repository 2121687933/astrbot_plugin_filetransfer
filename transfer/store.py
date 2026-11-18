"""接收下来的文件落地与索引。

QQ 里发过来的文件统一存到插件数据目录的 ``uploads/``，
按天分子目录，超出保留数量就自动清理最旧的。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import fmt_size, safe_name, unique_path


@dataclass
class StoredFile:
    """一条落地的接收记录。"""

    id: str
    name: str                      # 原始文件名
    path: str                      # 实际落盘路径
    size: int = 0
    time: float = 0.0
    source: str = ""               # qq群:123 / qq私聊:456
    sha256: str = ""

    def as_line(self) -> str:
        return (
            f"#{self.id} {self.name} ｜ {fmt_size(self.size)} ｜ "
            f"{time.strftime('%m-%d %H:%M', time.localtime(self.time or 0))} ｜ {self.source}"
        )


class FileStore:
    """管 uploads 目录 + index.json 的读写。"""

    def __init__(self, base_dir: Path, keep: int = 500) -> None:
        self.base_dir = Path(base_dir)
        self.upload_dir = self.base_dir / "uploads"
        self.index_path = self.base_dir / "index.json"
        self.keep = max(1, int(keep or 500))
        self._items: List[StoredFile] = []
        self._loaded = False

    # ---------------------------------------------------------------- #
    def ensure(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> List[StoredFile]:
        if self._loaded:
            return self._items
        self._loaded = True
        try:
            raw = self.index_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return self._items
        items: List[StoredFile] = []
        if isinstance(data, list):
            for row in data:
                if not isinstance(row, dict):
                    continue
                try:
                    items.append(StoredFile(**{k: row.get(k, "") for k in StoredFile.__dataclass_fields__}))
                except (TypeError, ValueError):
                    continue
        self._items = items
        return self._items

    def save(self) -> None:
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([asdict(i) for i in self._items], ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self.index_path)
        except OSError:
            pass

    # ---------------------------------------------------------------- #
    def next_id(self) -> str:
        items = self.load()
        nums = []
        for item in items:
            try:
                nums.append(int(item.id))
            except (TypeError, ValueError):
                continue
        return str((max(nums) + 1) if nums else 1)

    def dest_for(self, name: str) -> Path:
        """给要来落地的文件挑一个按天分目录的路径。"""
        self.ensure()
        day = time.strftime("%Y%m%d")
        return unique_path(self.upload_dir / day, safe_name(name))

    def add(self, path: Path, name: str, source: str = "", sha256: str = "") -> StoredFile:
        self.ensure()
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        item = StoredFile(
            id=self.next_id(),
            name=safe_name(name),
            path=str(path),
            size=size,
            time=time.time(),
            source=source,
            sha256=sha256,
        )
        self._items.append(item)
        self.save()
        self.trim()
        return item

    def get(self, ident: str) -> Optional[StoredFile]:
        """按 id 取记录。"""
        key = str(ident).strip().lstrip("#")
        for item in self.load():
            if item.id == key:
                return item
        for item in self.load():
            if item.name == key:
                return item
        return None

    def recent(self, limit: int = 10) -> List[StoredFile]:
        items = list(self.load())
        items.sort(key=lambda i: i.time or 0, reverse=True)
        return items[: max(1, int(limit))]

    def trim(self) -> None:
        """条数超了就删最旧的记录和文件。"""
        if len(self._items) <= self.keep:
            return
        keep_set = {id(i) for i in self._items[-self.keep:]}
        dropped = [i for i in self._items if id(i) not in keep_set]
        self._items = [i for i in self._items if id(i) in keep_set]
        for item in dropped:
            try:
                p = Path(item.path)
                if p.is_file() and self.upload_dir in p.resolve(strict=False).parents:
                    p.unlink()
            except (OSError, ValueError, AttributeError):
                pass
        self.save()

    def total_size(self) -> int:
        total = 0
        for item in self.load():
            try:
                total += int(item.size or 0)
            except (TypeError, ValueError):
                continue
        return total
