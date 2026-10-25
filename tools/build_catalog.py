"""由 NapCat 官方 OpenAPI 文档生成插件 catalog.json。

用法：
    python tools/build_catalog.py <openapi.json> <输出 catalog.json>

OpenAPI 中的 requestBody schema 直接复用为 LLM 工具参数 schema，
只做必要的合法性修补（补 type、剥离 $ref、限制层级）。
"""

from __future__ import annotations

import json
import sys
from typing import Any

# OpenAPI 的 tag -> 插件对 LLM 展示的分类名。保持 NapCat 文档原始分组，
# 便于用户在 WebUI 里按文档对照勾选。
TAG_TO_CATEGORY = {
    "消息接口": "消息",
    "消息扩展": "消息",
    "群组接口": "群组",
    "群组扩展": "群组",
    "用户接口": "用户",
    "用户扩展": "用户",
    "系统接口": "系统",
    "系统扩展": "系统",
    "文件接口": "文件",
    "文件扩展": "文件",
    "流式接口": "文件",
    "流式传输扩展": "文件",
    "扩展接口": "扩展",
    "核心接口": "扩展",
    "频道接口": "扩展",
    "AI 扩展": "扩展",
    "Go-CQHTTP": "扩展",
}

# 以这些前缀开头的 action 视为只读。写操作一律不放行，
# 未匹配到的 action 默认按写操作处理（保守）。
READ_ONLY_PREFIXES = (
    "get_",
    "fetch_",
    "can_",
    "check_",
    "nc_get_",
    "_get_",
    "_mark_",
    "test_",
)
READ_ONLY_EXACT = {
    ".get_word_slices",
    # 注意：.handle_quick_operation 不在此列 —— 它能 kick / ban / delete
    "mark_msg_as_read",
    "mark_group_msg_as_read",
    "mark_private_msg_as_read",
    "ocr_image",
    ".ocr_image",
    "translate_en2zh",
    "get_emoji_likes",
    "ArkSharePeer",
    "ArkShareGroup",
}

MAX_SCHEMA_DEPTH = 6
MAX_DESC_CHARS = 400

# OneBot 消息段联合类型展开后单个 action 就有 2 万字符，178 个工具累计会
# 吃掉大量上下文。这里用等价但精简的 schema 替代：LLM 只需要知道可以传
# CQ 码字符串、单个消息段对象或消息段数组，段内结构在描述里说明即可。
_COMPACT_SEGMENT = {
    "type": "object",
    "description": "消息段，形如 {type, data}，如 {type:'text',data:{text:'hi'}}",
    "properties": {
        "type": {
            "type": "string",
            "description": "段类型：text/image/record/video/file/at/reply/face/node/...",
        },
        "data": {
            "type": "object",
            "description": "段数据，键随 type 而变：text 用 text；at 用 qq；image/file 用 file 或 url；reply 用 id 或 seq",
            "additionalProperties": True,
        },
    },
    "required": ["type", "data"],
    "additionalProperties": True,
}

COMPACT_SCHEMAS = {
    "OB11MessageMixType": {
        "description": "消息内容：CQ 码字符串、单个消息段对象或消息段数组",
        "anyOf": [
            {"type": "string", "description": "纯文本或 CQ 码，如 [CQ:face,id=1]"},
            _COMPACT_SEGMENT,
            {"type": "array", "items": _COMPACT_SEGMENT, "description": "消息段数组"},
        ],
    },
    "OB11MessageData": _COMPACT_SEGMENT,
}


def _is_read_only(name: str) -> bool:
    if name in READ_ONLY_EXACT:
        return True
    return name.startswith(READ_ONLY_PREFIXES)


def _category_of(op: dict[str, Any]) -> str:
    tags = op.get("tags") or []
    if not tags:
        return "扩展"
    return TAG_TO_CATEGORY.get(tags[0], "扩展")


