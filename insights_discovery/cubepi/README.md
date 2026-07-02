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
  --code-root ./data/10k
```

## Run one company

```bash
uv run python insights_discovery/cubepi/run_single.py \
  --provider openai \
  --model gpt-5.5 \
  --cik 6201 \
  --insight-max-tokens 512 \
  --summary-max-tokens 16384 \
  --insight-temperature 0.5 \
  --output-dir outputs/cubepi
```

For an OpenAI-compatible gateway, set `MODEL_BASE_URL` and `MODEL_API_KEY`, or pass `--base-url` and `--api-key`.

Anthropic is also supported:

```bash
uv run python insights_discovery/cubepi/run_single.py \
  --provider anthropic \
  --model claude-sonnet-4-6 \
  --cik 6201 \
  --output-dir outputs/cubepi
```

## Run a batch

```bash
uv run python insights_discovery/cubepi/run_batch.py \
  --limit 1 \
  --model gpt-5.5
```

Single and batch runs create one timestamped experiment directory. Every
company in the same batch shares that directory:

```text
outputs/cubepi/runs_<timestamp>/company_<cik>/prompt.txt
outputs/cubepi/runs_<timestamp>/company_<cik>/run.log
outputs/cubepi/runs_<timestamp>/company_<cik>/insights_<timestamp>.csv
outputs/cubepi/runs_<timestamp>/company_<cik>/chat_messages_<timestamp>.csv
outputs/cubepi/runs_<timestamp>/company_<cik>/session_stats_<timestamp>.json
outputs/cubepi/runs_<timestamp>/company_<cik>/trajectory_<timestamp>.jsonl
outputs/cubepi/runs_<timestamp>/company_<cik>/sqlite-mcp_calls_<timestamp>.csv
outputs/cubepi/runs_<timestamp>/company_<cik>/code-mcp_calls_<timestamp>.csv
outputs/cubepi/runs_<timestamp>/batch_manifest.json
```

The timestamped files use the same message-wise and chat-wise artifact
contract as the ReAct runner. Insight and final-summary generation reuse the
CubePI exploration model.
