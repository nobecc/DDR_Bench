# CubePi

CubePi-based DDR_Bench insight discovery agent.

This runner mirrors the `.deepagents` single-agent rules:

- system prompt is loaded from `.deepagents/AGENTS.md`;
- MCP tools are exposed with DDR_Bench-prefixed names such as `ddrbench_sqlite_execute_query` and `ddrbench_code_execute_code`;
- tool execution is sequential, matching the "exactly one tool at a time" rule;
- final output is normalized to the shared insights JSON/CSV schema.

The implementation targets the repository lockfile dependency, `cubepi 0.11.0`. The official quick start pattern is `Agent`, provider-bound models, async `@tool` functions, and subscribing to events before `agent.prompt(...)`.

## MCP Servers

`run_single.py` and `run_batch.py` auto-start the DDR_Bench SSE MCP servers needed by the configured data sources. Use `--mcp-mode auto` (default) to start only available sources, `--mcp-mode all` to expose both SQLite and code MCP, or `--mcp-mode none` to run without MCP. Pass `--no-auto-mcp` if you want to manage MCP servers yourself.

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
  --data-path ./data/10k
```

## Run one company

```bash
uv run python insights_discovery/cubepi/run_single.py \
  --provider openai \
  --model gpt-5.5 \
  --cik 6201 \
  --output-file outputs/cubepi/company_6201/insights.json
```

For an OpenAI-compatible gateway, set `MODEL_BASE_URL` and `MODEL_API_KEY`, or pass `--base-url` and `--api-key`.

Anthropic is also supported:

```bash
uv run python insights_discovery/cubepi/run_single.py \
  --provider anthropic \
  --model claude-sonnet-4-6 \
  --cik 6201 \
  --output-file outputs/cubepi/company_6201/insights.json
```

## Run a batch

```bash
uv run python insights_discovery/cubepi/run_batch.py \
  --limit 1 \
  --model gpt-5.5
```

Batch outputs follow the shared layout:

```text
outputs/cubepi/company_<cik>/insights.json
outputs/cubepi/company_<cik>/insights.csv
outputs/cubepi/company_<cik>/prompt.txt
outputs/cubepi/company_<cik>/run.log
outputs/cubepi/batch_manifest.json
```
