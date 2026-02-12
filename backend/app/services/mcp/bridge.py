"""Bridge helpers between local Tool schema and MCP schema."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .client import MCPCallResult, MCPToolSchema


def mcp_tool_schema_to_function_schema(schema: MCPToolSchema) -> Dict[str, Any]:
    """Convert MCP schema into local function-tool schema."""

    return {
        "type": "function",
        "function": {
            "name": schema.qualified_name,
            "description": schema.description,
            "parameters": schema.input_schema or {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }


def mcp_tool_schema_to_description(schema: MCPToolSchema) -> str:
    """Convert MCP schema into ReAct prompt tool description line."""

    params = schema.input_schema.get("properties", {}) if isinstance(schema.input_schema, dict) else {}
    required = schema.input_schema.get("required", []) if isinstance(schema.input_schema, dict) else []
    params_desc = []
    for key, config in params.items():
        if not isinstance(config, dict):
            config = {}
        part = f"{key}: {config.get('type', 'any')}"
        if key in required:
            part += " (必填)"
        if "description" in config:
            part += f" - {config['description']}"
        params_desc.append(part)

    tool_desc = schema.description or f"MCP 远程工具（server={schema.server_name}）"
    return (
        f"**{schema.qualified_name}**: {tool_desc}\n"
        f"  参数: {', '.join(params_desc) if params_desc else '无'}"
    )


def mcp_call_result_to_tool_result_payload(result: MCPCallResult) -> Dict[str, Any]:
    """Convert MCP call result to payload compatible with ToolResult fields."""

    return {
        "success": result.success,
        "output": result.output,
        "data": result.data,
        "error": result.error,
    }


def build_local_tool_as_mcp_schema(
    *,
    name: str,
    description: str,
    parameters: Optional[Dict[str, Any]] = None,
    server_name: str = "local",
    prefix: str = "mcp",
) -> MCPToolSchema:
    """Expose local tool schema in MCP-style naming for future migration."""

    return MCPToolSchema(
        server_name=server_name,
        tool_name=name,
        qualified_name=f"{prefix}.{server_name}.{name}",
        description=description or "",
        input_schema=parameters or {"type": "object", "properties": {}, "required": []},
    )
