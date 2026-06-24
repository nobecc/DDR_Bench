# Insights Discovery

This directory organizes agent-specific scripts for DDR_Bench insight discovery. It is self-contained and does not import the legacy `scripts/` runners.

The common contract is:

1. Run an agent for each 10-K CIK from `data/10k/entity_ids.json`.
2. Save each company output as:
   `outputs/<agent_name>/company_<cik>/insights.json`
3. Convert those insights to DDR_Bench evaluation logs.
4. Run checklist evaluation and read the selected context-mode statistics.
5. Optionally run pairwise novelty evaluation on unused insights.

## Agents

- `common/`: reusable output normalization, local tools, batch execution, evaluation preparation, checklist evaluation, and pairwise novelty evaluation.
- `openai_o4_mini_deep_research/`: implemented OpenAI-compatible runner for `openai/o4-mini-deep-research`.
- `gemini_deep_research/`: planned.
- `dcode/`: Deep Agents Code runner driven by `.deepagents/` project config.
- `cubepi/`: CubePi runner with local MCP tool integration.

## Output Layout

Each agent must write to its own output namespace:

```text
outputs/openai_o4_mini_deep_research/company_<cik>/insights.json
outputs/gemini_deep_research/company_<cik>/insights.json
outputs/dcode/company_<cik>/insights.json
outputs/cubepi/company_<cik>/insights.json
```

Prepared DDR_Bench evaluation logs should also stay separate:

```text
logs/openai_o4_mini_deep_research_10k/company_<cik>/insights_research.csv
logs/gemini_deep_research_10k/company_<cik>/insights_research.csv
logs/dcode_10k/company_<cik>/insights_research.csv
logs/cubepi_10k/company_<cik>/insights_research.csv
```

## Common Evaluation Flow

Prepare evaluation logs:

```bash
./.venv/bin/python insights_discovery/common/prepare_eval.py \
  --source-dir ./outputs/openai_o4_mini_deep_research \
  --output-dir ./logs/openai_o4_mini_deep_research_10k \
  --manifest ./logs/openai_o4_mini_deep_research_10k/prepare_manifest.json
```

Run checklist evaluation:

```bash
./.venv/bin/python insights_discovery/common/evaluate_checklist.py \
  --scenario 10k \
  --logs-dir ./logs/openai_o4_mini_deep_research_10k \
  --output ./outputs/openai_o4_mini_deep_research_10k_evaluation_result.json \
  --context-mode both
```

Run pairwise novelty evaluation:

```bash
./.venv/bin/python insights_discovery/common/evaluate_novelty_pairwise.py \
  --method openai_o4_mini_deep_research=./outputs/openai_o4_mini_deep_research_10k_evaluation_result.json=./logs/openai_o4_mini_deep_research_10k \
  --method cubepi=./outputs/cubepi_10k_evaluation_result.json=./logs/cubepi_10k \
  --output-dir ./outputs/novelty_eval
```
