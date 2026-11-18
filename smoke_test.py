"""不装 AstrBot 的情况下，用桩模块把插件真正 import 一遍（不需要安装 AstrBot）。

验证：import 不炸、装饰器注册了哪些命令、插件能实例化、纯逻辑函数可调用。
用法：py -3 smoke_test.py
"""

import asyncio
import importlib
import inspect
import json
import os
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
RAW = []  # 记录桩里注册到的东西


# ----------------------------------------------------------------------# 桩
def build_stub(root: Path) -> None:
    pkg = root / "astrbot"
    (pkg / "api").mkdir(parents=True, exist_ok=True)
    (pkg / "core" / "utils").mkdir(parents=True, exist_ok=True)
    (root / "astrbot" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core" / "utils" / "__init__.py").write_text("", encoding="utf-8")

    logger = types.SimpleNamespace(
        info=lambda *a, **k: RAW.append(("log", "info", a)),
        warning=lambda *a, **k: RAW.append(("log", "warning", a)),
        warn=lambda *a, **k: RAW.append(("log", "warning", a)),
        error=lambda *a, **k: RAW.append(("log", "error", a)),
        debug=lambda *a, **k: RAW.append(("log", "debug", a)),
        exception=lambda *a, **k: RAW.append(("log", "error", a)),
    )

    api = types.ModuleType("astrbot.api")
    api.logger = logger
    sys.modules["astrbot.api"] = api

    # --- astrbot.api.event ---
    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        def __init__(self, text="", umo="aiocqhttp:group:1000", gid="1000", uid="42"):
            self.message_str = text
            self.unified_msg_origin = umo
            self._gid = gid
            self._uid = uid
            self.message_obj = None
            self.context = None
            self.sent_chains = []

        async def send(self, chain):
            self.sent_chains.append(chain)

        def is_admin(self):
            return True

        def get_group_id(self):
            return self._gid

        def get_sender_id(self):
            return self._uid

        def plain_result(self, text):
            return ("plain", text)

    class _Filter:
        def __init__(self):
            self.commands = []

        def command(self, name, alias=None, priority=0):
            def deco(fn):
                if not (
                    inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn)
                ):
                    raise TypeError(f"handler {fn.__name__} 必须是 async 函数")
                self.commands.append(name)
                RAW.append(("command", name, fn.__name__))
                return fn

            return deco

        def event_message_type(self, *a, **k):
            return lambda fn: fn

        def on_astrbot_loaded(self):
            return lambda fn: fn

    class MessageChain:
        def __init__(self, chain=None, **kw):
            self.chain = list(chain or [])

        def message(self, text):
            return self

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.filter = _Filter()
    sys.modules["astrbot.api.event"] = event_mod

    # --- astrbot.api.star ---
    star_mod = types.ModuleType("astrbot.api.star")

    class Context:
        def __init__(self):
            self.sent = []

        async def send_message(self, umo, chain):
            self.sent.append((umo, chain))

    class Star:
        def __init__(self, context=None):
            self.context = context
            self.name = type(self).__name__

    def register(*args, **kwargs):
        RAW.append(("register", args))

        def deco(cls):
            cls.__registered__ = args
            return cls

        return deco

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register
    star_mod.StarTools = types.SimpleNamespace(
        get_data_dir=lambda name: tempfile.mkdtemp(prefix=f"stub_{name}_")
    )
    sys.modules["astrbot.api.star"] = star_mod

    # --- astrbot.api.message_components ---
    comp = types.ModuleType("astrbot.api.message_components")

    class _C:
        type = "plain"

        def __init__(self, **kw):
            self.__dict__.update(kw)

    comp.Plain = _C
    comp.File = _C
    comp.Image = _C
    comp.Record = _C
    sys.modules["astrbot.api.message_components"] = comp

    # --- astrbot.core.utils.astrbot_path ---
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_data_path = lambda: str(root / "data")
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod

    # --- astrbot.core.star (一些插件会用) ---
    core_star = types.ModuleType("astrbot.core.star")
    core_star.register = register
    sys.modules["astrbot.core.star"] = core_star


