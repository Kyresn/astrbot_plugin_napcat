# astrbot_plugin_napcat

[AstrBot 插件](https://github.com/AstrBotDevs/AstrBot)：把 [NapCat](https://github.com/NapNeko/NapCatQQ)（NapCatQQ）的全部 OneBot v11 API 注册为 LLM 工具，让大模型在对话中直接调用 QQ 的消息收发、群管理、好友、文件、群相册等 178 个 action。

适配自 [astrbot_plugin_snowluma](https://github.com/Kyresn114/astrbot_plugin_snowluma)，改用 AstrBot 自带的 aiocqhttp 连接调用 NapCat。

## 工作原理

1. 插件启动时读取内置 `catalog.json`（178 个 action，由 NapCat 官方 OpenAPI 文档 4.18.41 生成，含参数 JSON Schema 与中文描述）。
2. 每个 action 生成一个 `FunctionTool`（工具名 `napcat_<action名>`），通过 `context.add_llm_tools()` 注册。
3. LLM 调用工具时，插件直接用当前消息事件自带的 `event.bot`（aiocqhttp 的 CQHttp 客户端）调用 `bot.call_action(action, **params)`，走 AstrBot 与 NapCat 之间已建立的连接。返回的 `data` 序列化后交回模型。

**默认**不需要为插件单独配置 NapCat 的 HTTP 端点、端口或 access token —— 复用 AstrBot 平台适配器里已配好的 aiocqhttp 连接。如果希望插件独立于 aiocqhttp 适配器（比如多机器人实例），可在配置里填 NapCat 的 HTTP 服务器端点。

## 前置要求

- AstrBot >= v4.5.7（在 4.28.1 上开发测试）
- NapCat 已运行，且 AstrBot 已用 aiocqhttp（OneBot v11）平台适配器与其建立连接（反向 WS 或正向 WS 均可）
- 插件目录内已含 `catalog.json`

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `endpoint` | 空 | NapCat OneBot HTTP 端点（可选）。**留空**则复用 AstrBot aiocqhttp 适配器的现有连接（推荐，零配置）；填写如 `http://127.0.0.1:3000` 则工具改走 NapCat 独立 HTTP 服务器（NapCat WebUI → 网络配置 → HTTP 服务器） |
| `http_access_token` | 空 | HTTP 端点的 access token，仅 `endpoint` 非空时生效 |
| `http_timeout` | `30` | HTTP 端点调用超时（秒），仅 `endpoint` 非空时生效 |
| `read_only` | `true` | 只读模式：只注册查询类 action（`get_*`/`fetch_*`/`can_*` 等 82 个），发消息、踢人、退群、上传等写操作不进工具列表 |
| `categories` | 空（全开） | 勾选启用的分类（消息 / 群组 / 用户 / 系统 / 文件 / 扩展） |
| `excluded_actions` | 空 | 按 action 名排除，如 `set_restart`、`bot_exit`；优先级高于分类勾选 |
| `max_tools` | `60` | 最多注册的工具数，防止超出模型工具数上限；0 表示不限制 |
| `tool_prefix` | `napcat_` | 工具名前缀 |
| `prompt_injection` | `true` | 自动注入工具使用规范：LLM 请求挂载了本插件工具时，在 system prompt 追加一段 NapCat 使用规范（ID 用字符串、发消息传法、先查后写、写操作确认），减少模型误调用；换 persona 自带规范时可关 |

调用通道选择逻辑：`endpoint` 非空 → 独立 HTTP（`POST /<action>`，Bearer token 鉴权）；`endpoint` 为空 → 复用事件自带的 aiocqhttp 客户端（此时无需 NapCat 额外开启 HTTP 服务器网络）。`/napcat_status` 会显示当前通道。

只读分类依据 action 名判定（`get_`/`fetch_`/`can_`/`check_`/`mark_*_as_read`/`ocr_image` 等为只读；`send_`/`set_`/`delete_`/`upload_`/`create_` 等为写）。`catalog.json` 的 `readOnly` 字段记录判定结果，可用文本编辑器核对。

## 指令

- `/napcat_status`（别名 `/ncs`）：查看 NapCat 连接状态、目录版本、已注册工具数、只读开关
- `/napcat_reload`：重新加载 catalog 并重注册工具（管理员）

## 安全提示

默认只读。关闭 `read_only` 后，LLM 将能执行发消息、踢人、退群、删好友、改群设置等写操作。建议：

- 配合 `excluded_actions` 排除高危 action（`set_group_leave`、`delete_friend`、`bot_exit` 等）
- 在 WebUI → 配置 → 工具权限里把写工具设为管理员可用
- 仅在信任的群聊/私聊环境与受控 persona 下放开

## 更新 catalog

NapCat 迭代快，action 集会变化。更新方法：

1. 从 [NapCatDocs](https://github.com/NapNeko/NapCatDocs) 取最新版 OpenAPI：

   ```bash
   curl -L -o openapi.json https://raw.githubusercontent.com/NapNeko/NapCatDocs/main/src/api/<版本号>/openapi.json
   ```

2. 重新生成：

   ```bash
   python tools/build_catalog.py openapi.json astrbot_plugin_napcat/catalog.json
   ```

3. 替换插件目录内 `catalog.json`，执行 `/napcat_reload`。

## 开发

```bash
# 离线测试（需 AstrBot 的 venv，因为要 import astrbot.*）
<AstrBot>/venv/Scripts/python.exe tools/test_plugin_offline.py
```

测试覆盖：catalog 结构与 JSON Schema 合法性、工具名唯一性与合法性、只读判定、工具调用路径（参数透传 / 失败信息透出 / 只读拦截 / 无 bot 与错误平台处理 / 超长截断）、initialize 过滤链路（只读、分类、max_tools、excluded_actions、tool_prefix）。

## 与 SnowLuma 版的差异

| | SnowLuma 版 | NapCat 版 |
|---|---|---|
| 调用通道 | 独立 OneBot HTTP 端点（必须配 endpoint/token） | 默认复用 aiocqhttp 连接；也可配 `endpoint` 走独立 HTTP |
| HTTP 协议 | `POST /`，body 为 `{action, params}` 信封 | `POST /<action>`，body 即参数（NapCat 路径路由） |
| action 来源 | SnowLuma MCP catalog（200 个） | NapCat 官方 OpenAPI（178 个） |
| 流式 action | 依赖多帧 JSON 解析 | 不适用（流式 action 仅 HTTP 特有，WS 通道不暴露） |
| 依赖 | aiohttp | endpoint 模式需要 aiohttp（AstrBot 环境自带） |
