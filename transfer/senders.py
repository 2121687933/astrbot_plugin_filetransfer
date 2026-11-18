"""把服务器上的文件真正送到聊天平台。

QQ 侧有两条主路，互相兜底：

1. **OneBot 扩展接口**（NapCat / Lagrange / go-cqhttp 都认）
   - 群聊：``upload_group_file``
   - 私聊：``upload_private_file``
   这是最可靠的一条，收到的文件带真实文件名，也走平台原生的文件通道。

2. **AstrBot 内核的 File 消息段**（``Comp.File``）
   由平台适配器自己翻译，适合 Telegram 这类非 OneBot 平台。

另外还留了两条保底路，专门对付接入方式拿不到 bot 实例、
或者 AstrBot 和 OneBot 实现根本不在同一台机器上的情况：

3. base64 内联（小文件）
4. 直连 OneBot HTTP 接口（配置里填地址和 token）

所有路径都只在"文件确实存在于本机"时才直接用；跨机器场景由 base64 兜。
"""

from __future__ import annotations

import base64
import importlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .paths import fmt_size

# base64 内联最多吃这么大的文件，再大容易把内存和消息大小撑爆
BASE64_LIMIT = 20 * 1024 * 1024

ONEBOT_ACTION_GROUP = "upload_group_file"
ONEBOT_ACTION_PRIVATE = "upload_private_file"


@dataclass
class Target:
    """文件要发到哪儿。"""

    kind: str                 # group / private
    ident: str                # 群号或 QQ 号
    umo: str = ""             # 当前会话的 unified_msg_origin

    @property
    def label(self) -> str:
        return f"{'群' if self.kind == 'group' else '私聊'} {self.ident}"


@dataclass
class SendCfg:
    """发送相关的配置快照。"""

    mode: str = "auto"            # auto / native / onebot / base64 / http
    onebot_url: str = ""
    onebot_token: str = ""
    force_base64: bool = False
    max_mb: int = 100


