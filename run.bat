@echo off
setlocal
set "MCP_ROOT=%~dp0"
set "EASY_QUERY_MCP_CONFIG=%MCP_ROOT%config\sources.yaml"
set "EASY_QUERY_MCP_DB=%MCP_ROOT%index_easyquery.db"
set "EASY_QUERY_MCP_QUERY_ALIASES=%MCP_ROOT%config\query_aliases.yaml"
set "EASY_QUERY_MCP_HOME=%MCP_ROOT%"
if not defined UV_CACHE_DIR set "UV_CACHE_DIR=%MCP_ROOT%.uv-cache"

echo [easy-query-mcp] Using config: %EASY_QUERY_MCP_CONFIG%
echo [easy-query-mcp] Using database: %EASY_QUERY_MCP_DB%
echo [easy-query-mcp] Using query aliases: %EASY_QUERY_MCP_QUERY_ALIASES%
echo [easy-query-mcp] Starting Streamable HTTP server on port 8090...
echo [easy-query-mcp] MCP endpoint: http://127.0.0.1:8090/mcp
uv run easy-query-mcp-server --config "%EASY_QUERY_MCP_CONFIG%" --streamable-http --port 8090
