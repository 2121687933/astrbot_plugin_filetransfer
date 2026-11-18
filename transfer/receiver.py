"""接收端的另一条腿：把 QQ 里发过来的文件扒下来落到服务器。

QQ 文件消息在事件里是 ``File`` 段，字段各家实现不太一样：

- ``url``：临时下载地址，最通用，但会过期
- ``file``：OneBot 实现侧的本地缓存路径，只在同一台机器上才有效
- ``name``：原始文件名

所以策略是「能读到本地就用本地，读不到再下载」，两条都失败才报错。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .paths import fmt_size, safe_name, sha256_file
from .store import FileStore


@dataclass
class Incoming:
    """从事件里扒出来的一个文件。"""

    name: str = ""
    url: str = ""
    local: str = ""
    size: int = 0


def _comp_type(comp: Any) -> str:
    """组件的类型名，兼容 type 字段是枚举 / 字符串两种情况。"""
    raw = getattr(comp, "type", None)
    if raw is None:
        return comp.__class__.__name__.lower()
    text = getattr(raw, "value", raw)
    return str(text).lower()


def iter_incoming(event: Any) -> List[Incoming]:
    """扫一遍消息里的所有文件段。"""
    found: List[Incoming] = []
    obj = getattr(event, "message_obj", None)
    chain = getattr(obj, "message", None) if obj is not None else None
    if not chain:
        return found
    try:
        comps = list(chain)
    except TypeError:
        return found

    for comp in comps:
        kind = _comp_type(comp)
        if kind != "file":
            continue
        name = ""
        url = ""
        local = ""
        for attr in ("name", "file_name"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        for attr in ("url", "file_url"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value.strip():
                url = value.strip()
                break
        for attr in ("file", "file_", "path"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value.strip():
                local = value.strip()
                break
        if not name and local:
            name = os.path.basename(local.replace("\\", "/"))
        if not name and url:
            name = os.path.basename(url.split("?")[0])
        found.append(Incoming(name=name or "未命名文件", url=url, local=local))
    return found


async def _resolve_local(comp_local: str) -> Optional[Path]:
    """判断 OneBot 实现给的本地路径在这台机器上到底可不可读。"""
    if not comp_local:
        return None
    try:
        path = Path(comp_local)
        if path.is_file():
            return path
    except (OSError, ValueError):
        return None
    return None


async def _download(url: str, dest: Path, timeout: int = 300) -> Optional[Path]:
    """下载到 dest，成功返回路径。"""
    if not url:
        return None
    try:
        import aiohttp
    except ImportError:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        fh.write(chunk)
                os.replace(tmp, dest)
                return dest
    except Exception:  # noqa: BLE001
        return None


async def save_incoming(
    event: Any,
    store: FileStore,
    source: str,
    rename: str = "",
    max_mb: int = 0,
) -> Tuple[List[str], List[str]]:
    """把事件里的文件全部存下来。

    返回 (成功的人话描述列表, 失败原因列表)。
    """
    ok_lines: List[str] = []
    fails: List[str] = []
    items = iter_incoming(event)
    if not items:
        return [], ["这条消息里没找到文件。"]

    for idx, item in enumerate(items):
        name = safe_name(rename) if rename and len(items) == 1 else safe_name(item.name)
        local = await _resolve_local(item.local)

        if local is not None:
            try:
                size = local.stat().st_size
            except OSError:
                size = 0
            if max_mb > 0 and size > max_mb * 1024 * 1024:
                fails.append(f"{name}：{fmt_size(size)} 超过保存上限 {max_mb}MB")
                continue
            dest = store.dest_for(name)
            try:
                # 同一块盘就搬过去，跨盘就复制
                try:
                    os.replace(str(local), str(dest))
                except OSError:
                    import shutil

                    shutil.copy2(str(local), str(dest))
            except OSError as exc:
                fails.append(f"{name}：落地失败 {exc}")
                continue
            digest = ""
            try:
                digest = sha256_file(dest, limit=64 * 1024 * 1024)
            except OSError:
                pass
            rec = store.add(dest, name, source=source, sha256=digest)
            ok_lines.append(f"#{rec.id} {name}（{fmt_size(rec.size)}）→ {rec.path}")
            continue

        # 本地读不到，走 URL 下载
        if not item.url:
            fails.append(f"{name}：既没有本地路径也没有下载地址")
            continue
        dest = store.dest_for(name)
        got = await _download(item.url, dest)
        if got is None:
            fails.append(f"{name}：下载失败（链接可能过期了，让 TA 再发一次）")
            continue
        digest = ""
        try:
            digest = sha256_file(got, limit=64 * 1024 * 1024)
        except OSError:
            pass
        rec = store.add(got, name, source=source, sha256=digest)
        ok_lines.append(f"#{rec.id} {name}（{fmt_size(rec.size)}）→ {rec.path}")

    return ok_lines, fails
