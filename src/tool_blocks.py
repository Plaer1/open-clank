"""Leaf home for ToolBlock and TOOL_TAGS.

These lived in the src.agent_tools package __init__, which also re-imports
tool_parsing/tool_schemas/tool_execution — and each of those needs ToolBlock
and TOOL_TAGS. Entering the graph through any satellite (the standalone
lifetools MCP server imports tool_schemas first) recursed back into the
partially-initialized hub and died on ImportError, taking mimo's host-tool
bridge down. This module imports only src.tool_security (itself leaf-safe),
so every entry order works. The hub still re-exports both names.
"""
from collections import namedtuple

from src.tool_security import BUILTIN_EMAIL_TOOLS

# Tool types that trigger execution
TOOL_TAGS = {"bash", "python", "web_search", "web_fetch", "read_file", "write_file", "edit_file",
             "grep", "glob", "ls", "get_workspace", "manage_bg_jobs",
             "create_document", "update_document", "edit_document",
             "search_chats",
             "chat_with_model", "create_session", "list_sessions",
             "send_to_session",
             "pipeline",
             "manage_session", "manage_memory", "list_models",
             "ui_control", "generate_image", "ask_user", "update_plan",
             "manage_tasks", "api_call", "ask_teacher", "manage_skills",
             "suggest_document",
             "manage_endpoints", "manage_mcp", "manage_webhooks",
             "manage_tokens", "manage_documents", "manage_settings",
             "manage_notes", "manage_calendar",
             "resolve_contact", "manage_contact",
             # Email tool names come from BUILTIN_EMAIL_TOOLS (unioned below)
             # so the fence regex, dispatch, and non-admin blocklist all cover
             # the same set.
             # Cookbook tools (LLM serving + downloads). Without these
             # entries, native function calls to e.g. list_served_models
             # are rejected as "Unknown function call" before reaching
             # the dispatcher — silent failure for the whole cookbook
             # surface.
             "download_model", "serve_model",
             "list_served_models", "stop_served_model",
             "list_downloads", "cancel_download",
             "search_hf_models", "list_cached_models",
             "list_serve_presets", "serve_preset", "adopt_served_model",
             "list_cookbook_servers",
             # Other tools the agent reaches for that were also missing.
             "edit_image", "trigger_research", "manage_research",
             # Generic loopback to any UI-button endpoint (cookbook,
             # gallery, email folders, etc.) — agent uses this when
             # there's no named tool wrapper for the action.
             "app_api"} | BUILTIN_EMAIL_TOOLS

ToolBlock = namedtuple("ToolBlock", ["tool_type", "content"])
