# Gemini Deep Research

Implemented runner for Gemini Deep Research via the Gemini Interactions API.

Gemini Deep Research must run in background mode and be polled until completion.
The runner asks the agent to return plain text containing JSON, then parses the
final text locally. It uses the same DDR MCP configuration as the OpenAI runner:
one remote MCP server with `search` and `fetch` in `allowed_tools`.

Run a single company:

```bash
./.venv/bin/python insights_discovery/gemini_deep_research/run_single.py \
  --cik 6201 \
  --mcp-url https://feeble-anyway-barbed.ngrok-free.dev/sse \
  --output-file ./outputs/gemini_deep_research/company_6201/insights.json \
  --api-timeout 0 \
  --use-code-execution \
  --poll-status
```

Use the Max agent:

```bash
./.venv/bin/python insights_discovery/gemini_deep_research/run_single.py \
  --cik 6201 \
  --model deep-research-max-preview-04-2026 \
  --mcp-url https://feeble-anyway-barbed.ngrok-free.dev/sse \
  --output-file ./outputs/gemini_deep_research/company_6201/insights.json \
  --api-timeout 0 \
  --use-code-execution
```

Run a batch:

```bash
./.venv/bin/python insights_discovery/gemini_deep_research/run_batch.py \
  --mcp-url https://feeble-anyway-barbed.ngrok-free.dev/sse \
  --output-dir ./outputs/gemini_deep_research \
  --api-timeout 14400 \
  --use-code-execution \
  --limit 20
```

Optional uploaded data package mode:

MCP plus uploaded SQLite-export documents:

```bash
./.venv/bin/python insights_discovery/gemini_deep_research/run_single.py \
  --cik 6201 \
  --mcp-url https://feeble-anyway-barbed.ngrok-free.dev/sse \
  --output-file ./outputs/gemini_deep_research/company_6201/insights.json \
  --api-timeout 0 \
  --use-data-package \
  --poll-status
```

Pure uploaded-data mode, with no DDR_Bench MCP exposed to Gemini:

```bash
./.venv/bin/python insights_discovery/gemini_deep_research/run_single.py \
  --cik 6201 \
  --data-package-only \
  --output-file ./outputs/gemini_deep_research_data_package/company_6201/insights.json \
  --api-timeout 0 \
  --min-data-tool-calls 0 \
  --poll-status
```

`--use-data-package` uploads the per-company schema, metadata, filings,
financial facts, and summary files through the Gemini Files API, then attaches
them to the Interactions request as `document` inputs. Files API uploads are
stored by Gemini for 48 hours.

Evaluate:

```bash
./.venv/bin/python insights_discovery/common/evaluate_checklist.py \
  --scenario 10k \
  --source-dir ./outputs/gemini_deep_research \
  --output-dir ./outputs \
  --context-mode both
```

Required auth:

```bash
export GEMINI_API_KEY=...
```
