"""离线测试：不连 NapCat、不启 AstrBot，只验证插件核心逻辑。

运行（需要 AstrBot 自带的 venv，因为要 import astrbot.*）：
    <astrbot>/venv/Scripts/python.exe tools/test_plugin_offline.py

覆盖点：
1. catalog.json 结构完整，178 个 action，schema 全部通过 Draft 2020-12
2. 工具名合法、唯一、长度合规
3. OneBotActionTool 的属性在 pydantic dataclass 初始化后仍然存在
4. call() 走 event.bot.call_action，参数与返回值处理正确
5. 只读模式下写操作被拦截
6. 非 aiocqhttp 平台、无 bot 时给明确错误
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any

import jsonschema

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG_PATH = os.path.join(PLUGIN_DIR, "catalog.json")

sys.path.insert(0, PLUGIN_DIR)


def load_main():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_napcat_main", os.path.join(PLUGIN_DIR, "main.py")
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeBot:
    """模拟 aiocqhttp 的 CQHttp.call_action。"""

    def __init__(self, result: Any = None, error: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._result = result
        self._error = error

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        if self._error is not None:
            raise self._error
        return self._result


class FakeEvent:
    def __init__(self, bot: Any = None, platform: str = "aiocqhttp"):
        self.bot = bot
        self._platform = platform

    def get_platform_name(self):
        return self._platform


class FakeRunContext:
    def __init__(self, event):
        self.context = type("Ctx", (), {"event": event})()


def check(cond: bool, msg: str, failures: list[str]) -> None:
    if not cond:
        failures.append(msg)


def test_catalog(failures: list[str]) -> dict[str, Any]:
    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)
    actions = catalog.get("actions") or []
    check(
        len(actions) == 178, f"catalog action 数应为 178，实际 {len(actions)}", failures
    )

    names = [a["name"] for a in actions]
    check(len(names) == len(set(names)), "catalog 内 action 名重复", failures)

    bad: list[str] = []
    for a in actions:
        try:
            jsonschema.validate(
                a["inputSchema"], jsonschema.Draft202012Validator.META_SCHEMA
            )
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{a['name']}: {exc}")
    check(not bad, f"{len(bad)} 个 schema 非法: {bad[:3]}", failures)

    for a in actions:
        check("readOnly" in a, f"{a['name']} 缺 readOnly", failures)
        check(
            a["inputSchema"].get("type") == "object",
            f"{a['name']} inputSchema 顶层不是 object",
            failures,
        )
        check(
            isinstance(a.get("params"), list),
            f"{a['name']} params 不是列表",
            failures,
        )
    return catalog


def test_tool_names(module, catalog, failures: list[str]) -> None:
    tool_names = [module._make_tool_name(a["name"]) for a in catalog["actions"]]
    check(len(tool_names) == len(set(tool_names)), "工具名冲突", failures)
    dupes = {n for n in tool_names if tool_names.count(n) > 1}
    check(not dupes, f"重复工具名: {sorted(dupes)}", failures)

    pattern = re.compile(r"^[A-Za-z0-9_]+$")
    for tn in tool_names:
        check(bool(pattern.match(tn)), f"非法工具名 {tn}", failures)
        check(len(tn) <= module.MAX_TOOL_NAME, f"工具名超长 {tn}", failures)
        check(tn.startswith("napcat_"), f"工具名缺前缀 {tn}", failures)

    # 带点 action 需要区分开
    dotted = [a["name"] for a in catalog["actions"] if a["name"].startswith(".")]
    for d in dotted:
        check(
            module._make_tool_name(d) != module._make_tool_name(d.lstrip(".")),
            f"{d} 与去掉前导点后撞名",
            failures,
        )


def test_tool_object(module, catalog, failures: list[str]) -> None:
    actions = {a["name"]: a for a in catalog["actions"]}
    action = actions["send_group_msg"]
    tool = module.OneBotActionTool(action, read_only=False, read_only_guard=False)

    check(tool.name == "napcat_send_group_msg", f"工具名错误: {tool.name}", failures)
    check(tool.action_name == "send_group_msg", "action_name 丢失", failures)
    check(tool.read_only is False, "read_only 丢失", failures)
    check(tool.category == "群组", f"category 错误: {tool.category}", failures)
    check(len(tool.description) > 0, "描述为空", failures)
    check(tool.parameters.get("type") == "object", "参数 schema 顶层错误", failures)
    check(
        tool.parameters.get("additionalProperties") is True,
        "参数 schema 缺 additionalProperties",
        failures,
    )

    # readOnly 标记各取一条
    ro = actions["get_group_list"]
    wo = actions["set_group_ban"]
    check(
        module.OneBotActionTool(ro, True, True).read_only is True,
        "get_* 应为只读",
        failures,
    )
    check(
        module.OneBotActionTool(wo, False, True).read_only is False,
        "set_* 应为写",
        failures,
    )


def test_call_paths(module, catalog, failures: list[str]) -> None:
    actions = {a["name"]: a for a in catalog["actions"]}
    tool = module.OneBotActionTool(
        actions["get_group_list"], read_only=True, read_only_guard=True
    )

    # 正常调用：参数透传、data 序列化
    bot = FakeBot(result=[{"group_id": 1, "group_name": "g"}])
    ctx = FakeRunContext(FakeEvent(bot))
    out = asyncio.run(tool.call(ctx, no_cache=True, empty="", none=None))
    check(
        bot.calls == [("get_group_list", {"no_cache": True})],
        f"参数处理错误: {bot.calls}",
        failures,
    )
    check("group_name" in out, f"返回值未序列化: {out}", failures)

    # 服务端失败：ActionFailed 风格的 result 要透出 retcode/wording
    class ActionFailed(Exception):
        def __init__(self, result):
            super().__init__("failed")
            self.result = result

    bot = FakeBot(error=ActionFailed({"retcode": 100, "wording": "权限不足"}))
    out = asyncio.run(tool.call(FakeRunContext(FakeEvent(bot))))
    check("100" in out and "权限不足" in out, f"失败信息未透出: {out}", failures)

    # 普通异常
    bot = FakeBot(error=RuntimeError("boom"))
    out = asyncio.run(tool.call(FakeRunContext(FakeEvent(bot))))
    check("boom" in out, f"异常未回给模型: {out}", failures)

    # data 为 None
    bot = FakeBot(result=None)
    out = asyncio.run(tool.call(FakeRunContext(FakeEvent(bot))))
    check(out == "ok", f"None data 处理错误: {out}", failures)

    # 只读拦截写操作
    write_tool = module.OneBotActionTool(
        actions["set_group_ban"], read_only=False, read_only_guard=True
    )
    bot = FakeBot(result=None)
    out = asyncio.run(write_tool.call(FakeRunContext(FakeEvent(bot))))
    check("只读" in out, f"只读拦截失败: {out}", failures)
    check(not bot.calls, "只读模式下仍发出了 action 调用", failures)

    # 无 bot
    out = asyncio.run(tool.call(FakeRunContext(FakeEvent(None))))
    check("调用失败" in out, f"无 bot 时未报明确错误: {out}", failures)

    # 非 aiocqhttp 平台
    out = asyncio.run(tool.call(FakeRunContext(FakeEvent(FakeBot(), "telegram"))))
    check("aiocqhttp" in out, f"错误平台未拦截: {out}", failures)

    # 超长结果截断
    big = {"data": "x" * (module.MAX_RESULT_CHARS + 500)}
    bot = FakeBot(result=big)
    out = asyncio.run(tool.call(FakeRunContext(FakeEvent(bot))))
    check("已截断" in out, "超长结果未截断", failures)


class FakeAiohttpResponse:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeAiohttpSession:
    """记录 post 调用并回放预设响应。"""

    closed = False  # _get_session() 会检查此属性

    def __init__(self, response: FakeAiohttpResponse):
        self.response = response
        self.posts: list[tuple[str, dict, dict]] = []

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json or {}, headers or {}))
        return self.response

    async def close(self):
        self.closed = True


def test_http_endpoint_channel(module, catalog, failures: list[str]) -> None:
    """配置 endpoint 后走独立 HTTP：POST /<action>、Bearer token、信封解包。"""
    actions = {a["name"]: a for a in catalog["actions"]}
    client = module._EndpointHTTPClient(
        "http://127.0.0.1:3000/", access_token="sekret", timeout=5.0
    )
    session = FakeAiohttpSession(
        FakeAiohttpResponse(
            200,
            json.dumps({"status": "ok", "retcode": 0, "data": [{"group_id": 1}]}),
        )
    )
    client._session = session  # type: ignore[assignment]

    tool = module.OneBotActionTool(
        actions["get_group_list"], True, True, "napcat_", client
    )
    out = asyncio.run(tool.call(FakeRunContext(FakeEvent(None)), no_cache=True))
    url, body, headers = session.posts[0]
    check(url == "http://127.0.0.1:3000/get_group_list", f"URL 错误: {url}", failures)
    check(body == {"no_cache": True}, f"body 错误: {body}", failures)
    check(headers.get("Authorization") == "Bearer sekret", "token 未带上", failures)
    check("group_id" in out, f"data 未解包: {out}", failures)

    # 失败信封
    session2 = FakeAiohttpSession(
        FakeAiohttpResponse(
            200,
            json.dumps({"status": "failed", "retcode": 1200, "wording": "炸了"}),
        )
    )
    client2 = module._EndpointHTTPClient("http://x")
    client2._session = session2  # type: ignore[assignment]
    tool2 = module.OneBotActionTool(
        actions["get_group_list"], True, True, "napcat_", client2
    )
    out2 = asyncio.run(tool2.call(FakeRunContext(FakeEvent(None))))
    check("1200" in out2 and "炸了" in out2, f"失败信封未透出: {out2}", failures)

    # 401 认证失败
    session3 = FakeAiohttpSession(FakeAiohttpResponse(403, "token verify failed!"))
    client3 = module._EndpointHTTPClient("http://x")
    client3._session = session3  # type: ignore[assignment]
    tool3 = module.OneBotActionTool(
        actions["get_group_list"], True, True, "napcat_", client3
    )
    out3 = asyncio.run(tool3.call(FakeRunContext(FakeEvent(None))))
    check("认证失败" in out3, f"403 未识别: {out3}", failures)

    asyncio.run(client.close())
    asyncio.run(client2.close())
    asyncio.run(client3.close())


def test_initialize_http_channel(module, catalog, failures: list[str]) -> None:
    """initialize 时配置 endpoint 会创建 _EndpointHTTPClient 并注入工具。"""

    class FakeConfig(dict):
        pass

    ctx = FakeContext()

    class Plugin(module.NapcatPlugin):
        def __init__(self, context, config):
            self.context = context
            self.config = config
            self._registered_tools = []
            self._catalog = {}
            self._http_client = None

    plugin = Plugin(
        ctx,
        FakeConfig(
            read_only=True,
            endpoint="http://127.0.0.1:3000",
            http_access_token="tok",
        ),
    )
    asyncio.run(plugin.initialize())
    check(len(ctx.added) > 0, "HTTP 通道下未注册工具", failures)
    check(
        all(t.http_client is not None for t in ctx.added),
        "endpoint 配置后工具未绑定 HTTP 客户端",
        failures,
    )
    check(plugin._http_client is not None, "插件未保存 HTTP 客户端", failures)
    asyncio.run(plugin.terminate())
    check(plugin._http_client is None, "terminate 未关闭 HTTP 客户端", failures)


def test_readonly_classification(module, catalog, failures: list[str]) -> None:
    write_prefixes = ("send_", "set_", "delete_", "upload_", "create_", "kick")
    for a in catalog["actions"]:
        if a["name"].startswith(write_prefixes):
            check(
                a["readOnly"] is False,
                f"{a['name']} 是写操作却被标为只读",
                failures,
            )
    for name in ("get_group_list", "get_friend_list", "get_login_info"):
        entry = next((a for a in catalog["actions"] if a["name"] == name), None)
        check(entry is not None and entry["readOnly"], f"{name} 应为只读", failures)


class FakeContext:
    """模拟 astrbot.api.star.Context 里插件用到的两个方法。"""

    def __init__(self):
        self.added: list[Any] = []
        self._tool_manager = type(
            "TM", (), {"remove_func": staticmethod(lambda name: None)}
        )()

    def add_llm_tools(self, *tools):
        self.added.extend(tools)

    def get_llm_tool_manager(self):
        return self._tool_manager


def test_initialize(module, catalog, failures: list[str]) -> None:
    """initialize() 全链路：读 catalog → 过滤 → 构造工具 → add_llm_tools。"""

    class FakeConfig(dict):
        pass

    ctx = FakeContext()

    class Plugin(module.NapcatPlugin):
        def __init__(self, context, config):
            # 跳过 Star.__init__（依赖真实 star_map 注册表）
            self.context = context
            self.config = config
            self._registered_tools = []
            self._catalog = {}
            self._http_client = None

    # 只读默认：全部 readOnly action 注册
    plugin = Plugin(ctx, FakeConfig())
    asyncio.run(plugin.initialize())
    names = [t.name for t in ctx.added]
    ro_total = sum(1 for a in catalog["actions"] if a["readOnly"])
    check(
        len(names) == ro_total,
        f"只读默认应注册 {ro_total} 个工具，实际 {len(names)}",
        failures,
    )
    check(
        not any(n.startswith("napcat_set_") for n in names),
        "只读模式下出现了写操作工具",
        failures,
    )

    # 关闭只读：全量注册
    ctx2 = FakeContext()
    plugin2 = Plugin(ctx2, FakeConfig(read_only=False, max_tools=0))
    asyncio.run(plugin2.initialize())
    check(
        len(ctx2.added) == len(catalog["actions"]),
        f"关闭只读应注册 {len(catalog['actions'])} 个工具，实际 {len(ctx2.added)}",
        failures,
    )

    # 分类过滤 + max_tools
    ctx3 = FakeContext()
    plugin3 = Plugin(
        ctx3, FakeConfig(read_only=False, categories=["消息"], max_tools=3)
    )
    asyncio.run(plugin3.initialize())
    check(
        len(ctx3.added) == 3,
        f"max_tools=3 应截断到 3，实际 {len(ctx3.added)}",
        failures,
    )
    check(
        all(t.category == "消息" for t in ctx3.added),
        "分类过滤失效",
        failures,
    )

    # excluded_actions
    ctx4 = FakeContext()
    plugin4 = Plugin(
        ctx4, FakeConfig(read_only=False, excluded_actions=["set_restart", "bot_exit"])
    )
    asyncio.run(plugin4.initialize())
    got = {t.action_name for t in ctx4.added}
    check(
        "set_restart" not in got and "bot_exit" not in got,
        "excluded_actions 未生效",
        failures,
    )

    # tool_prefix
    ctx5 = FakeContext()
    plugin5 = Plugin(ctx5, FakeConfig(read_only=True, tool_prefix="nc_"))
    asyncio.run(plugin5.initialize())
    check(
        all(t.name.startswith("nc_") for t in ctx5.added),
        "tool_prefix 未生效",
        failures,
    )

    # terminate 清空
    asyncio.run(plugin.terminate())
    check(plugin._registered_tools == [], "terminate 未清空工具", failures)


def test_prompt_injection(module, catalog, failures: list[str]) -> None:
    """on_llm_request 钩子：挂了本插件工具才注入规范，未挂不注入。"""

    class FakeConfig(dict):
        pass

    class FakeToolSet:
        def __init__(self, names):
            self.tools = [type("T", (), {"name": n})() for n in names]

    class FakeReq:
        def __init__(self, tool_names=None, system_prompt="base"):
            self.system_prompt = system_prompt
            self.func_tool = FakeToolSet(tool_names) if tool_names is not None else None

    class Plugin(module.NapcatPlugin):
        def __init__(self, context, config):
            self.context = context
            self.config = config
            self._registered_tools = []
            self._catalog = {}
            self._http_client = None

    plugin = Plugin(None, FakeConfig())
    plugin._registered_tools = [
        module.OneBotActionTool(a, True, True) for a in catalog["actions"][:2]
    ]

    # 挂了本插件工具 → 注入
    req = FakeReq([t.name for t in plugin._registered_tools])
    asyncio.run(plugin.inject_tool_usage_prompt(None, req))
    check(
        "NapCat 工具使用规范" in req.system_prompt,
        f"挂载工具时未注入: {req.system_prompt[:80]}",
        failures,
    )
    check(
        req.system_prompt.startswith("base"),
        "注入应追加在原 system_prompt 之后",
        failures,
    )

    # 只挂其他工具 → 不注入
    req2 = FakeReq(["some_other_tool"])
    asyncio.run(plugin.inject_tool_usage_prompt(None, req2))
    check(
        "NapCat 工具使用规范" not in req2.system_prompt,
        "未挂本插件工具时不应注入",
        failures,
    )

    # 无工具 → 不注入
    req3 = FakeReq(None)
    asyncio.run(plugin.inject_tool_usage_prompt(None, req3))
    check(
        "NapCat 工具使用规范" not in req3.system_prompt,
        "无 func_tool 时不应注入",
        failures,
    )

    # 关闭配置 → 不注入
    plugin2 = Plugin(None, FakeConfig(prompt_injection=False))
    plugin2._registered_tools = plugin._registered_tools
    req4 = FakeReq([t.name for t in plugin2._registered_tools])
    asyncio.run(plugin2.inject_tool_usage_prompt(None, req4))
    check(
        "NapCat 工具使用规范" not in req4.system_prompt,
        "prompt_injection=false 时不应注入",
        failures,
    )

    # 重复请求不叠加
    req5 = FakeReq([t.name for t in plugin._registered_tools])
    asyncio.run(plugin.inject_tool_usage_prompt(None, req5))
    asyncio.run(plugin.inject_tool_usage_prompt(None, req5))
    check(
        req5.system_prompt.count("NapCat 工具使用规范") == 1,
        "同一请求重复触发时注入应去重",
        failures,
    )


def main() -> int:
    failures: list[str] = []
    module = load_main()
    catalog = test_catalog(failures)
    test_tool_names(module, catalog, failures)
    test_tool_object(module, catalog, failures)
    test_call_paths(module, catalog, failures)
    test_readonly_classification(module, catalog, failures)
    test_initialize(module, catalog, failures)
    test_http_endpoint_channel(module, catalog, failures)
    test_initialize_http_channel(module, catalog, failures)
    test_prompt_injection(module, catalog, failures)

    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    ro = sum(1 for a in catalog["actions"] if a["readOnly"])
    print(f"OK: {len(catalog['actions'])} actions ({ro} read-only), all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
