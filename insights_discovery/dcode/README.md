# dcode Insight Discovery

This agent slot uses Deep Agents Code project configuration rather than the legacy `scripts/` runners.

Agent exploration rules live at:

```text
.deepagents/AGENTS.md
```

The current `.deepagents/` configuration is single-agent only and intentionally does not define `.deepagents/agents/`. MCP configuration is generated privately for each run rather than stored in `.deepagents/`.

## MCP

`run_single.py` and `run_batch.py` expose only the MCP servers needed by the configured data sources. In `--mcp-mode auto` (default), CSV-only configs expose only `ddrbench_code`, SQLite configs expose `ddrbench_sqlite` plus `ddrbench_code`, and missing data sources expose no MCP. Use `--mcp-mode all` to expose both servers or `--mcp-mode none` to disable MCP.

By default, the dcode runner writes `.dcode_runtime/mcp.json` inside the
company run directory and passes it explicitly with `--mcp-config`. D-Code
then starts the required stdio servers itself:

```json
{
  "mcpServers": {
    "ddrbench_code": {
      "command": "./.venv/bin/python",
      "args": [
        "tool_server/code_mcp.py",
        "--transport",
        "stdio",
        "--code-root",
        "."
      ]
    }
  }
}
```

For SSE debugging, pass `--mcp-transport sse`. In that mode the runner auto-starts local SSE servers unless `--no-auto-mcp` is passed. The SSE config shape is:

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

Manual startup remains useful for debugging. For SQLite:

```bash
./.venv/bin/python tool_server/sqlite_mcp.py \
  --transport sse \
  --host 127.0.0.1 \
  --port 8765 \
  --data-path ./data/10k/raw/10k_financial_data.db
```

For CSV/file analysis:

```bash
./.venv/bin/python tool_server/code_mcp.py \
  --transport sse \
  --host 127.0.0.1 \
  --port 8766 \
  --code-root ./data/10k
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
./.venv/bin/python insights_discovery/dcode/run_single.py \
  --cik 6201 \
  --output-dir outputs/dcode \
  --mcp-mode auto \
  -M openai:gpt-5.1
```

The runner creates `outputs/dcode/runs_<timestamp>/company_6201/` by default. MCP uses stdio by default, so no separate MCP server process is needed.
The runner loads `.env` by default before launching dcode; pass `--env-file PATH` to use a different file. DeepAgents debug logs are written to each company directory as `dcode_debug.log`.

Useful single-run options:

```bash
# Show the dcode command and resolved MCP servers without running the model
./.venv/bin/python insights_discovery/dcode/run_single.py \
  --cik 6201 \
  --output-dir outputs/dcode \
  --dry-run

# Force both SQLite and code MCP servers to be exposed
./.venv/bin/python insights_discovery/dcode/run_single.py \
  --cik 6201 \
  --output-dir outputs/dcode \
  --mcp-mode all

# Use already-running SSE MCP servers instead of auto-starting them
./.venv/bin/python insights_discovery/dcode/run_single.py \
  --cik 6201 \
  --output-dir outputs/dcode \
  --mcp-transport sse \
  --no-auto-mcp
```

## Run Batch

Run every CIK in `data/10k/entity_ids.json`:

```bash
./.venv/bin/python insights_discovery/dcode/run_batch.py \
  --output-dir outputs/dcode \
  --mcp-mode auto \
  -M openai:gpt-5.1
```

Useful options:

```bash
# Smoke test the first two companies
./.venv/bin/python insights_discovery/dcode/run_batch.py \
  --output-dir outputs/dcode \
  --limit 2

# Resume without overwriting existing completed trajectory artifacts
./.venv/bin/python insights_discovery/dcode/run_batch.py \
  --output-dir outputs/dcode

# Force rerun
./.venv/bin/python insights_discovery/dcode/run_batch.py \
  --output-dir outputs/dcode \
  --overwrite

# Run specific CIKs
./.venv/bin/python insights_discovery/dcode/run_batch.py \
  --output-dir outputs/dcode \
  --target-ids 6201,1551152

# Use a specific dcode model
./.venv/bin/python insights_discovery/dcode/run_batch.py \
  --output-dir outputs/dcode \
  -M openai:gpt-5.1
```

`run_all_companies.py` remains as a compatibility wrapper around `run_batch.py`:

```bash
./.venv/bin/python insights_discovery/dcode/run_all_companies.py \
  --output-dir outputs/dcode \
  --limit 2
```

Each single or batch experiment creates one timestamped run directory:

```text
outputs/dcode/runs_<timestamp>/company_<CIK>/run.log
outputs/dcode/runs_<timestamp>/company_<CIK>/prompt.txt
outputs/dcode/runs_<timestamp>/company_<CIK>/run_metadata.json
outputs/dcode/runs_<timestamp>/company_<CIK>/dcode_debug.log
outputs/dcode/runs_<timestamp>/company_<CIK>/trajectory_events.jsonl
outputs/dcode/runs_<timestamp>/company_<CIK>/insights_<timestamp>.csv
outputs/dcode/runs_<timestamp>/company_<CIK>/chat_messages_<timestamp>.csv
outputs/dcode/runs_<timestamp>/company_<CIK>/session_stats_<timestamp>.json
outputs/dcode/runs_<timestamp>/company_<CIK>/trajectory_<timestamp>.jsonl
outputs/dcode/runs_<timestamp>/company_<CIK>/sqlite-mcp_calls_<timestamp>.csv
outputs/dcode/runs_<timestamp>/company_<CIK>/code-mcp_calls_<timestamp>.csv
```

The batch manifest is saved at:

```text
outputs/dcode/runs_<timestamp>/batch_manifest.json
```

`run_metadata.json` and `batch_manifest.json` include:

- per-company status and runtime;
- MCP tool call counts by server and tool name;
- token usage parsed from dcode's `Usage Stats` table when available.

Token usage depends on the model provider returning usage metadata through dcode. The batch runner does not pass `-q` by default so dcode can print the usage table into `run.log`. Passing `--quiet` makes logs cleaner but usually prevents token usage from being captured.

The runner captures D-Code's structured LangGraph `AIMessage` and
`ToolMessage` stream in a process-local runtime hook. It then uses the same
exploration model to generate ReAct-compatible per-tool insights and the final
chat-wise summary. Generation controls are `--insight-max-tokens` (512),
`--summary-max-tokens` (16384), and `--insight-temperature` (0.5).