@dataclass
class SendResult:
    ok: bool = False
    strategy: str = ""
    detail: str = ""
    sent: int = 0
    attempts: List[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        self.attempts.append(text)


# ---------------------------------------------------------------------- #
# 小工具
# ---------------------------------------------------------------------- #
def _platform_of(event: Any) -> str:
    """拿平台名，各家版本字段名不一样，挨个试。"""
    for getter in ("get_platform_name", "get_platform"):
        fn = getattr(event, getter, None)
        if callable(fn):
            try:
                value = fn()
                if value:
                    return str(value).lower()
            except Exception:  # noqa: BLE001
                pass
    meta = getattr(event, "platform_meta", None)
    name = getattr(meta, "name", "") if meta is not None else ""
    if name:
        return str(name).lower()
    meta = getattr(event, "platform", None)
    if isinstance(meta, str):
        return meta.lower()
    return ""


def _is_onebot_platform(platform: str) -> bool:
    return any(key in platform for key in ("aiocqhttp", "onebot", "napcat", "lagrange"))


def _get_bot(event: Any) -> Any:
    return getattr(event, "bot", None)


def _get_call_action(bot: Any) -> Optional[Callable]:
    """aiocqhttp 的 CQHttp 实例有 call_action；有的版本在 bot.api 上。"""
    if bot is None:
        return None
    fn = getattr(bot, "call_action", None)
    if callable(fn):
        return fn
    api = getattr(bot, "api", None)
    fn = getattr(api, "call_action", None) if api is not None else None
    if callable(fn):
        return fn
    return None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _action_failed(ret: Any) -> str:
    """有的实现失败时返回 {"status":"failed",...} 而不抛异常，得自己看一眼。"""
    if not isinstance(ret, dict):
        return ""
    status = str(ret.get("status", "")).lower()
    retcode = ret.get("retcode")
    if status == "failed" or (retcode is not None and str(retcode) not in ("0", "")):
        detail = ret.get("message") or ret.get("wording") or ret.get("msg") or ret
        return str(detail)[:200]
    return ""


def _session_ids(event: Any) -> Tuple[str, str]:
    """返回 (group_id, user_id)，拿不到就是空串。"""
    gid = ""
    uid = ""
    for attr in ("get_group_id",):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                gid = str(fn() or "")
            except Exception:  # noqa: BLE001
                gid = ""
    obj = getattr(event, "message_obj", None)
    if not gid and obj is not None:
        gid = str(getattr(obj, "group_id", "") or "")
    for attr in ("get_sender_id",):
        fn = getattr(event, attr, None)
        if callable(fn):
            try:
                uid = str(fn() or "")
            except Exception:  # noqa: BLE001
                uid = ""
    if not uid and obj is not None:
        sender = getattr(obj, "sender", None)
        uid = str(getattr(sender, "user_id", "") or "")
    return gid, uid


def current_target(event: Any) -> Optional[Target]:
    """当前会话该发给谁。"""
    gid, uid = _session_ids(event)
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    if gid:
        return Target(kind="group", ident=gid, umo=umo)
    if uid:
        return Target(kind="private", ident=uid, umo=umo)
    return None


def _safe_int(text: str) -> Optional[int]:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------- #
# 策略 1：OneBot 扩展接口
# ---------------------------------------------------------------------- #
async def _send_via_onebot_action(
    event: Any, target: Target, path: Path, name: str, base64_inline: bool = False
) -> Tuple[bool, str]:
    bot = _get_bot(event)
    call_action = _get_call_action(bot)
    if call_action is None:
        return False, "拿不到 OneBot 的 bot 实例"

    group_int = _safe_int(target.ident) if target.kind == "group" else None
    user_int = _safe_int(target.ident) if target.kind == "private" else None
    if target.kind == "group" and group_int is None:
        return False, f"群号不是数字：{target.ident}"
    if target.kind == "private" and user_int is None:
        return False, f"QQ 号不是数字：{target.ident}"

    if base64_inline:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return False, f"读取文件失败：{exc}"
        if len(raw) > BASE64_LIMIT:
            return False, f"文件 {fmt_size(len(raw))} 超过 base64 内联上限"
        file_value = "base64://" + base64.b64encode(raw).decode("ascii")
    else:
        # OneBot 实现通常接受本地绝对路径；用 as_posix 让 Windows 路径也可读
        file_value = str(path)

    if target.kind == "group":
        payload: Dict[str, Any] = {
            "group_id": group_int,
            "file": file_value,
            "name": name,
        }
        action = ONEBOT_ACTION_GROUP
    else:
        payload = {"user_id": user_int, "file": file_value, "name": name}
        action = ONEBOT_ACTION_PRIVATE

    try:
        ret = await _maybe_await(call_action(action, **payload))
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    else:
        bad = _action_failed(ret)
        if not bad:
            return True, ""
        err = bad

    # 有些实现不认 name 参数（go-cqhttp 旧版），去掉再试一次
    payload.pop("name", None)
    try:
        ret = await _maybe_await(call_action(action, **payload))
        bad = _action_failed(ret)
        if not bad:
            return True, ""
        return False, f"{err[:160]} / {bad[:160]}"
    except Exception as exc2:  # noqa: BLE001
        return False, f"{err[:160]} / {str(exc2)[:160]}"


# ---------------------------------------------------------------------- #
# 策略 2：AstrBot 的 File 消息段
# ---------------------------------------------------------------------- #
def _build_file_chain(path: Path, name: str) -> Any:
    """构造只含一个 File 段的消息链，兼容新旧字段名和不同版本的导入路径。"""
    import astrbot.api.message_components as Comp  # 延迟导入，自检时不依赖

    chain_cls = None
    for mod_path in (
        "astrbot.api.event",
        "astrbot.core.message.message_event_result",
        "astrbot.core.message.message_chain",
    ):
        try:
            module = importlib.import_module(mod_path)
        except ImportError:
            continue
        chain_cls = getattr(module, "MessageChain", None)
        if chain_cls is not None:
            break
    if chain_cls is None:
        raise RuntimeError("这个 AstrBot 版本找不到 MessageChain，改用 onebot 通道")

    try:
        comp = Comp.File(file=str(path), name=name)
    except TypeError:
        comp = Comp.File(file_=str(path), name=name)
    try:
        return chain_cls(chain=[comp])
    except TypeError:
        return chain_cls([comp])


async def _send_via_native(event: Any, target: Target, path: Path, name: str) -> Tuple[bool, str]:
    try:
        chain = _build_file_chain(path, name)
    except Exception as exc:  # noqa: BLE001
        return False, f"构造文件消息失败：{str(exc)[:160]}"
    umo = str(getattr(event, "unified_msg_origin", "") or target.umo)
    if not umo:
        return False, "当前事件没有 unified_msg_origin"

    ctx = getattr(event, "context", None)
    sender = getattr(ctx, "send_message", None) if ctx is not None else None
    if not callable(sender):
        # 老办法：直接走事件自己的发送通道
        send = getattr(event, "send", None)
        if not callable(send):
            return False, "context 和 event 都没有发送接口"
        try:
            await _maybe_await(send(chain))
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:200]

    try:
        await _maybe_await(sender(umo, chain))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


# ---------------------------------------------------------------------- #
# 策略 3：直连 OneBot HTTP 接口
# ---------------------------------------------------------------------- #
async def _send_via_http(cfg: SendCfg, target: Target, path: Path, name: str) -> Tuple[bool, str]:
    url = (cfg.onebot_url or "").strip().rstrip("/")
    if not url:
        return False, "没配置 OneBot HTTP 地址"
    try:
        import aiohttp
    except ImportError:
        return False, "缺少 aiohttp"

    action = ONEBOT_ACTION_GROUP if target.kind == "group" else ONEBOT_ACTION_PRIVATE
    id_key = "group_id" if target.kind == "group" else "user_id"
    ident = _safe_int(target.ident)
    if ident is None:
        return False, f"目标 ID 不是数字：{target.ident}"

    payload = {id_key: ident, "file": str(path), "name": name}
    headers = {}
    if cfg.onebot_token:
        headers["Authorization"] = f"Bearer {cfg.onebot_token}"
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{url}/{action}", json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return False, f"HTTP {resp.status}: {text[:160]}"
                if '"status":"failed"' in text.replace(" ", "") or '"retcode":' in text and '"retcode":0' not in text.replace(" ", ""):
                    return False, text[:160]
                return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


