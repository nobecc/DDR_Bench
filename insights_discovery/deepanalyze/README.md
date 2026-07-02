# DeepAnalyze

DeepAnalyze runner for DDR_Bench 10-K insight discovery. This method does not expose DDR_Bench MCP tools to the model. Instead, `run_single.py` exports the target CIK rows from `data/10k/raw/10k_financial_data.db` into JSON/JSONL files, uploads them to the DeepAnalyze API, and asks DeepAnalyze to use its own file-analysis/code-execution workflow.

Start DeepAnalyze as you described:

```bash
# 1. Start vLLM on one rlaunch GPU worker, then put that worker IP into DeepAnalyze/API/config.py API_BASE.
chmod +x /mnt/shared-storage-user/chenbei/DDR_Bench/insights_discovery/deepanalyze/run.sh
rlaunch --gpu=1 --memory=160000  --cpu=16 --charged-group=evobox_gpu --private-machine=yes --mount=gpfs://gpfs1/chenbei:/mnt/shared-storage-user/chenbei --mount=gpfs://gpfs2/gpfs2-shared-public:/mnt/shared-storage-gpfs2/gpfs2-shared-public --image=registry.h.pjlab.org.cn/ailab-evobox-evobox_gpu/vllm:0.19.0-20260511 -- bash -exc /mnt/shared-storage-user/chenbei/DDR_Bench/insights_discovery/deepanalyze/run.sh

# 2. Start the DeepAnalyze FastAPI server.
cd /mnt/shared-storage-user/chenbei/DeepAnalyze
sudo python API/start_server.py
```

Run one company from `DDR_Bench`:

```bash
cd /mnt/shared-storage-user/chenbei/DDR_Bench
./.venv/bin/python insights_discovery/deepanalyze/run_single.py \
  --cik 6201 \
  --deepanalyze-url http://localhost:8200/v1 \
  --output-file ./outputs/deepanalyze/company_6201/insights.json \
  --min-insights 20 \
  --dump-raw-response
```

Run a batch:

```bash
cd /mnt/shared-storage-user/chenbei/DDR_Bench
./.venv/bin/python insights_discovery/deepanalyze/run_batch.py \
  --deepanalyze-url http://localhost:8200/v1 \
  --output-dir ./outputs/deepanalyze \
  --limit 20 \
  --timeout 36000
```

Evaluate:

```bash
./.venv/bin/python insights_discovery/common/evaluate_checklist.py \
  --scenario 10k \
  --source-dir ./outputs/deepanalyze \
  --output-dir ./outputs \
  --context-mode message-wise
```

If you already have a standalone DeepAnalyze Markdown report, parse it into DDR_Bench output files:

```bash
./.venv/bin/python insights_discovery/deepanalyze/parse_report.py \
  --input ./outputs/deepanalyze/company_6201/insights.md \
  --cik 6201 \
  --output-file ./outputs/deepanalyze/company_6201/insights.json
```
