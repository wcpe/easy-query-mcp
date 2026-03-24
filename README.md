# EasyQuery MCP

`easy-query-mcp` 是一个 ORM 专项 MCP。它只保留 EasyQuery 相关源码、文档、知识卡和查询词表，用来回答实体建模、查询、事务、Repository、Code First、分页、批量、逻辑删除、拦截器、分片等问题。

## 职责边界

包含：

- EasyQuery 源码检索
- EasyQuery 官方文档检索
- EasyQuery 专属知识卡
- ORM 语义查询扩展

## 目录结构

```text
easy-query-mcp/
  config/
    sources.yaml
    query_aliases.yaml
  knowledge_cards/
    transaction/
      easyquery-transaction.md
    testing/
      h2-concurrency-testing.md
  PLAN.md
  README.md
  run.bat
  run_stdio.bat
  reindex.bat
  reindex_changed.bat
```

## 运行方式

### 1. 初始化环境

先安装依赖：

```bat
uv sync
```

### 2. 同步源码和文档

默认会把 Git 源同步到仓库内这两个目录：

- `sources\easy-query`
- `sources\easy-query-doc`

首次使用先执行：

```bat
easy-query-mcp\sync_sources.bat
```

如果你想改成本机其他目录，修改 `config/sources.yaml` 即可。

### 3. 重建索引

```bat
easy-query-mcp\reindex.bat
```

增量重建：

```bat
easy-query-mcp\reindex_changed.bat
```

### 4. 启动 MCP

```bat
easy-query-mcp\run.bat
```

默认端口：

- `http://127.0.0.1:8090/mcp`

如果客户端只支持本地 stdio：

```bat
easy-query-mcp\run_stdio.bat
```

## 运行时依赖

本仓库现在自带 `src/easy_query_mcp` 运行时，不再依赖外部 `minecraft-plugin-mcp` 仓库。

## 维护规则

- 新增知识卡时，只放 EasyQuery 相关内容
- 查询别名只保留 ORM / EasyQuery 语义，不要混入其他领域词面
- 冒烟验收至少覆盖：
  - `search_symbol`
  - `search_docs`
  - `read_doc`
  - `get_examples`
  - `search_by_concept`
  - `search_knowledge_cards`

## 当前状态

- 已初始化为 `uv` Python 项目
- 已内置独立 `src/easy_query_mcp` 运行时
- 已拆分独立 `sources.yaml`
- 已拆分独立 `query_aliases.yaml`
- 已附带 2 张 EasyQuery 相关知识卡
- 已提供独立的同步、索引和启动脚本