# ----------------------------------------------------------------------# 主流程
def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="astrbot_smoke_"))
    sys.path.insert(0, str(tmp))
    build_stub(tmp)

    sys.path.insert(0, str(PLUGIN_DIR))
    os.chdir(PLUGIN_DIR)

    ok = True
    try:
        mod = importlib.import_module("main")
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("[X ] import main 失败:", exc)
        return 1
    print("[OK] import main 成功")

    registered = [r for r in RAW if r[0] == "register"]
    print("     register 注册:", registered[:3])

    cmds = [r[1] for r in RAW if r[0] == "command"]
    print("[OK] 命令数:", len(cmds), cmds)

    # 实例化
    ctx_mod = sys.modules["astrbot.api.star"]
    cfg = {
        "allow_dirs": [str(PLUGIN_DIR)],
        "allow_all_paths": False,
        "admin_only": True,
        "send_mode": "auto",
        "max_file_mb": 100,
        "list_limit": 20,
        "rate_limit_per_min": 10,
        "keep_files": 5,
        "enable_receive": True,
    }
    try:
        plugin = mod.FileTransferPlugin(ctx_mod.Context(), cfg)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("[X ] 实例化失败:", exc)
        return 1
    print("[OK] 插件实例化成功")

    # 配置读取
    print("     allow_dirs ->", plugin._roots())
    print("     send_mode  ->", plugin._conf("send_mode", "auto"))

    # 权限
    ev = mod.AstrMessageEvent("/文件帮助")
    print("[OK] _is_allowed ->", plugin._is_allowed(ev))

    # 路径解析
    target = plugin._roots()[0] / "README.md"
    print("     resolve README ->", target.exists())

    # 帮助文本能出
    async def run_help():
        out = []
        async for item in plugin._impl_help(ev, ("filehelp",)):
            out.append(item)
        return out

    items = asyncio.run(run_help())
    print("[OK] 帮助文本 %d 条，%d 字" % (len(items), len(items[0][1]) if items else 0))

    # 参数解析（带引号、序号）
    toks = mod._split_args('"/tmp/a b.txt" 3')
    print("[OK] _split_args ->", toks)

    # strip 命令
    print("     _strip_command ->", repr(mod._strip_command("/取 /tmp/x.txt", ("fileget", "取"))))

    # 每个命令函数都能被调用（dry：只检查是 async generator）
    for name in cmds:
        fn = None
        for attr in dir(plugin):
            if attr.startswith("cmd_"):
                pass
        break
    gens = [
        a
        for a in dir(plugin)
        if a.startswith("cmd_")
        and inspect.isasyncgenfunction(getattr(plugin, a))
    ]
    print("[OK] cmd_* 异步生成器:", len(gens), sorted(gens))

    # ---------------- 发送策略 -------------------------------- #
    print("\n[发送策略]")
    senders = importlib.import_module("transfer.senders")

    calls = []

    class FakeBot:
        async def call_action(self, action, **kwargs):
            calls.append((action, kwargs))
            return {"status": "ok", "retcode": 0}

    class FakeCtx:
        def __init__(self):
            self.sent = []

        async def send_message(self, umo, chain):
            self.sent.append((umo, chain))

    class FakeEvent(mod.AstrMessageEvent):
        def __init__(self, gid="1000"):
            super().__init__()
            self.bot = FakeBot()
            self.context = FakeCtx()
            self.unified_msg_origin = f"aiocqhttp:group:{gid}"
            self._gid = gid

        def get_platform_name(self):
            return "aiocqhttp"

    sample = tmp / "hello.txt"
    sample.write_text("hello astrbot", encoding="utf-8")
    target = senders.Target(kind="group", ident="1000", umo="aiocqhttp:group:1000")

    async def try_mode(mode, tgt=None):
        calls.clear()
        ev = FakeEvent()
        cfg = senders.SendCfg(mode=mode)
        res = await senders.deliver(ev, tgt or target, [(sample, "hello.txt")], cfg)
        return res, ev

    res, ev = asyncio.run(try_mode("onebot"))
    ok1 = res.ok and calls and calls[0][0] == "upload_group_file" and calls[0][1]["group_id"] == 1000
    print(("  [OK] " if ok1 else "  [X ] ") + f"onebot 群上传 -> {res.strategy} {calls[:1]}")

    res, ev = asyncio.run(try_mode("base64"))
    b64_ok = res.ok and calls and str(calls[0][1]["file"]).startswith("base64://")
    print(("  [OK] " if b64_ok else "  [X ] ") + f"base64 内联 -> {res.strategy}")

    res, ev = asyncio.run(try_mode("native"))
    nat_ok = res.ok and ev.sent_chains and len(ev.sent_chains[0].chain) == 1
    print(("  [OK] " if nat_ok else "  [X ] ") + f"native 文件段（event.send）-> {res.strategy}")

    priv = senders.Target(kind="private", ident="42", umo="aiocqhttp:private:42")
    res, ev = asyncio.run(try_mode("onebot", priv))
    p_ok = res.ok and calls and calls[0][0] == "upload_private_file" and calls[0][1]["user_id"] == 42
    print(("  [OK] " if p_ok else "  [X ] ") + f"onebot 私聊上传 -> {res.strategy} {calls[:1]}")

    # auto 在没有 onebot 能力的平台上应能退到 native
    res, ev = asyncio.run(try_mode("auto"))
    a_ok = res.ok and res.strategy in ("onebot", "native")
    print(("  [OK] " if a_ok else "  [X ] ") + f"auto 调度 -> {res.strategy}")

    # ---------------- QQ 官方机器人 ---------------------------- #
    class FakeQQEvent(FakeEvent):
        """qq_official 拿不到数字 ID，只有 openid 和 unified_msg_origin。"""

        def __init__(self):
            super().__init__(gid="")
            self.unified_msg_origin = "qq_official:GroupMessage:GRPOPENID1"
            self._uid = ""
            self.bot = None

        def get_platform_name(self):
            return "qq_official"

    ev_qq = FakeQQEvent()
    tgt_qq = senders.current_target(ev_qq)
    qq_t_ok = (
        tgt_qq is not None
        and tgt_qq.kind == "group"
        and tgt_qq.umo == "qq_official:GroupMessage:GRPOPENID1"
    )
    print(("  [OK] " if qq_t_ok else "  [X ] ") + f"qq_official 会话识别 -> {tgt_qq}")

    async def try_qq():
        ev = FakeQQEvent()
        return await senders.deliver(
            ev, senders.current_target(ev), [(sample, "hello.txt")], senders.SendCfg(mode="auto")
        ), ev

    rqq, evqq = asyncio.run(try_qq())
    qq_ok = rqq.ok and rqq.strategy == "native" and bool(evqq.sent_chains)
    print(("  [OK] " if qq_ok else "  [X ] ") + f"qq_official 走 native -> {rqq.strategy} {rqq.attempts}")

    # event.send 挂了要退到 context.send_message（跨平台老版本兼容）
    class BrokenSendEvent(FakeEvent):
        async def send(self, chain):
            raise RuntimeError("被动回复窗口已过期")

    async def try_fallback():
        ev = BrokenSendEvent()
        return await senders.deliver(
            ev, target, [(sample, "hello.txt")], senders.SendCfg(mode="native")
        ), ev

    rfb, evfb = asyncio.run(try_fallback())
    fb_ok = rfb.ok and bool(evfb.context.sent)
    print(("  [OK] " if fb_ok else "  [X ] ") + f"event.send 失败退 context -> {rfb.strategy}")

    # qq_official 全失败时，回执要带上排查提示
    class DeadCtx:
        async def send_message(self, umo, chain):
            raise RuntimeError("qq_official 不支持主动消息")

    class DeadQQEvent(FakeQQEvent):
        async def send(self, chain):
            raise RuntimeError("No permission to upload file")

    async def try_dead():
        ev = DeadQQEvent()
        ev.context = DeadCtx()
        return await senders.deliver(
            ev, senders.current_target(ev), [(sample, "hello.txt")], senders.SendCfg(mode="auto")
        )

    rdead = asyncio.run(try_dead())
    dead_ok = (not rdead.ok) and "QQ 官方机器人" in (rdead.detail or "")
    print(("  [OK] " if dead_ok else "  [X ] ") + f"qq_official 失败提示 -> {rdead.detail}")

    # 超限拦截
    big = tmp / "big.bin"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    async def try_big():
        ev = FakeEvent()
        return await senders.deliver(
            ev, target, [(big, "big.bin")], senders.SendCfg(mode="auto", max_mb=1)
        )
    rbig = asyncio.run(try_big())
    print(("  [OK] " if not rbig.ok else "  [X ] ") + f"大小上限拦截 -> {rbig.detail}")

    # 帮助文本换行是真换行
    nl_ok = "\n" in items[0][1]
    print(("  [OK] " if nl_ok else "  [X ] ") + f"帮助文本含真换行 -> {nl_ok}")

    # 返回体是 failed 但不抛异常的情况（NapCat/SnowLuma 容量满时就是这样）
    class SoftBot:
        def __init__(self, sequence):
            self.sequence = list(sequence)
            self.seen = []

        async def call_action(self, action, **kwargs):
            self.seen.append(dict(kwargs))
            status = self.sequence.pop(0) if self.sequence else 0
            if status == 0:
                return {"status": "ok", "retcode": 0, "data": {}}
            return {"status": "failed", "retcode": status, "message": "文件空间不足"}

    ev3 = FakeEvent()
    ev3.bot = SoftBot([100, 100])
    r3 = asyncio.run(
        senders.deliver(ev3, target, [(sample, "hello.txt")], senders.SendCfg(mode="onebot"))
    )
    soft_ok = not r3.ok and r3.attempts and "文件空间不足" in r3.attempts[0]
    print(("  [OK] " if soft_ok else "  [X ] ") + f"软失败被识别 -> {r3.attempts}")

    ev4 = FakeEvent()
    ev4.bot = SoftBot([100, 0])
    r4 = asyncio.run(
        senders.deliver(ev4, target, [(sample, "hello.txt")], senders.SendCfg(mode="onebot"))
    )
    retry_ok = r4.ok and len(ev4.bot.seen) == 2 and "name" not in ev4.bot.seen[1]
    print(("  [OK] " if retry_ok else "  [X ] ") + f"去掉 name 重试 -> {r4.ok} {[list(k) for k in ev4.bot.seen]}")

    # ---------------- 接收 -------------------------------- #
    print("\n[接收]")
    receiver = importlib.import_module("transfer.receiver")
    store_mod = importlib.import_module("transfer.store")

    class FakeFileComp:
        type = "file"

        def __init__(self, name, url="", file=""):
            self.name = name
            self.url = url
            self.file = file

    inc_src = tmp / "incoming.txt"
    inc_src.write_text("from qq", encoding="utf-8")
    ev2 = FakeEvent()
    ev2.message_obj = types.SimpleNamespace(
        message=[FakeFileComp("incoming.txt", file=str(inc_src))], group_id="1000"
    )

    found = receiver.iter_incoming(ev2)
    print(("  [OK] " if len(found) == 1 else "  [X ] ") + f"识别文件段 -> {[i.name for i in found]}")

    st = store_mod.FileStore(tmp / "inbox", keep=10)
    st.ensure()
    st.load()

    async def do_save():
        return await receiver.save_incoming(ev2, st, source="群1000")

    ok_lines, fails = asyncio.run(do_save())
    print(("  [OK] " if ok_lines and not fails else "  [X ] ") + f"存文件 -> {ok_lines} {fails}")
    recent = st.recent(5)
    print(("  [OK] " if len(recent) == 1 else "  [X ] ") + f"收件箱 -> {[(r.id, r.name, r.size) for r in recent]}")
    gone = not inc_src.exists()
    print(("  [OK] " if gone else "  [X ] ") + "源文件已移入收件箱")

    return 0 if ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
