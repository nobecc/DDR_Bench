#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

CIKS=(
  1037868
  1051470
  1166691
  7084
  1551152
  1739940
  1053507
  18230
  14272
  6201
  12927
  318154
  875045
  796343
  1800
  1043277
  4904
  1058090
  915912
  1091667
)

PYTHON="${PYTHON:-./.venv/bin/python}"
BASE_URL="${BASE_URL:-${MODEL_BASE_URL:-http://35.220.164.252:3888/v1}}"
MODEL="${MODEL:-${MODEL_NAME:-o3-deep-research}}"
MCP_URL="${MCP_URL:-${SQLITE_MCP_URL:-https://feeble-anyway-barbed.ngrok-free.dev/sse}}"
API_KEY="${API_KEY:-${MODEL_API_KEY:-${OPENAI_API_KEY:-}}}"
OUTPUT_DIR="${OUTPUT_DIR:-./outputs/openai_o4_mini_deep_research}"
LOGS_DIR="${LOGS_DIR:-./logs/openai_o4_mini_deep_research_10k}"
EVAL_OUTPUT="${EVAL_OUTPUT:-./outputs/openai_o4_mini_deep_research_evaluation_result.json}"
MIN_INSIGHTS="${MIN_INSIGHTS:-20}"
CURL_RETRIES="${CURL_RETRIES:-3}"
RETRY_SLEEP="${RETRY_SLEEP:-1}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-60}"
SKIP_EXISTING="${SKIP_EXISTING:-0}"
RUN_EVAL="${RUN_EVAL:-1}"

if [[ -z "${API_KEY}" ]]; then
  echo "Missing API key. Set API_KEY, MODEL_API_KEY, or OPENAI_API_KEY." >&2
  exit 1
fi

RESPONSES_URL="${BASE_URL%/}/responses"
MANIFEST="${OUTPUT_DIR}/curl_batch_manifest.jsonl"

mkdir -p "${OUTPUT_DIR}" "${LOGS_DIR}" "$(dirname "${EVAL_OUTPUT}")"
: > "${MANIFEST}"

for index in "${!CIKS[@]}"; do
  cik="${CIKS[$index]}"
  company_dir="${OUTPUT_DIR}/company_${cik}"
  payload_file="${company_dir}/request_payload.json"
  raw_response="${company_dir}/raw_response.json"
  output_file="${company_dir}/insights.json"
  metadata_file="${company_dir}/run_metadata.json"
  curl_log="${company_dir}/curl.log"
  mkdir -p "${company_dir}"

  echo "[$((index + 1))/${#CIKS[@]}] company ${cik}"

  if [[ "${SKIP_EXISTING}" == "1" && -s "${output_file}" ]]; then
    echo "  skip existing ${output_file}"
    printf '{"cik":"%s","status":"skipped","output_file":"%s"}\n' "${cik}" "${output_file}" >> "${MANIFEST}"
    continue
  fi

  "${PYTHON}" - "${payload_file}" "${cik}" "${MODEL}" "${MCP_URL}" "${MIN_INSIGHTS}" <<'PY'
import json
import sys
from pathlib import Path

from insights_discovery.common.output import build_task

