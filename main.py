"""文件传输插件（AstrBot）。

让跑在服务器上的机器人把本机文件发到 QQ（群聊 / 私聊），
也能把 QQ 里发过来的文件收下来落到服务器。

一句话用法：
    服务器 → QQ    /取  /path/to/report.pdf
    QQ → 服务器   发文件时顺手打 /存
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from astrbot.api import logger                       # noqa: E402
from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402
from astrbot.api.star import Context, Star           # noqa: E402

try:                                                  # 有的版本有 register，有的靠 metadata.yaml
    from astrbot.api.star import register as _real_register  # noqa: E402
except ImportError:                                   # pragma: no cover
    _real_register = None


def _noop_deco(cls):
    return cls


def register(*args, **kwargs):
    """不同版本 register 的参数个数不一样，这里兜住异常，别让装饰失败。"""
    if _real_register is None:
        return _noop_deco
    try:
        deco = _real_register(*args, **kwargs)
    except Exception:                                 # noqa: BLE001
        return _noop_deco
    return deco if callable(deco) else _noop_deco


from transfer.paths import (                          # noqa: E402
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
)
from transfer.receiver import save_incoming           # noqa: E402
from transfer.senders import (                        # noqa: E402
    SendCfg,
    Target,
    current_target,
    deliver,
)
from transfer.store import FileStore                  # noqa: E402

PLUGIN_NAME = "astrbot_plugin_filetransfer"

# 单次 /取 最多发几个文件，防止被一次刷爆
MAX_FILES_PER_REQUEST = 10


def _resolve_data_dir(plugin_name: str) -> Path:
    """找 AstrBot 的插件数据目录，找不到就退回插件自己的 data/。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path  # type: ignore

        return Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
    except Exception:                                 # noqa: BLE001
        pass
    try:
        from astrbot.api.star import StarTools  # type: ignore

        return Path(StarTools.get_data_dir(plugin_name))
    except Exception:                                 # noqa: BLE001
        pass
    return Path(__file__).resolve().parent / "data"


def _split_args(text: str) -> List[str]:
    """把命令后面的参数切开，支持引号包住带空格的路径。"""
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        import shlex

        parts = shlex.split(raw, posix=False)
    except ValueError:
        parts = raw.split()
    out: List[str] = []
    for item in parts:
        token = item.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
            token = token[1:-1]
        if token:
            out.append(token)
    return out


