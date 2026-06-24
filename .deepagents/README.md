# DDR_Bench Single-Agent dcode Configuration

This configuration intentionally disables custom subagents by not defining a `.deepagents/agents/` directory.

Run from the DDR_Bench repo root:

```bash
export no_proxy=localhost,127.0.0.1,10.0.0.0/8,100.96.0.0/12,.pjlab.org.cn
export NO_PROXY="$no_proxy"

dcode --trust-project-mcp -n "$(cat outputs/dcode/company_6201/prompt.txt)" --timeout 3600
```

Start MCP servers first:

```bash
./.venv/bin/python tool_server/sqlite_mcp.py \
  --transport sse \
  --host 127.0.0.1 \
  --port 8765 \
  --data-path ./data/10k/raw/10k_financial_data.db
```

```bash
./.venv/bin/python tool_server/code_mcp.py \
  --transport sse \
  --host 127.0.0.1 \
  --port 8766 \
  --data-path ./data/10k
```
