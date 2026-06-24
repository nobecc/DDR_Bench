#!/usr/bin/env python3
"""OpenAI Responses API MCP tool configuration for insight discovery agents."""

import os
from typing import Any, Dict, List, Sequence


MCP_ALLOWED_TOOLS = ["search", "fetch"]
DEFAULT_MCP_SERVER_LABEL = "ddr_10k_sqlite"


def build_openai_mcp_tools(
    mcp_url: str | None = None,
    server_label: str = DEFAULT_MCP_SERVER_LABEL,
    require_approval: str = "never",
    allowed_tools: Sequence[str] = MCP_ALLOWED_TOOLS,
    include_code_interpreter: bool = False,
) -> List[Dict[str, Any]]:
    """Build OpenAI Responses API tools for a remote MCP server.

    The returned schema follows OpenAI's MCP tool shape:
    {
      "type": "mcp",
      "server_label": "...",
      "server_url": "...",
      "require_approval": "never",
      "allowed_tools": ["search", "fetch"]
    }

    When requested, code interpreter is added as OpenAI's built-in tool shape:
    {"type": "code_interpreter", "container": {"type": "auto"}}
    """
    server_url = mcp_url or os.getenv("SQLITE_MCP_URL", "")
    if not server_url:
        raise ValueError("mcp_url or SQLITE_MCP_URL is required to build MCP tools.")

    filtered_tools = [tool for tool in allowed_tools if tool in MCP_ALLOWED_TOOLS]
    if filtered_tools != MCP_ALLOWED_TOOLS:
        # Keep the contract tight for deep research / company knowledge compatibility.
        filtered_tools = MCP_ALLOWED_TOOLS.copy()

    tools: List[Dict[str, Any]] = [
        {
            "type": "mcp",
            "server_label": server_label,
            "server_url": server_url,
            "require_approval": require_approval,
            "allowed_tools": filtered_tools,
        }
    ]
    if include_code_interpreter:
        tools.append({"type": "code_interpreter", "container": {"type": "auto"}})
    return tools


def build_openai_response_tools(
    mcp_url: str | None = None,
    server_label: str = DEFAULT_MCP_SERVER_LABEL,
    require_approval: str = "never",
    include_code_interpreter: bool = False,
) -> List[Dict[str, Any]]:
    """Alias with a name that makes the intended API surface explicit."""
    return build_openai_mcp_tools(
        mcp_url=mcp_url,
        server_label=server_label,
        require_approval=require_approval,
        include_code_interpreter=include_code_interpreter,
    )


def build_gemini_mcp_tools(
    mcp_url: str | None = None,
    server_label: str = DEFAULT_MCP_SERVER_LABEL,
    allowed_tools: Sequence[str] = MCP_ALLOWED_TOOLS,
    include_code_execution: bool = False,
    include_web_search: bool = False,
    headers: Dict[str, str] | None = None,
) -> List[Dict[str, Any]]:
    """Build Gemini Interactions API tools for the same DDR MCP server.

    The MCP configuration mirrors build_openai_mcp_tools semantically:
    one remote MCP server, the same server label, and the same search/fetch
    allowlist. Gemini's schema names the server type mcp_server and uses
    name/url fields.
    """
    server_url = mcp_url or os.getenv("SQLITE_MCP_URL", "")
    if not server_url:
        raise ValueError("mcp_url or SQLITE_MCP_URL is required to build MCP tools.")

    filtered_tools = [tool for tool in allowed_tools if tool in MCP_ALLOWED_TOOLS]
    if filtered_tools != MCP_ALLOWED_TOOLS:
        filtered_tools = MCP_ALLOWED_TOOLS.copy()

    mcp_tool: Dict[str, Any] = {
        "type": "mcp_server",
        "name": server_label,
        "url": server_url,
        "allowed_tools": filtered_tools,
    }
    if headers:
        mcp_tool["headers"] = dict(headers)

    tools: List[Dict[str, Any]] = [mcp_tool]
    if include_code_execution:
        tools.append({"type": "code_execution"})
    if include_web_search:
        tools.extend([{"type": "google_search"}, {"type": "url_context"}])
    return tools
