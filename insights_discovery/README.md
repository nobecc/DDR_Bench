# Insights Discovery

This directory organizes agent-specific scripts for DDR_Bench insight discovery. It is self-contained and does not import the legacy `scripts/` runners.

The common contract is:

1. Run an agent for each 10-K CIK from `data/10k/entity_ids.json`.
2. Save native per-tool and final-summary artifacts under each company.
3. Use those trajectory artifacts directly as DDR_Bench evaluation logs.
4. Run checklist evaluation and read the selected context-mode statistics.
5. Optionally run pairwise novelty evaluation on unused insights.

## Agents

- `common/`: reusable trajectory generation, local tools, batch execution, checklist evaluation, and pairwise novelty evaluation.
- `openai_o4_mini_deep_research/`: implemented OpenAI-compatible runner for `openai/o4-mini-deep-research`.
- `gemini_deep_research/`: planned.
- `dcode/`: Deep Agents Code runner driven by `.deepagents/` project config.
- `cubepi/`: CubePi runner with local MCP tool integration.

## Output Layout

Each agent must write to its own output namespace:

```text
outputs/openai_o4_mini_deep_research/company_<cik>/insights.json
outputs/gemini_deep_research/company_<cik>/insights.json
outputs/dcode/runs_<timestamp>/company_<cik>/insights_<timestamp>.csv
outputs/cubepi/runs_<timestamp>/company_<cik>/insights_<timestamp>.csv
```

## Common Evaluation Flow

Run evaluation against a method root or a specific `runs_*` experiment
directory. A method root automatically resolves to its latest run. The result
is saved inside the selected run directory as
`10k_evaluation_result_<context_mode>.json`. CubePI and D-Code evaluation
requires native `insights_*.csv` and `session_stats_*.json` artifacts.

```bash
./.venv/bin/python insights_discovery/common/evaluate_checklist.py \
  --scenario 10k \
  --source-dir ./outputs/cubepi \
  --context-mode both
```

Run pairwise novelty evaluation:

```bash
./.venv/bin/python insights_discovery/common/evaluate_novelty_pairwise.py \
  --method cubepi=./outputs/cubepi/runs_<timestamp> \
  --method dcode=./outputs/dcode/runs_<timestamp> \
  --output-dir ./outputs/novelty_eval
```
