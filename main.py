"""把 NapCat 的 OneBot v11 API 注册为 AstrBot 的 LLM 工具。

设计要点：
- 复用 AstrBot 自带的 aiocqhttp 适配器连接。工具被调用时直接从
  `event.bot` 取 CQHttp 客户端，因此不需要额外配置 NapCat 的 HTTP
  端点、端口或 access token；NapCat 只需按原有方式与 AstrBot 建立反向
  WebSocket（或由 AstrBot 主动连接）。
- 工具目录来自 NapCat 官方 OpenAPI 文档生成的 `catalog.json`，每个
  action 对应一个 `FunctionTool`，由 `Context.add_llm_tools()` 注册。
- 默认只读：写操作在调用层直接拦下并回一句说明，模型会看到原因。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.config.astrbot_config import AstrBotConfig

_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")

# OneBot action 名里可能有 '.'、'-'，工具名只允许 [A-Za-z0-9_]
_TOOL_NAME_SAFE = re.compile(r"[^0-9a-zA-Z_]+")
# 工具名上限较保守，兼容各家 provider 的 64 字符限制
MAX_TOOL_NAME = 64
MAX_RESULT_CHARS = 4000
MAX_DESCRIPTION_CHARS = 2000

_TOOL_PREFIX = "napcat_"

# 挂载了本插件工具的请求，注入 system prompt 的使用规范。
# 只写模型容易犯错的点，不复述 schema 里已有的参数说明。
_TOOL_USAGE_PROMPT = """\
[NapCat 工具使用规范]
- QQ 号、群号、消息 ID 一律用字符串：group_id="123456"，不要传数字。
- 发消息用 napcat_send_group_msg / napcat_send_private_msg，message 参数直接传文本字符串即可；需要 @、图片、表情时才传消息段数组，如 [{"type":"at","data":{"qq":"123456"}},{"type":"text","data":{"text":" 你好"}}]；CQ 码字符串（如 "[CQ:face,id=1]"）也可以。
- 不知道群号/用户号/消息 ID 时先用 napcat_get_group_list、napcat_get_friend_list、napcat_get_msg 查，不要猜测。
- 查询大列表（群成员、历史消息）可能返回很长，结果被截断时改用更精确的查询参数，不要反复全量重查。
- 写操作（send_/set_/delete_/kick 等开头）影响真实 QQ 环境：批量或不可逆操作前先向用户确认。
"""


def _make_tool_name(action_name: str, prefix: str = _TOOL_PREFIX) -> str:
    """把 OneBot action 名转换为合法的 LLM 工具名。

    `ocr_image` 与 `.ocr_image`（go-cqhttp 兼容别名）规范化后会撞名，
    因此带前导非字母数字的 action 统一加 `alias_` 段区分。
    """
    name = _TOOL_NAME_SAFE.sub("_", action_name).strip("_")
    if action_name and action_name[0] in "._-":
        name = f"alias_{name}"
    tool_name = f"{prefix}{name}"
    return tool_name[:MAX_TOOL_NAME]


def _truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(结果过长已截断，完整长度 {len(text)} 字符)"


def _format_data(data: Any) -> str:
    """把 action 返回的 data 序列化成给模型看的文本。"""
    if data is None:
        return "ok"
    if isinstance(data, str):
        return _truncate(data)
    try:
        return _truncate(json.dumps(data, ensure_ascii=False))
    except (TypeError, ValueError):
        return _truncate(str(data))


def _normalize_schema(schema: Any) -> dict[str, Any]:
    """确保交给 AstrBot 的参数 schema 是合法 JSON Schema object。"""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    schema = dict(schema)
    if schema.get("type") != "object":
        schema = {
            "type": "object",
            "properties": {"payload": schema},
            "additionalProperties": True,
        }
    props = schema.get("properties")
    if not isinstance(props, dict):
        schema["properties"] = {}
    schema.setdefault("additionalProperties", True)
    return schema


class _EndpointHTTPClient:
    """直连 NapCat OneBot HTTP 服务器的客户端。

    NapCat 的 HTTP server 按路径路由（POST /<action>，body 即参数），
    与 SnowLuma 的 {action, params} 信封不同。
    """

    def __init__(self, endpoint: str, access_token: str = "", timeout: float = 30.0):
        self.endpoint = endpoint.rstrip("/")
        self.access_token = access_token
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def call_action(self, action: str, params: dict[str, Any]) -> Any:
        """POST /<action>，返回信封里的 data；失败抛异常。"""
        session = await self._get_session()
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        url = f"{self.endpoint}/{action}"
        async with session.post(url, json=params or {}, headers=headers) as resp:
            text = await resp.text()
            if resp.status in (401, 403):
                raise PermissionError(
                    f"OneBot 认证失败 (HTTP {resp.status}): {text[:200]}"
                )
            try:
                envelope = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                raise RuntimeError(
                    f"OneBot 返回非 JSON (HTTP {resp.status}): {text[:200]}"
                )
            if not isinstance(envelope, dict):
                raise TypeError(f"OneBot 响应格式异常: {text[:200]}")
            if envelope.get("status") != "ok":
                raise RuntimeError(
                    f"action {action} 失败: retcode={envelope.get('retcode')} "
                    f"wording={envelope.get('wording') or envelope.get('message')}"
                )
            return envelope.get("data")

    async def ping(self) -> tuple[bool, str]:
        """GET 根路径检查在线，NapCat 返回 NapCat4 Is Running。"""
        session = await self._get_session()
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            async with session.get(self.endpoint, headers=headers) as resp:
                text = (await resp.text())[:200]
                return resp.status == 200, text
        except aiohttp.ClientError:
            return False, ""

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None


class OneBotActionTool(FunctionTool[AstrAgentContext]):
    """单个 OneBot action 包装成的 LLM 工具。

    参数 schema 与描述在实例化时从 catalog 条目生成；`call()` 把模型给的
    kwargs 原样作为 action 参数发给 NapCat。
    """

    def __init__(
        self,
        action: dict[str, Any],
        read_only: bool,
        read_only_guard: bool,
        tool_prefix: str = _TOOL_PREFIX,
        http_client: _EndpointHTTPClient | None = None,
    ):
        description = (
            action.get("toolDescription")
            or action.get("summary")
            or action.get("description")
            or f"NapCat OneBot action {action.get('name')}"
        )
        # FunctionTool 是 pydantic dataclass，super().__init__ 会按字段重建
        # 实例状态，因此实例属性必须在 super() 之后赋值。
        super().__init__(
            name=_make_tool_name(str(action["name"]), tool_prefix),
            description=str(description)[:MAX_DESCRIPTION_CHARS],
            parameters=_normalize_schema(action.get("inputSchema")),
        )
        self.action_name = str(action["name"])
        self.read_only = bool(read_only)
        self.read_only_guard = bool(read_only_guard)
        self.category = str(action.get("category") or "")
        # 非空时优先走独立 HTTP 端点，否则用事件自带的 aiocqhttp 连接
        self.http_client = http_client

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        if self.read_only_guard and not self.read_only:
            return (
                f"工具 {self.name} 对应写操作 action '{self.action_name}'，"
                "当前插件处于只读模式，已拦截。需要执行请在插件配置中关闭只读模式。"
            )

        params = {k: v for k, v in kwargs.items() if v is not None and v != ""}

        # 通道一：插件自配的 NapCat HTTP 端点
        if self.http_client is not None:
            try:
                data = await self.http_client.call_action(self.action_name, params)
            except asyncio.TimeoutError:
                return f"调用 {self.action_name} 超时。"
            except Exception as exc:  # noqa: BLE001 - OneBot 侧任何错误都回给模型
                return f"调用 {self.action_name} 失败: {exc}"
            return _format_data(data)

        # 通道二：事件自带的 aiocqhttp 客户端（推荐，零配置）
        event = context.context.event
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return (
                "调用失败：当前事件没有可用的 OneBot 客户端。"
                "请确认本会话来自 aiocqhttp 适配器且 NapCat 已连接，"
                "或在插件配置里填写 NapCat HTTP 端点。"
            )
        if event.get_platform_name() != "aiocqhttp":
            return (
                f"调用失败：当前平台是 {event.get_platform_name()}，"
                "本插件只能通过 aiocqhttp 适配器调用 NapCat，"
                "或在插件配置里填写 NapCat HTTP 端点。"
            )

        try:
            data = await bot.call_action(self.action_name, **params)
        except asyncio.TimeoutError:
            return f"调用 {self.action_name} 超时。"
        except Exception as exc:  # noqa: BLE001 - OneBot 侧任何错误都回给模型
            # aiocqhttp 会把 status=failed 转成 ActionFailed，其 result 里有
            # wording/retcode，尽量把这些信息带给模型
            detail = getattr(exc, "result", None)
            if isinstance(detail, dict):
                return (
                    f"action {self.action_name} 失败: "
                    f"retcode={detail.get('retcode')} "
                    f"wording={detail.get('wording') or detail.get('message')}"
                )
            return f"调用 {self.action_name} 失败: {exc}"

        return _format_data(data)


class NapcatPlugin(Star):
    """把 NapCat 的 OneBot v11 action 全量注册为 AstrBot LLM 工具。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._registered_tools: list[OneBotActionTool] = []
        self._catalog: dict[str, Any] = {}
        self._http_client: _EndpointHTTPClient | None = None

    async def initialize(self):
        """加载 catalog 并注册全部工具。"""
        try:
            catalog = await asyncio.to_thread(self._load_catalog_file, _CATALOG_PATH)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(f"[NapCat] catalog.json 加载失败: {exc}")
            return
        self._catalog = catalog

        actions = catalog.get("actions") or []
        if not actions:
            logger.warning("[NapCat] catalog 为空，未注册任何工具")
            return

        enabled_categories = self._resolve_categories(actions)
        read_only_guard = bool(self.config.get("read_only", True))
        # 前缀同样只允许 [A-Za-z0-9_]，非法字符折叠为下划线
        tool_prefix = _TOOL_NAME_SAFE.sub(
            "_", str(self.config.get("tool_prefix") or _TOOL_PREFIX)
        ).strip("_")
        tool_prefix = f"{tool_prefix}_" if tool_prefix else _TOOL_PREFIX

        selected = [
            a for a in actions if str(a.get("category") or "") in enabled_categories
        ]
        if bool(self.config.get("read_only", True)):
            selected = [a for a in selected if a.get("readOnly")]

        excluded = {
            str(name) for name in (self.config.get("excluded_actions") or []) if name
        }
        if excluded:
            selected = [a for a in selected if str(a["name"]) not in excluded]

        limit = int(self.config.get("max_tools", 0) or 0)
        dropped_by_limit = 0
        if limit > 0 and len(selected) > limit:
            dropped_by_limit = len(selected) - limit
            selected = selected[:limit]

        # 配置了 endpoint 就走独立 HTTP，否则复用事件自带连接
        endpoint = str(self.config.get("endpoint") or "").strip()
        http_client: _EndpointHTTPClient | None = None
        channel = "aiocqhttp 连接"
        if endpoint:
            http_client = _EndpointHTTPClient(
                endpoint=endpoint,
                access_token=str(self.config.get("http_access_token") or ""),
                timeout=float(self.config.get("http_timeout", 30) or 30),
            )
            channel = f"HTTP 端点 {endpoint}"

        tools = [
            OneBotActionTool(
                a,
                bool(a.get("readOnly")),
                read_only_guard,
                tool_prefix,
                http_client,
            )
            for a in selected
        ]
        self._registered_tools = tools
        self._http_client = http_client
        if tools:
            self.context.add_llm_tools(*tools)

        version = (catalog.get("generatedFrom") or {}).get("version") or "unknown"
        logger.info(
            f"[NapCat] 已注册 {len(tools)} 个工具 "
            f"(通道: {channel}，目录版本 {version}，分类 {sorted(enabled_categories)}"
            f"{f'，因 max_tools 截断 {dropped_by_limit} 个' if dropped_by_limit else ''})"
        )

    @staticmethod
    def _load_catalog_file(path: str) -> dict[str, Any]:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _resolve_categories(self, actions: list[dict[str, Any]]) -> set[str]:
        """收集配置里勾选的分类；一项都没勾时视为全开。"""
        selected = {str(cat) for cat in (self.config.get("categories") or []) if cat}
        if selected:
            return selected
        return {
            str(a.get("category")) for a in actions if a.get("category") is not None
        }

    async def terminate(self):
        if self._registered_tools:
            for tool in self._registered_tools:
                try:
                    self.context.get_llm_tool_manager().remove_func(tool.name)
                except Exception as exc:  # noqa: BLE001 - 卸载不应影响停机
                    logger.debug(f"[NapCat] 卸载工具 {tool.name} 失败: {exc}")
        self._registered_tools = []
        if self._http_client is not None:
            await self._http_client.close()
            self._http_client = None

    def _current_http_client(self) -> _EndpointHTTPClient | None:
        """取当前生效的 HTTP 客户端（可能由最近一次 initialize 创建）。"""
        if self._http_client is not None:
            return self._http_client
        client = (
            self._registered_tools[0].http_client if self._registered_tools else None
        )
        if client is not None:
            self._http_client = client
        return client

    @filter.command("napcat_status", alias={"ncs", "napcat状态"})
    async def napcat_status(self, event: AstrMessageEvent):
        """查看 NapCat 工具注册状态"""
        endpoint = str(self.config.get("endpoint") or "").strip()
        online = "未知"

        if endpoint:
            client = self._current_http_client()
            if client is None:
                client = _EndpointHTTPClient(
                    endpoint=endpoint,
                    access_token=str(self.config.get("http_access_token") or ""),
                    timeout=float(self.config.get("http_timeout", 30) or 30),
                )
                self._http_client = client
            ok, body = await client.ping()
            online = f"在线 ({body})" if ok else f"离线或不可用 ({body or '无响应'})"
            channel = f"HTTP 端点 {endpoint}"
        else:
            bot = getattr(event, "bot", None)
            if bot is not None and hasattr(bot, "call_action"):
                try:
                    info = await bot.call_action("get_version_info")
                    if isinstance(info, dict):
                        online = (
                            f"在线 ({info.get('app_name', '?')} "
                            f"{info.get('app_version', '?')})"
                        )
                    else:
                        online = "在线"
                except Exception as exc:  # noqa: BLE001 - 状态查询失败也只回显
                    online = f"离线或不可用: {exc}"
            channel = "aiocqhttp 连接（复用适配器）"

        version = (self._catalog.get("generatedFrom") or {}).get("version", "unknown")
        total = len(self._catalog.get("actions") or [])
        yield event.plain_result(
            f"NapCat: {online}\n"
            f"平台: {event.get_platform_name()}\n"
            f"调用通道: {channel}\n"
            f"目录版本: {version} (共 {total} 个 action)\n"
            f"已注册工具: {len(self._registered_tools)} 个\n"
            f"只读模式: {'开' if self.config.get('read_only', True) else '关'}"
        )

    @filter.command("napcat_reload")
    async def napcat_reload(self, event: AstrMessageEvent):
        """重新加载 catalog 并重注册工具（需管理员）"""
        if not event.is_admin():
            yield event.plain_result("仅管理员可执行。")
            return
        await self.terminate()
        await self.initialize()
        yield event.plain_result(f"已重载，当前工具数: {len(self._registered_tools)}")

    @filter.on_llm_request()
    async def inject_tool_usage_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """LLM 请求前注入 NapCat 工具使用规范。

        只在本次请求实际挂了本插件工具时注入，避免污染无关会话。
        可用配置项 prompt_injection 关闭。
        """
        if not self._registered_tools or not bool(
            self.config.get("prompt_injection", True)
        ):
            return
        registered = {t.name for t in self._registered_tools}
        mounted = req.func_tool is not None and any(
            tool.name in registered for tool in req.func_tool.tools
        )
        if not mounted:
            return
        marker = _TOOL_USAGE_PROMPT
        if marker not in req.system_prompt:
            req.system_prompt = (req.system_prompt or "").rstrip() + "\n\n" + marker
