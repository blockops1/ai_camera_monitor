# Farm Surveillance v2 — Operator Manual

Operational checklists and post-merge verification steps.

## MCP server live verification (post-068g2)

After US-068g2 merge, operator must verify the listener-spawned MCP server live:

- [ ] Restart listener: `launchctl kickstart -k gui/$UID/com.farm.surveillance.v2`
- [ ] Local probe: `curl -H "Authorization: Bearer $FARMSURV_MCP_TOKEN" http://localhost:8091/healthz` returns JSON
- [ ] Cross-host probe from another Tailscale machine (same bearer token) succeeds — confirms cross-host listener access works
- [ ] Optional: `RUN_INTEGRATION=1 pytest tests/integration/test_mcp_live.py -v` passes