def _strip_command(text: str, names: Sequence[str]) -> str:
    """去掉消息开头的命令词，返回后面的参数原文。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    head, _, tail = raw.partition(" ")
    bare = head.lstrip("/／!#！")
    if bare in names or head in names:
        return tail.strip()
    # 命令和参数之间没有空格（比如 /取/path）
    for name in names:
        for prefix in (f"/{name}", f"／{name}"):
            if raw.startswith(prefix) and len(raw) > len(prefix):
                return raw[len(prefix):].strip()
    return raw


@register(PLUGIN_NAME, "大肥鱼", "把服务器上的文件发到 QQ，也能收 QQ 文件", "1.0.0")
class FileTransferPlugin(Star):
    """服务器 ⇄ QQ 的文件搬运工。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = _resolve_data_dir(PLUGIN_NAME)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.store = FileStore(self.data_dir, keep=self._conf_int("keep_files", 500))
        self._dir_cache: Dict[str, List[Path]] = {}
        self._rate: Dict[str, List[float]] = {}
        self._tmp_dir = self.data_dir / "tmp"
        self._stats = {"sent": 0, "received": 0, "failed": 0}
        try:
            self.store.load()
        except Exception:                             # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def _conf(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:                             # noqa: BLE001
            return default
        return default if value is None else value

    def _conf_int(self, key: str, default: int) -> int:
        try:
            return int(self._conf(key, default))
        except (TypeError, ValueError):
            return default

    def _conf_bool(self, key: str, default: bool) -> bool:
        value = self._conf(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "开")
        return bool(value)

    def _conf_list(self, key: str) -> List[str]:
        value = self._conf(key, [])
        if isinstance(value, str):
            return [v.strip() for v in value.replace(",", "\n").splitlines() if v.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if str(v).strip()]
        return []

    def _roots(self) -> List[Path]:
        roots = normalize_roots(self._conf_list("allow_dirs"))
        # 插件自己的数据目录永远可读，方便收下来的文件再发出去
        try:
            if self.data_dir not in roots:
                roots.append(self.data_dir.resolve(strict=False))
        except OSError:
            pass
        return roots

    def _send_cfg(self) -> SendCfg:
        return SendCfg(
            mode=str(self._conf("send_mode", "auto") or "auto"),
            onebot_url=str(self._conf("onebot_http_url", "") or ""),
            onebot_token=str(self._conf("onebot_token", "") or ""),
            force_base64=self._conf_bool("force_base64", False),
            max_mb=self._conf_int("max_file_mb", 100),
        )

    # ------------------------------------------------------------------ #
    # 权限与限流
    # ------------------------------------------------------------------ #
    def _is_allowed(self, event: AstrMessageEvent) -> Tuple[bool, str]:
        if not self._conf_bool("admin_only", True):
            return True, ""
        try:
            if event.is_admin():
                return True, ""
        except Exception:                             # noqa: BLE001
            pass
        gid, uid = "", ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:                             # noqa: BLE001
            gid = ""
        try:
            uid = str(event.get_sender_id() or "")
        except Exception:                             # noqa: BLE001
            uid = ""
        if gid and gid in self._conf_list("allowed_groups"):
            return True, ""
        if uid and uid in self._conf_list("allowed_users"):
            return True, ""
        return False, "这个命令只给管理员用。想放开的话，把群号或 QQ 号填进插件配置的 allowed_groups / allowed_users。"

    def _rate_ok(self, event: AstrMessageEvent) -> bool:
        limit = self._conf_int("rate_limit_per_min", 10)
        if limit <= 0:
            return True
        key = str(getattr(event, "unified_msg_origin", "") or "default")
        now = time.time()
        bucket = self._rate.setdefault(key, [])
        bucket[:] = [t for t in bucket if now - t < 60.0]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        # 顺手清掉太老的 key，别让字典无限长
        if len(self._rate) > 200:
            for k in list(self._rate):
                if not self._rate[k]:
                    self._rate.pop(k, None)
        return True

    def _guard(self, event: AstrMessageEvent) -> Optional[str]:
        if not self._rate_ok(event):
            return "太频繁了，等一分钟再来。"
        ok, reason = self._is_allowed(event)
        if not ok:
            return reason
        return None

    # ------------------------------------------------------------------ #
    # 公共小工具
    # ------------------------------------------------------------------ #
    def _cache_dir(self, event: AstrMessageEvent, entries: List[Path]) -> None:
        key = str(getattr(event, "unified_msg_origin", "") or "default")
        self._dir_cache[key] = list(entries)

    def _pick_cached(self, event: AstrMessageEvent, raw: str) -> Optional[Path]:
        key = str(getattr(event, "unified_msg_origin", "") or "default")
        entries = self._dir_cache.get(key, [])
        token = str(raw).strip().lstrip("#＃")
        if not token.isdigit():
            return None
        idx = int(token) - 1
        if 0 <= idx < len(entries):
            return entries[idx]
        return None

    def _source_label(self, event: AstrMessageEvent) -> str:
        try:
            gid = str(event.get_group_id() or "")
        except Exception:                             # noqa: BLE001
            gid = ""
        try:
            uid = str(event.get_sender_id() or "")
        except Exception:                             # noqa: BLE001
            uid = ""
        if gid:
            return f"群{gid} / 用户{uid}" if uid else f"群{gid}"
        return f"私聊{uid}" if uid else "未知来源"

    def _parse_paths(
        self, event: AstrMessageEvent, tokens: Sequence[str]
    ) -> Tuple[List[Path], List[str]]:
        """把参数解析成文件列表，同时返回一批错误说明。"""
        paths: List[Path] = []
        errors: List[str] = []
        roots = self._roots()
        allow_all = self._conf_bool("allow_all_paths", False)

        for token in tokens:
            picked = self._pick_cached(event, token)
            if picked is not None:
                paths.append(picked)
                continue
            if token.lstrip("#＃").isdigit() and len(token) <= 4:
                # 像是序号但缓存里没有
                errors.append(f"{token}：当前没有目录列表，先 /目录 看一下。")
                continue
            try:
                real = resolve_target(token, roots, allow_all)
            except PathDenied as exc:
                errors.append(f"{token}：{exc.reason}")
                continue
            if not real.exists():
                errors.append(f"{token}：文件不存在。")
                continue
            paths.append(real)
        return paths, errors

    # ------------------------------------------------------------------ #
    # /取  服务器 → 当前会话
    # ------------------------------------------------------------------ #
    async def _impl_get(self, event: AstrMessageEvent, names: Sequence[str]):
        guard = self._guard(event)
        if guard:
            yield event.plain_result(guard)
            return

        raw = _strip_command(event.message_str, names)
        tokens = _split_args(raw)
        if not tokens:
            yield event.plain_result(
                "用法：/取 <服务器路径> [更多路径…]\n"
                "也可以 /目录 看列表，然后 /取 3 直接取第 3 个。"
            )
            return

        paths, errors = self._parse_paths(event, tokens)
        if not paths:
            yield event.plain_result("没找到能发的文件：\n" + "\n".join(errors or ["路径是空的。"]))
            return

        dirs = [p for p in paths if p.is_dir()]
        files = [p for p in paths if p.is_file()]
        if dirs and not files:
            yield event.plain_result(
                "这个是目录，要用 /打包 才发得出去：\n" + "\n".join(f"· {d}" for d in dirs[:5])
            )
            return
        if len(files) > MAX_FILES_PER_REQUEST:
            files = files[:MAX_FILES_PER_REQUEST]
            errors.append(f"一次最多发 {MAX_FILES_PER_REQUEST} 个，剩下的自己再取。")

        target = current_target(event)
        if target is None:
            yield event.plain_result("认不出这个会话，不知道发给谁。")
            return

        items = [(p, safe_name(p.name)) for p in files]
        result = await deliver(event, target, items, self._send_cfg(), logger.info)
        if result.ok:
            self._stats["sent"] += len(items)
            lines = [f"已发送 {len(items)} 个文件："]
            for p in files:
                try:
                    size = fmt_size(p.stat().st_size)
                except OSError:
                    size = "?"
                lines.append(f"· {p.name}（{size}）")
            lines.append(f"通道：{result.strategy}")
            if errors:
                lines.append("另外：" + "；".join(errors))
            yield event.plain_result("\n".join(lines))
        else:
            self._stats["failed"] += 1
            detail = "\n".join(f"· {line}" for line in result.attempts[:6])
            yield event.plain_result(
                f"没发出去。\n{detail}\n"
                "排查顺序：① 机器人是不是 NapCat/Lagrange 接的 OneBot；"
                "② 文件和机器人是不是同一台机器；③ 配置里换个 send_mode 试试。"
            )

    @filter.command("fileget")
    @filter.command("取")
    async def cmd_get(self, event: AstrMessageEvent):
        """把服务器上的文件发到当前会话。"""
        async for item in self._impl_get(event, ("fileget", "取", "fget")):
            yield item

    # ------------------------------------------------------------------ #
    # /目录  浏览
    # ------------------------------------------------------------------ #
    async def _impl_ls(self, event: AstrMessageEvent, names: Sequence[str]):
        guard = self._guard(event)
        if guard:
            yield event.plain_result(guard)
            return

        raw = _strip_command(event.message_str, names)
        tokens = _split_args(raw)
        roots = self._roots()
        allow_all = self._conf_bool("allow_all_paths", False)

        if tokens:
            target_dir, errors = self._parse_paths(event, tokens[:1])
            if errors or not target_dir:
                yield event.plain_result("路径不对：" + "；".join(errors or ["空的"]))
                return
            path = target_dir[0]
        else:
            if not roots:
                yield event.plain_result(
                    "还没配置允许访问的目录。去插件配置的 allow_dirs 里填一个，比如 /root/data。"
                )
                return
            path = roots[0]

        if path.is_file():
            # 直接给了文件就当 /文件信息 用
            lines = [
                f"文件：{path}",
                f"大小：{fmt_size(path.stat().st_size)}",
                f"修改：{fmt_time(path.stat().st_mtime)}",
            ]
            yield event.plain_result("\n".join(lines))
            return
        if not path.is_dir():
            yield event.plain_result(f"{path} 不是一个目录。")
            return

        limit = self._conf_int("list_limit", 60)
        entries, truncated = list_dir(path, limit=limit)
        self._cache_dir(event, entries)

        if not entries:
            yield event.plain_result(f"{path} 是空的。")
            return

        lines = [f"{path}"]
        for i, entry in enumerate(entries, 1):
            try:
                if entry.is_dir():
                    lines.append(f"{i}. 📁 {entry.name}/")
                else:
                    size = fmt_size(entry.stat().st_size)
                    lines.append(f"{i}. {entry.name}  ({size})")
            except OSError:
                lines.append(f"{i}. {entry.name}")
        lines.append("")
        lines.append(f"共 {len(entries)} 项" + ("（已截断）" if truncated else ""))
        lines.append("用 /取 序号 发送，比如 /取 2")

        text = "\n".join(lines)
        if len(text) > 3500:
            text = text[:3500] + "\n…太长了，先 /取 指定文件。"
        yield event.plain_result(text)

    @filter.command("filelist")
    @filter.command("目录")
    async def cmd_ls(self, event: AstrMessageEvent):
        """列目录。"""
        async for item in self._impl_ls(event, ("filelist", "目录", "ls")):
            yield item

    # ------------------------------------------------------------------ #
    # /存  QQ → 服务器
    # ------------------------------------------------------------------ #
    async def _impl_save(self, event: AstrMessageEvent, names: Sequence[str]):
        guard = self._guard(event)
        if guard:
            yield event.plain_result(guard)
            return
        if not self._conf_bool("enable_receive", True):
            yield event.plain_result("接收功能关掉了，去插件配置里打开 enable_receive。")
            return

        raw = _strip_command(event.message_str, names)
        tokens = _split_args(raw)
        rename = tokens[0] if tokens else ""

        ok_lines, fails = await save_incoming(
            event,
            self.store,
            source=self._source_label(event),
            rename=rename,
            max_mb=self._conf_int("max_save_mb", 200),
        )
        if ok_lines:
            self._stats["received"] += len(ok_lines)
            text = "存好了：\n" + "\n".join(ok_lines)
            if fails:
                text += "\n\n没存下来的：\n" + "\n".join(fails)
            yield event.plain_result(text)
        else:
            self._stats["failed"] += 1
            yield event.plain_result(
                "没存下来：\n" + "\n".join(fails or ["没找到文件段。"])
                + "\n\n提示：直接发文件，或者引用那条文件消息再打 /存。"
            )

    @filter.command("filestore")
    @filter.command("存")
    async def cmd_save(self, event: AstrMessageEvent):
        """把聊天里的文件存到服务器。"""
        async for item in self._impl_save(event, ("filestore", "存", "save")):
            yield item

    # ------------------------------------------------------------------ #
    # /转  发到指定群或 QQ
    # ------------------------------------------------------------------ #
    async def _impl_relay(self, event: AstrMessageEvent, names: Sequence[str]):
        guard = self._guard(event)
        if guard:
            yield event.plain_result(guard)
            return

        raw = _strip_command(event.message_str, names)
        tokens = _split_args(raw)
        if len(tokens) < 2:
            yield event.plain_result(
                "用法：/转 <群号 或 @某人 或 QQ号> <文件路径…>\n"
                "比如 /转 123456789 /root/report.pdf"
            )
            return

        target_token = tokens[0]
        file_tokens = tokens[1:]
        target, err = self._parse_target(event, target_token)
        if target is None:
            yield event.plain_result(err)
            return

        paths, errors = self._parse_paths(event, file_tokens)
        files = [p for p in paths if p.is_file()]
        if not files:
            yield event.plain_result(
                "没找到文件：\n" + "\n".join(errors or ["路径空的，或者是个目录（先用 /打包）"])
            )
            return

        items = [(p, safe_name(p.name)) for p in files[:MAX_FILES_PER_REQUEST]]
        result = await deliver(event, target, items, self._send_cfg(), logger.info)
        if result.ok:
            self._stats["sent"] += len(items)
            yield event.plain_result(
                f"已把 {len(items)} 个文件发到{target.label}，通道：{result.strategy}"
            )
        else:
            self._stats["failed"] += 1
            yield event.plain_result(
                f"发到{target.label}失败：\n" + "\n".join(f"· {a}" for a in result.attempts[:6])
            )

    def _parse_target(self, event: AstrMessageEvent, token: str) -> Tuple[Optional[Target], str]:
        text = str(token or "").strip()
        if not text:
            return None, "空的目标。"

        # @某人：从消息链里找 At 段对应的 QQ 号
        if text.startswith("@") or text in ("群", "这里"):
            uid = self._at_target(event, text)
            if uid:
                return Target(kind="private", ident=uid), ""
            if text in ("群", "这里"):
                cur = current_target(event)
                if cur is not None and cur.kind == "group":
                    return cur, ""
            return None, "没认出 @ 的是谁，直接写数字 QQ 号或群号。"

        digits = text.lstrip("#＃")
        if not digits.isdigit():
            return None, f"目标不是数字：{text}（写群号或 QQ 号）"

        # 数字既可能是群也可能是 QQ。默认当群；私聊用 "q123456" 明确指定
        return Target(kind="group", ident=digits), ""

    def _at_target(self, event: AstrMessageEvent, text: str) -> str:
        """从消息链的 At 段里拿 QQ 号。"""
        obj = getattr(event, "message_obj", None)
        chain = getattr(obj, "message", None) if obj is not None else None
        if not chain:
            return ""
        try:
            comps = list(chain)
        except TypeError:
            return ""
        for comp in comps:
            kind = str(getattr(getattr(comp, "type", ""), "value", getattr(comp, "type", ""))).lower()
            if kind not in ("at", "mention"):
                continue
            for attr in ("qq", "user_id", "uid", "target"):
                value = getattr(comp, attr, None)
                if value and str(value).isdigit():
                    return str(value)
        return ""

    @filter.command("filerelay")
    @filter.command("转")
    async def cmd_relay(self, event: AstrMessageEvent):
        """把服务器文件发到别的群或 QQ。"""
        async for item in self._impl_relay(event, ("filerelay", "转", "fsend")):
            yield item

    # ------------------------------------------------------------------ #
    # /打包
    # ------------------------------------------------------------------ #
    async def _impl_zip(self, event: AstrMessageEvent, names: Sequence[str]):
        guard = self._guard(event)
        if guard:
            yield event.plain_result(guard)
            return

        raw = _strip_command(event.message_str, names)
        tokens = _split_args(raw)
        if not tokens:
            yield event.plain_result("用法：/打包 <目录或文件…>\n比如 /打包 /root/logs")
            return

        paths, errors = self._parse_paths(event, tokens)
        if not paths:
            yield event.plain_result("没找到能打包的：\n" + "\n".join(errors or ["路径空的"]))
            return

        max_mb = self._conf_int("max_zip_mb", 200)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_name = safe_name(f"打包-{stamp}.zip")
        zip_path = self._tmp_dir / zip_name

        try:
            await asyncio.get_running_loop().run_in_executor(
                None, make_zip, paths, zip_path
            )
        except Exception as exc:                      # noqa: BLE001
            yield event.plain_result(f"打包失败：{exc}")
            return

        try:
            size = zip_path.stat().st_size
        except OSError:
            size = 0
        if max_mb > 0 and size > max_mb * 1024 * 1024:
            try:
                zip_path.unlink()
            except OSError:
                pass
            yield event.plain_result(f"打包出来 {fmt_size(size)}，超过上限 {max_mb}MB，没发。")
            return

        target = current_target(event)
        if target is None:
            yield event.plain_result("认不出这个会话。")
            return
        result = await deliver(event, target, [(zip_path, zip_name)], self._send_cfg(), logger.info)
        if result.ok:
            self._stats["sent"] += 1
            yield event.plain_result(f"打包好了，{fmt_size(size)}，已发送。")
        else:
            self._stats["failed"] += 1
            yield event.plain_result(
                f"打包好了（{fmt_size(size)}，在 {zip_path}）但没发出去：\n"
                + "\n".join(f"· {a}" for a in result.attempts[:5])
            )

    @filter.command("filezip")
    @filter.command("打包")
    async def cmd_zip(self, event: AstrMessageEvent):
        """打包目录再发。"""
        async for item in self._impl_zip(event, ("filezip", "打包", "zip")):
            yield item

    # ------------------------------------------------------------------ #
    # /收件箱
    # ------------------------------------------------------------------ #
    async def _impl_inbox(self, event: AstrMessageEvent, names: Sequence[str]):
        guard = self._guard(event)
        if guard:
            yield event.plain_result(guard)
            return
        items = self.store.recent(10)
        if not items:
            yield event.plain_result("收件箱是空的。发个文件给我，然后打 /存。")
            return
        lines = ["最近收到的文件："]
        for item in items:
            lines.append(f"#{item.id} {item.name} ｜ {fmt_size(item.size)} ｜ {fmt_time(item.time)}")
        lines.append("")
        lines.append("用 /取 #编号 可以把它们再发出去（比如 /取 #3）。")
        yield event.plain_result("\n".join(lines))

    @filter.command("fileinbox")
    @filter.command("收件箱")
    async def cmd_inbox(self, event: AstrMessageEvent):
        """看最近收到的文件。"""
        async for item in self._impl_inbox(event, ("fileinbox", "收件箱", "inbox")):
            yield item

    # ------------------------------------------------------------------ #
    # /文件信息
    # ------------------------------------------------------------------ #
    async def _impl_info(self, event: AstrMessageEvent, names: Sequence[str]):
        guard = self._guard(event)
        if guard:
            yield event.plain_result(guard)
            return
        raw = _strip_command(event.message_str, names)
        tokens = _split_args(raw)
        if not tokens:
            yield event.plain_result("用法：/文件信息 <路径>")
            return
        paths, errors = self._parse_paths(event, tokens[:1])
        if not paths:
            yield event.plain_result("；".join(errors or ["没找到。"]))
            return
        path = paths[0]
        meta = describe(path)
        lines = [
            f"路径：{path}",
            f"类型：{'目录' if meta.is_dir else '文件'}",
            f"大小：{meta.size_text if not meta.is_dir else '（目录不统计）'}",
            f"修改：{fmt_time(meta.mtime)}",
        ]
        if not meta.is_dir:
            try:
                lines.append(f"sha256：{sha256_file(path, limit=128 * 1024 * 1024)}")
            except OSError:
                lines.append("sha256：算不出来")
        yield event.plain_result("\n".join(lines))

    @filter.command("fileinfo")
    @filter.command("文件信息")
    async def cmd_info(self, event: AstrMessageEvent):
        """看文件信息。"""
        async for item in self._impl_info(event, ("fileinfo", "文件信息", "finfo")):
            yield item

    # ------------------------------------------------------------------ #
    # /文件帮助
    # ------------------------------------------------------------------ #
    async def _impl_help(self, event: AstrMessageEvent, names: Sequence[str]):
        roots = self._roots()
        allow_all = self._conf_bool("allow_all_paths", False)
        if allow_all:
            scope = "整个文件系统（allow_all_paths 已打开）"
        elif roots:
            scope = "、".join(str(r) for r in roots[:5])
            if len(roots) > 5:
                scope += f" 等 {len(roots)} 个目录"
        else:
            scope = "还没配（去配置里填 allow_dirs）"

        text = (
            "文件传输\n"
            "/取 <路径|序号> — 把服务器上的文件发到这里\n"
            "/目录 [路径] — 列目录，带序号\n"
            "/打包 <路径…> — 目录打成 zip 再发\n"
            "/转 <群号|@某人> <路径…> — 发到别的群或 QQ\n"
            "/存 [新名字] — 把这条消息里的文件存到服务器\n"
            "/收件箱 — 看最近存下来的文件\n"
            "/文件信息 <路径> — 大小、修改时间、sha256\n"
            "\n"
            f"可访问范围：{scope}\n"
            f"单文件上限：{self._conf_int('max_file_mb', 100)}MB\n"
            f"发送通道：{self._conf('send_mode', 'auto')}"
        )
        try:
            text += (
                f"\n已发 {self._stats['sent']} 个｜已收 {self._stats['received']} 个"
                f"｜失败 {self._stats['failed']} 次"
            )
        except Exception:                             # noqa: BLE001
            pass
        yield event.plain_result(text)

    @filter.command("filehelp")
    @filter.command("文件帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """帮助。"""
        async for item in self._impl_help(event, ("filehelp", "文件帮助", "fhelp")):
            yield item

    # ------------------------------------------------------------------ #
    async def terminate(self):
        """插件卸载时清掉临时目录里过期的打包文件。"""
        try:
            if not self._tmp_dir.is_dir():
                return
            now = time.time()
            for item in self._tmp_dir.glob("*.zip"):
                try:
                    if now - item.stat().st_mtime > 3600:
                        item.unlink()
                except OSError:
                    continue
        except Exception:                             # noqa: BLE001
            pass
