# DeepAnalyze

DeepAnalyze runner for DDR_Bench 10-K insight discovery. This method does not expose DDR_Bench MCP tools to the model. Instead, `run_single.py` exports the target CIK rows from `data/10k/raw/10k_financial_data.db` into JSON/JSONL files, uploads them to the DeepAnalyze API, and asks DeepAnalyze to use its own file-analysis/code-execution workflow.

Start DeepAnalyze as you described:

```bash
# 1. Start vLLM on one rlaunch GPU worker, then put that worker IP into DeepAnalyze/API/config.py API_BASE.

# 2. Start the DeepAnalyze FastAPI server.
cd /mnt/shared-storage-user/chenbei/DeepAnalyze
sudo python API/start_server.py
```

Run one company from `DDR_Bench`:

Precompute reusable input packages once:

```bash
cd /mnt/shared-storage-user/chenbei/DDR_Bench
./.venv/bin/python insights_discovery/common/export_data_packages.py \
  --output-dir ./data/10k/company_packages
```

```bash
cd /mnt/shared-storage-user/chenbei/DDR_Bench
./.venv/bin/python insights_discovery/deepanalyze/run_single.py \
  --cik 6201 \
  --deepanalyze-url http://localhost:8200/v1 \
  --data-package-dir ./data/10k/company_packages \
  --output-file ./outputs/deepanalyze/company_6201/insights.json \
  --min-insights 20 \
  --dump-raw-response
```

Run a batch:

```bash
cd /mnt/shared-storage-user/chenbei/DDR_Bench
./.venv/bin/python insights_discovery/deepanalyze/run_batch.py \
  --deepanalyze-url http://localhost:8200/v1 \
  --data-package-dir ./data/10k/company_packages \
  --output-dir ./outputs/deepanalyze \
  --limit 20 \
  --timeout 36000
```

Prepare eval CSVs:

```bash
./.venv/bin/python insights_discovery/common/prepare_eval.py \
  --source-dir ./outputs/deepanalyze \
  --output-dir ./eval/deepanalyze_10k \
  --manifest ./eval/deepanalyze_10k/prepare_manifest.json
```

Evaluate:

```bash
./.venv/bin/python insights_discovery/common/evaluate_checklist.py \
  --scenario 10k \
  --logs-dir ./eval/deepanalyze_10k \
  --output ./outputs/deepanalyze_10k_evaluation_result.json \
  --context-mode message-wise
```

If you already have a standalone DeepAnalyze Markdown report, parse it into DDR_Bench output files:

```bash
./.venv/bin/python insights_discovery/deepanalyze/parse_report.py \
  --input ./outputs/deepanalyze/company_6201/insights.md \
  --cik 6201 \
  --output-file ./outputs/deepanalyze/company_6201/insights.json
```
