@echo off
setlocal
set "MCP_ROOT=%~dp0"
set "EASY_QUERY_MCP_CONFIG=%MCP_ROOT%config\sources.yaml"
set "EASY_QUERY_MCP_QUERY_ALIASES=%MCP_ROOT%config\query_aliases.yaml"
set "EASY_QUERY_MCP_HOME=%MCP_ROOT%"
if not defined UV_CACHE_DIR set "UV_CACHE_DIR=%MCP_ROOT%.uv-cache"

echo [easy-query-mcp] Using config: %EASY_QUERY_MCP_CONFIG%
echo [easy-query-mcp] Starting stdio MCP server...
uv run easy-query-mcp-server --config "%EASY_QUERY_MCP_CONFIG%"
