# dcode Insight Discovery

This agent slot uses Deep Agents Code project configuration rather than the legacy `scripts/` runners.

Configuration files live at:

```text
.deepagents/AGENTS.md
.deepagents/.mcp.json
```

The current `.deepagents/` configuration is single-agent only and intentionally does not define `.deepagents/agents/`.

## MCP

`.deepagents/.mcp.json` defines SSE MCP servers for SQLite/database search and read-only code execution:

```json
{
  "mcpServers": {
    "ddrbench_sqlite": {
      "type": "sse",
      "url": "http://127.0.0.1:8765/sse"
    },
    "ddrbench_code": {
      "type": "sse",
      "url": "http://127.0.0.1:8766/sse"
    }
  }
}
```

If your MCP server is not local, edit `.deepagents/.mcp.json` or pass a different config with `--mcp-config`.

Start both servers:

```bash
export no_proxy=localhost,127.0.0.1,10.0.0.0/8,100.96.0.0/12,.pjlab.org.cn
export NO_PROXY="$no_proxy"

./.venv/bin/python tool_server/sqlite_mcp.py \
  --transport sse \
  --host 127.0.0.1 \
  --port 8765 \
  --data-path ./data/10k/raw/10k_financial_data.db
```

```bash
./.venv/bin/python tool_server/code_mcp.py \
  --transport sse \
  --host 127.0.0.1 \
  --port 8766 \
  --data-path ./data/10k
```

## Web Search

dcode loads its built-in `web_search` tool when `TAVILY_API_KEY` is available in the shell, project-root `.env`, or `~/.deepagents/.env`.

From the DDR_Bench repo root:

```bash
cp .env.example .env
# edit .env and set TAVILY_API_KEY
```

or:

```bash
export TAVILY_API_KEY=...
```

The agent prompts allow web search for secondary context and lead generation. Final DDR_Bench insights must still cite sqlite/file evidence for the target CIK.

## Run

From the DDR_Bench repo root:

```bash
export no_proxy=localhost,127.0.0.1,10.0.0.0/8,100.96.0.0/12,.pjlab.org.cn
export NO_PROXY="$no_proxy"

./.venv/bin/dcode --trust-project-mcp -n "$(cat outputs/dcode/company_6201/prompt.txt)" --timeout 3600
```

## Run All Companies

Run every CIK in `data/10k/entity_ids.json`:

```bash
./.venv/bin/python insights_discovery/dcode/run_all_companies.py
```

Useful options:

```bash
# Smoke test the first two companies
./.venv/bin/python insights_discovery/dcode/run_all_companies.py --limit 2

# Resume without overwriting existing valid insights.json files
./.venv/bin/python insights_discovery/dcode/run_all_companies.py

# Force rerun
./.venv/bin/python insights_discovery/dcode/run_all_companies.py --overwrite

# Run specific CIKs
./.venv/bin/python insights_discovery/dcode/run_all_companies.py --only 6201 1551152

# Use a specific dcode model
./.venv/bin/python insights_discovery/dcode/run_all_companies.py -M openai:gpt-5.5
```

Each company writes to:

```text
outputs/dcode/company_<CIK>/insights.json
outputs/dcode/company_<CIK>/run.log
outputs/dcode/company_<CIK>/prompt.txt
outputs/dcode/company_<CIK>/run_metadata.json
```

The batch manifest is saved at:

```text
outputs/dcode/batch_manifest.json
```

`run_metadata.json` and `batch_manifest.json` include:

- per-company status and runtime;
- MCP tool call counts by server and tool name;
- token usage parsed from dcode's `Usage Stats` table when available.

Token usage depends on the model provider returning usage metadata through dcode. The batch runner does not pass `-q` by default so dcode can print the usage table into `run.log`. Passing `--quiet` makes logs cleaner but usually prevents token usage from being captured.

To keep DDR evaluation inputs stable, `insights.json` is not modified with metadata by default. Use `--annotate-output` if you also want a `run_metadata` key inserted into each `insights.json`.