payload_path, cik, model, mcp_url, min_insights = sys.argv[1:6]
min_insights = int(min_insights)
task = build_task(cik, "")
prompt = (
    "You are a DDR_Bench insight discovery agent, not a company-summary writer. "
    f"Target task: {task}. "
    "Use local SQLite evidence through MCP search/fetch as the primary source; do not rely on web pages, memorized facts, or generic knowledge. "
    "If code interpreter is available, use it only for calculations, comparisons, aggregation, or consistency checks over MCP-retrieved data. "
    "First inspect available data, schemas, coverage, and relevant records. "
    "High-value insights are not isolated database facts: each must combine a concrete company subject with direction, scale, exposure, dependency, constraint, risk, period, comparison point, or operating context, and explain why it matters. "
    "Prioritize vulnerabilities, cost or margin drivers, liquidity/leverage/funding risk, accounting judgment, tax uncertainty, dependence, volatility, anomalies, concentration, and links between narrative risk and numbers. "
    "Avoid weak standalone facts such as ticker, SIC, address, employee count, filing date, former name, or segment count. "
    "Return valid JSON only with top-level keys task, cik, insights, summary; every insight must include evidence with source sqlite or mcp. "
    f"The insights array must contain at least {min_insights} distinct, non-duplicative items."
)
payload = {
    "model": model,
    "input": prompt,
    "tools": [
        {
            "type": "mcp",
            "server_label": "ddr_10k_sqlite",
            "server_url": mcp_url,
            "require_approval": "never",
            "allowed_tools": ["search", "fetch"],
        },
        {
            "type": "code_interpreter",
            "container": {
                "type": "auto",
                "memory_limit": "4g",
            },
        },
    ],
}
Path(payload_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY

  http_code=""
  curl_rc=0
  attempt=1
  while (( attempt <= CURL_RETRIES )); do
    tmp_response="${raw_response}.tmp"
    tmp_log="${curl_log}.tmp"
    set +e
    http_code="$(curl -sS \
      --connect-timeout "${CONNECT_TIMEOUT}" \
      -w "%{http_code}" \
      -X POST "${RESPONSES_URL}" \
      -H "Authorization: Bearer ${API_KEY}" \
      -H "Content-Type: application/json" \
      -o "${tmp_response}" \
      -d @"${payload_file}" \
      2>"${tmp_log}")"
    curl_rc=$?
    set -e

    {
      echo "attempt=${attempt} curl_rc=${curl_rc} http_code=${http_code}"
      cat "${tmp_log}" || true
      echo
    } >> "${curl_log}"

    if [[ "${curl_rc}" == "0" && "${http_code}" =~ ^2 ]]; then
      mv "${tmp_response}" "${raw_response}"
      break
    fi

    if [[ -s "${tmp_response}" ]]; then
      cp "${tmp_response}" "${raw_response}"
    fi
    rm -f "${tmp_response}" "${tmp_log}"

    if (( attempt == CURL_RETRIES )); then
      echo "  curl failed after ${CURL_RETRIES} attempts; see ${curl_log}"
      break
    fi

    echo "  curl failed rc=${curl_rc} http=${http_code}; retrying in ${RETRY_SLEEP}s"
    sleep "${RETRY_SLEEP}"
    attempt=$((attempt + 1))
  done

  parse_status="failed"
  if [[ -s "${raw_response}" ]]; then
    set +e
    "${PYTHON}" insights_discovery/openai_o4_mini_deep_research/parse_curl_response.py \
      --raw-response "${raw_response}" \
      --output-file "${output_file}" \
      --metadata-file "${metadata_file}" \
      --cik "${cik}" >> "${curl_log}" 2>&1
    parse_rc=$?
    set -e
    if [[ "${parse_rc}" == "0" ]]; then
      parse_status="parsed"
    else
      echo "  parse failed rc=${parse_rc}; see ${curl_log}"
    fi
  else
    echo "  no raw response saved; skip parse"
  fi

  printf '{"cik":"%s","http_code":"%s","curl_rc":%s,"parse_status":"%s","raw_response":"%s","output_file":"%s","metadata_file":"%s","curl_log":"%s"}\n' \
    "${cik}" "${http_code}" "${curl_rc}" "${parse_status}" "${raw_response}" "${output_file}" "${metadata_file}" "${curl_log}" >> "${MANIFEST}"
done

if [[ "${RUN_EVAL}" == "1" ]]; then
  "${PYTHON}" insights_discovery/common/prepare_eval.py \
    --source-dir "${OUTPUT_DIR}" \
    --output-dir "${LOGS_DIR}" \
    --manifest "${LOGS_DIR}/prepare_manifest.json"

  "${PYTHON}" insights_discovery/common/evaluate_checklist.py \
    --scenario 10k \
    --logs-dir "${LOGS_DIR}" \
    --output "${EVAL_OUTPUT}" \
    --context-mode both
fi

echo "Curl batch manifest: ${MANIFEST}"
echo "Batch output: ${OUTPUT_DIR}"
echo "Eval logs: ${LOGS_DIR}"
echo "Eval result: ${EVAL_OUTPUT}"