def _sanitize_schema(node: Any, depth: int = 0) -> Any:
    """把 OpenAPI schema 规整成 LLM 工具可接受的 JSON Schema。

    - 解析 $ref（调用方已内联，这里只清残留）
    - 补缺失的 type
    - 过深的嵌套折叠成宽松 object
    """
    if depth > MAX_SCHEMA_DEPTH:
        return {"type": "object", "description": "嵌套过深，详情见 NapCat 文档"}
    if isinstance(node, list):
        return [_sanitize_schema(v, depth + 1) for v in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in {"$ref", "x-schema-id", "examples", "example", "x-role"}:
            continue
        if key in {"anyOf", "oneOf", "allOf"}:
            out[key] = [_sanitize_schema(v, depth + 1) for v in value]
            continue
        if key in {"properties"} and isinstance(value, dict):
            out[key] = {k: _sanitize_schema(v, depth + 1) for k, v in value.items()}
            continue
        if key == "items":
            out[key] = _sanitize_schema(value, depth + 1)
            continue
        if key == "description" and isinstance(value, str):
            out[key] = value[:MAX_DESC_CHARS]
            continue
        out[key] = value

    # 无 type 也无 anyOf/$ref 的 object 形态，补成宽松 object
    if (
        "type" not in out
        and "anyOf" not in out
        and "oneOf" not in out
        and "allOf" not in out
        and "enum" not in out
    ):
        if "properties" in out or "additionalProperties" in out:
            out["type"] = "object"
        elif "minimum" in out or "maximum" in out:
            out["type"] = "number"
        elif "pattern" in out:
            out["type"] = "string"
        else:
            out["type"] = "string"
    return out


def _inline_refs(schema: Any, components: dict[str, Any], seen: frozenset[str]) -> Any:
    """递归内联 $ref，避免 LLM 端无法解析组件引用。"""
    if isinstance(schema, list):
        return [_inline_refs(v, components, seen) for v in schema]
    if not isinstance(schema, dict):
        return schema

    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        compact = COMPACT_SCHEMAS.get(name)
        if compact is not None:
            merged = dict(compact)
            for key, value in schema.items():
                if key != "$ref":
                    merged[key] = _inline_refs(value, components, seen)
            return merged
        if name in seen:
            return {"type": "object", "description": f"递归引用 {name}"}
        target = components.get(name)
        if target is None:
            return {"type": "object"}
        merged = dict(_inline_refs(target, components, seen | {name}))
        # $ref 同级的其它键（如 description）覆盖被引用对象
        for key, value in schema.items():
            if key != "$ref":
                merged[key] = _inline_refs(value, components, seen)
        return merged

    return {k: _inline_refs(v, components, seen) for k, v in schema.items()}


def _describe_params(schema: dict[str, Any]) -> str:
    """从 schema 里提取参数说明，用于工具描述。"""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parts: list[str] = []
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        desc = (spec.get("description") or "").strip()
        tag = "必填" if name in required else "可选"
        parts.append(f"{name}({tag}): {desc}" if desc else f"{name}({tag})")
    return "; ".join(parts)


def _build_description(op: dict[str, Any], schema: dict[str, Any]) -> str:
    summary = (op.get("summary") or "").strip()
    description = (op.get("description") or "").strip()
    # NapCat 文档里 summary 与 description 常完全一致，去重避免模型看到两遍
    parts = [summary] if summary else []
    if description and description != summary:
        parts.append(description)
    if not parts:
        parts.append("NapCat OneBot API")
    params = _describe_params(schema)
    if params:
        parts.append("参数 - " + params)
    return "\n".join(parts)[:2000]


def _normalize_action_name(path: str) -> str:
    name = path.lstrip("/")
    # OpenAPI 里个别 action 带前导点（OneBot 兼容别名），保留原样
    return name


def _params_list(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """导出便于人读的参数列表（与 SnowLuma catalog 对齐）。"""
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out: list[dict[str, Any]] = []
    for name, spec in props.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {
            "name": name,
            "required": name in required,
        }
        desc = (spec.get("description") or "").strip()
        if desc:
            entry["desc"] = desc
        if "default" in spec:
            entry["default"] = spec["default"]
        out.append(entry)
    return out


def build(openapi: dict[str, Any]) -> dict[str, Any]:
    components = (openapi.get("components") or {}).get("schemas") or {}
    actions: list[dict[str, Any]] = []

    for path, item in sorted(openapi.get("paths", {}).items()):
        op = item.get("post") or item.get("get")
        if not op:
            continue
        name = _normalize_action_name(path)
        raw = ((op.get("requestBody") or {}).get("content") or {}).get(
            "application/json", {}
        ).get("schema") or {}
        inlined = _inline_refs(raw, components, frozenset())
        schema = _sanitize_schema(inlined)
        if schema.get("type") != "object":
            # 非 object 顶层（如 Type.Union 的多个 object 之一）以 anyOf 保留
            schema = {
                "type": "object",
                "properties": {"payload": schema},
                "additionalProperties": True,
            }
        schema.setdefault("additionalProperties", True)

        actions.append(
            {
                "name": name,
                "category": _category_of(op),
                "summary": (op.get("summary") or "").strip() or name,
                "description": (op.get("description") or "").strip(),
                "readOnly": _is_read_only(name),
                "params": _params_list(schema),
                "inputSchema": schema,
                "toolDescription": _build_description(op, schema),
                "source": "napcat-openapi",
            }
        )

    return {
        "generatedFrom": {
            "openapi": openapi.get("openapi"),
            "version": (openapi.get("info") or {}).get("version"),
            "title": (openapi.get("info") or {}).get("title"),
        },
        "actions": actions,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    src, dst = argv[1], argv[2]
    with open(src, encoding="utf-8") as f:
        openapi = json.load(f)
    catalog = build(openapi)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    ro = sum(1 for a in catalog["actions"] if a["readOnly"])
    print(
        f"wrote {dst}: {len(catalog['actions'])} actions "
        f"({ro} read-only, version {catalog['generatedFrom']['version']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