# ---------------------------------------------------------------------- #
# 对外主函数
# ---------------------------------------------------------------------- #
async def deliver(
    event: Any,
    target: Optional[Target],
    items: Sequence[Tuple[Path, str]],
    cfg: SendCfg,
    log: Optional[Callable[[str], None]] = None,
) -> SendResult:
    """把一批文件发出去，返回整体结果。

    ``items`` 是 [(本地路径, 显示名)]。多个文件会依次发送，
    某一个失败不影响后面的，但最终 ok 要求全部成功。
    """
    result = SendResult()
    if not items:
        result.detail = "没有要发的文件"
        return result
    if target is None:
        result.detail = "认不出当前会话，不知道发给谁"
        return result

    platform = _platform_of(event)
    onebot_like = _is_onebot_platform(platform) or _get_call_action(_get_bot(event)) is not None
    mode = (cfg.mode or "auto").strip().lower()

    for index, (path, name) in enumerate(items):
        if not path.exists():
            result.add(f"[{name}] 文件不存在")
            result.detail = f"{name} 已经不在了"
            continue
        if path.is_dir():
            result.add(f"[{name}] 是个目录，不能直接发")
            result.detail = f"{name} 是目录，先打包"
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            result.add(f"[{name}] stat 失败：{exc}")
            result.detail = f"读不到 {name} 的大小"
            continue
        if cfg.max_mb > 0 and size > cfg.max_mb * 1024 * 1024:
            result.add(f"[{name}] {fmt_size(size)} 超过上限 {cfg.max_mb}MB")
            result.detail = f"{name} 太大（{fmt_size(size)}），上限 {cfg.max_mb}MB"
            continue

        ok, used, errs = await _deliver_one(event, target, path, name, cfg, onebot_like, mode)
        if ok:
            result.sent += 1
            result.strategy = used
            if log:
                log(f"发送 {name} 成功，走 {used}")
        else:
            result.add(f"[{name}] " + "；".join(errs))
            if log:
                log(f"发送 {name} 失败：{'；'.join(errs)}")

    result.ok = result.sent == len(items)
    if result.ok:
        result.detail = "全部发出去了"
    elif result.sent:
        result.detail = f"发出 {result.sent}/{len(items)} 个，剩下的没成功"
    elif not result.detail:
        result.detail = "一次都没发出去"
    return result


async def _deliver_one(
    event: Any,
    target: Target,
    path: Path,
    name: str,
    cfg: SendCfg,
    onebot_like: bool,
    mode: str,
) -> Tuple[bool, str, List[str]]:
    """按配置的优先级试各种策略，返回 (成功?, 用了哪种, 失败原因列表)。"""
    errors: List[str] = []
    use_base64 = bool(cfg.force_base64)

    # base64 模式下只走内联
    if mode == "base64":
        ok, err = await _send_via_onebot_action(event, target, path, name, base64_inline=True)
        if ok:
            return True, "base64", errors
        return False, "", [f"base64: {err}"]

    # 纯内核 File 段
    if mode == "native":
        ok, err = await _send_via_native(event, target, path, name)
        if ok:
            return True, "native", errors
        return False, "", [f"native: {err}"]

    # 纯 HTTP
    if mode == "http":
        ok, err = await _send_via_http(cfg, target, path, name)
        if ok:
            return True, "http", errors
        return False, "", [f"http: {err}"]

    # 指定只用 OneBot 扩展接口
    if mode == "onebot":
        ok, err = await _send_via_onebot_action(event, target, path, name, base64_inline=use_base64)
        if ok:
            return True, "onebot-base64" if use_base64 else "onebot", errors
        return False, "", [f"onebot: {err}"]

    # ---- auto：按可靠性从高到低试 ----
    if onebot_like and not use_base64:
        ok, err = await _send_via_onebot_action(event, target, path, name, base64_inline=False)
        if ok:
            return True, "onebot", errors
        errors.append(f"onebot: {err}")

    # QQ 官方机器人这类不支持 OneBot 扩展接口的平台，先试内核 File 段
    ok, err = await _send_via_native(event, target, path, name)
    if ok:
        return True, "native", errors
    errors.append(f"native: {err}")

    if onebot_like and not use_base64:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size <= BASE64_LIMIT:
            ok, err = await _send_via_onebot_action(event, target, path, name, base64_inline=True)
            if ok:
                return True, "onebot-base64", errors
            errors.append(f"base64: {err}")
        else:
            errors.append(f"base64: 文件 {fmt_size(size)} 超过内联上限，跳过")

    if cfg.onebot_url:
        ok, err = await _send_via_http(cfg, target, path, name)
        if ok:
            return True, "http", errors
        errors.append(f"http: {err}")

    return False, "", errors
