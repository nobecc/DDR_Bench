# OpenAI o4-mini-deep-research

Implemented runner for OpenAI-compatible `openai/o4-mini-deep-research`.

The deep-research model is used without Responses API structured outputs:
the runner asks the model to return plain text containing JSON, then parses
`output_text` locally. Do not add `response_format`, `json_schema`, or
`text.format` parameters for this runner.

Run a single company:

```bash
./.venv/bin/python insights_discovery/openai_o4_mini_deep_research/run_single.py \
  --cik 6201 \
  --mcp-url https://feeble-anyway-barbed.ngrok-free.dev/sse \
  --output-file ./outputs/openai_o4_mini_deep_research/company_6201/insights.json \
  --api-timeout 0 \
  --create-request-timeout 600 \
  --use-code-interpreter \
  --poll-status
```

If the OpenAI-compatible gateway returns `bad_response_body` for background
mode, bypass background polling:

```bash
./.venv/bin/python insights_discovery/openai_o4_mini_deep_research/run_single.py \
  --cik 6201 \
  --mcp-url https://feeble-anyway-barbed.ngrok-free.dev/sse \
  --output-file ./outputs/openai_o4_mini_deep_research/company_6201/insights.json \
  --api-timeout 0 \
  --no-background \
  --use-code-interpreter
```

Run a batch:

```bash
./.venv/bin/python insights_discovery/openai_o4_mini_deep_research/run_batch.py \
  --mcp-url https://feeble-anyway-barbed.ngrok-free.dev/sse \
  --output-dir ./outputs/openai_o4_mini_deep_research \
  --api-timeout 14400 \
  --create-request-timeout 600 \
  --use-code-interpreter \
  --limit 20
```

Optional SQLite-export data package mode:

Precompute reusable packages once:

```bash
./.venv/bin/python insights_discovery/common/export_data_packages.py \
  --output-dir ./data/10k/company_packages
```

```bash
./.venv/bin/python insights_discovery/openai_o4_mini_deep_research/run_single.py \
  --cik 6201 \
  --mcp-url https://feeble-anyway-barbed.ngrok-free.dev/sse \
  --output-file ./outputs/openai_o4_mini_deep_research/company_6201/insights.json \
  --data-package-dir ./data/10k/company_packages \
  --file-input-mode base64 \
  --api-timeout 0 \
  --use-code-interpreter \
  --use-data-package \
  --poll-status
```

Pure uploaded-data mode, with no DDR_Bench MCP exposed to the model:

```bash
./.venv/bin/python insights_discovery/openai_o4_mini_deep_research/run_single.py \
  --cik 6201 \
  --output-file ./outputs/openai_o4_mini_deep_research_data_package/company_6201/insights.json \
  --data-package-dir ./data/10k/company_packages \
  --file-input-mode base64 \
  --api-timeout 0 \
  --data-package-only \
  --min-data-tool-calls 0 \
  --no-background \
  --stream \
  --stream-status \
  --use-code-interpreter
```

`--use-data-package` exports schema, metadata, filings, and financial facts
from `data/10k/raw/10k_financial_data.db` and attaches them as Responses
`input_file` items. The default `--file-input-mode base64` follows the file
inputs guide and avoids `/v1/files`; use `--file-input-mode upload` only when
the target API endpoint supports OpenAI-compatible file uploads. Keep the
default MCP mode for the original OpenAI deep-research baseline; use
data-package mode for an apples-to-apples comparison with file-native methods
such as DeepAnalyze or for runs where public HTTPS MCP access is inconvenient.

Evaluate:

```bash
./.venv/bin/python insights_discovery/common/prepare_eval.py \
  --source-dir ./outputs/openai_o4_mini_deep_research \
  --output-dir ./logs/openai_o4_mini_deep_research_10k \
  --manifest ./logs/openai_o4_mini_deep_research_10k/prepare_manifest.json

./.venv/bin/python insights_discovery/common/evaluate_checklist.py \
  --scenario 10k \
  --logs-dir ./logs/openai_o4_mini_deep_research_10k \
  --output ./outputs/openai_o4_mini_deep_research_10k_evaluation_result.json \
  --context-mode message-wise
```
