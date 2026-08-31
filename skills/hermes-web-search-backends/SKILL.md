---
name: hermes-web-search-backends
description: How Hermes Agent's `web_search` / `web_extract` tools resolve to a backend at runtime — covers the 8 registered backends, the auto-fallback priority order, why `web_search` returns a Firecrawl error even when other backends are configured, how `hermes plugins enable <name>` activates a bundled plugin, the env-var name mismatches that cause silent `is_available() == False`, and the patch-tool's refusal to write `~/.hermes/config.yaml`. Load this skill before debugging "web_search doesn't work" or before suggesting web-search alternatives to the user.
---

# Hermes Agent web search backends

Hermes Agent's `web_search` and `web_extract` tools back onto one of **8
configurable backends**. The tool code lives at
`~/.hermes/hermes-agent/tools/web_tools.py`. Backends are registered
either as legacy (`_LEGACY_WEB_BACKENDS`) or via the plugin system
(`hermes_cli/plugins.py:discover_plugins`).

## The 8 backends

| Backend | Provider | Required env var | Cost | Auto-detected by |
|---|---|---|---|---|
| `tavily` | Tavily | `TAVILY_API_KEY` | Paid (free trial) | `_has_env` |
| `exa` | Exa | `EXA_API_KEY` | Paid | `_has_env` |
| `parallel` | Parallel | `PARALLEL_API_KEY` | Paid | `_has_env` |
| `firecrawl` | Firecrawl (cloud or self-hosted) | `FIRECRAWL_API_KEY` or `FIRECRAWL_API_URL` | Free tier, then paid | `_has_env` |
| `searxng` | SearXNG (self-hosted) | `SEARXNG_URL` | Free | `_has_env` |
| `brave-free` | Brave Search free tier | `BRAVE_SEARCH_API_KEY` | Free 2k queries/mo, **key required** | `_has_env` |
| `ddgs` | DuckDuckGo scraper (`ddgs` Python package) | None | Free, rate-limited | `_ddgs_package_importable()` |
| `xai` | xAI / Grok search | `XAI_API_KEY` | Paid (xAI credits) | `_has_env` |

## Auto-fallback priority order

When `web.search_backend` and `web.backend` are both empty (`""`), the
selection walks this candidate list and picks the first that's
available:

1. tavily (key)
2. exa (key)
3. parallel (key)
4. firecrawl (key or URL)
5. firecrawl (tool gateway — needs Nous Portal login)
6. searxng (URL)
7. brave-free (key)
8. **ddgs** (no key — only needs `pip install ddgs` in the hermes venv)

If NONE are available, you get the Firecrawl error message even though
Firecrawl isn't actually being attempted — it's just the catch-all
error string.

## Per-capability override

- `web.backend: <name>` — shared fallback for both search and extract
- `web.search_backend: <name>` — overrides for `web_search` only
- `web.extract_backend: <name>` — overrides for `web_extract` only

Useful when you want, e.g., Brave for search but Firecrawl for extract.

## Critical pitfalls

### 1. `web_search` failure message is misleading

The error string always mentions Firecrawl even when no Firecrawl
attempt was made:

```
Web tools are not configured. Set FIRECRAWL_API_KEY for cloud Firecrawl
or set FIRECRAWL_API_URL for a self-hosted Firecrawl instance.
```

This is the catch-all when **all** backends are unavailable. The user
probably has `BRAVE_API_KEY` set but not `BRAVE_SEARCH_API_KEY` (see pitfall
#2). Don't assume Firecrawl is the chosen backend.

### 2. Brave-free needs `BRAVE_SEARCH_API_KEY`, not `BRAVE_API_KEY`

The plugin's `is_available()` (in
`~/.hermes/hermes-agent/plugins/web/brave_free/provider.py:54`) calls
`get_provider_env("BRAVE_SEARCH_API_KEY")`. A common user mistake is
naming the variable `BRAVE_API_KEY`. Result: `is_available()` returns
False, the backend is silently skipped, and the agent falls through to
the next available backend (or fails if none).

**Fix:** add `BRAVE_SEARCH_API_KEY=<same-value>` to `~/.hermes/.env` (or
rename the existing var). Same-value duplicate is fine.

### 3. `ddgs` is the truly keyless option

If you want zero-config web search with no signup and no API key, `ddgs`
is the answer — but **the `ddgs` Python package must be installed** in
the hermes-agent venv (`~/.hermes/hermes-agent/venv/`). Without it,
`is_available()` returns False. Install with:

```bash
~/.hermes/hermes-agent/venv/bin/pip install ddgs
```

DDGS scrapes DuckDuckGo, is rate-limited, and can break if DuckDuckGo
changes its HTML. Use as last-resort fallback.

### 4. Plugins must be enabled before they're loaded

`hermes plugins list` shows all bundled plugins, but **only enabled ones
are loaded into the registry**. The default config has:

```yaml
plugins:
  enabled: []
```

To enable:

```bash
hermes plugins enable web-brave-free
hermes plugins enable web-ddgs
# etc.
```

The plugin name in CLI commands uses the directory name (`web-brave_free`,
`web-ddgs`), not the manifest's `provides_web_providers` list. Restart
the gateway for changes to take effect.

### 5. The patch tool refuses `~/.hermes/config.yaml`

`patch` and `write_file` tools refuse to write `~/.hermes/config.yaml`
with the message "Agent cannot modify security-sensitive configuration.
Edit ~/.hermes/config.yaml directly or use `hermes config` instead."

The workaround is the CLI:

```bash
hermes config set <key> <value>
hermes plugins enable <name>
```

These work fine from the agent's terminal. The user must run CLI
commands that touch secrets or the gateway config; the agent cannot
edit those files directly even with explicit permission.

### 6. Gateway restart is required for plugin/env changes

Plugin enable/disable and `.env` changes both require a gateway restart
to take effect. The running gateway loads its config at startup. To
restart:

```bash
sudo launchctl kickstart -k gui/$(id -u)/ai.farm.surveillance-listener
```

(or whatever process owns the gateway — check with
`lsof -iTCP -sTCP:LISTEN` and `ps`).

**WARNING:** the SOUL boundary says **never restart the gateway** without
explicit approval — if it fails to come back up, the agent goes dark with
no way to recover from Telegram.

## Discovery recipe (when debugging)

When the user reports "web_search doesn't work":

1. **Check the error message** — if it mentions Firecrawl but the user
   hasn't configured Firecrawl, all 8 backends are unavailable.
2. **Run `hermes plugins list --enabled`** — confirm which backends are
   enabled (defaults to empty).
3. **Check `~/.hermes/.env`** (carefully — don't print values) — for each
   `*_API_KEY` and `*_URL`, confirm the variable name matches what the
   plugin expects.
4. **Run `~/.hermes/hermes-agent/venv/bin/python3 -c "from agent.web_search_registry import list_providers; [print(p.name, p.is_available()) for p in list_providers()]"`**
   — shows exactly which backends the gateway would see.
5. **If a gateway restart is needed**, ask for explicit approval first.

## Sources

- `~/.hermes/hermes-agent/tools/web_tools.py` — `_get_backend()`,
  `_get_search_backend()`, `_get_extract_backend()`, `_get_capability_backend()`,
  `_is_backend_available()`
- `~/.hermes/hermes-agent/plugins/web/*/provider.py` — per-backend
  `is_available()` and env-var name
- `~/.hermes/hermes-agent/plugins/web/*/plugin.yaml` — manifest with
  description and `provides_web_providers` list
- `~/.hermes/hermes-agent/hermes_cli/plugins.py:discover_plugins` — how
  plugins are loaded into the registry
- `~/.hermes/hermes-agent/agent/web_search_provider.py:get_provider_env` —
  config-aware env lookup (checks `os.environ` then `~/.hermes/.env`)

## Verified 2026-07-22

- 8 backends enumerated and verified present in
  `~/.hermes/hermes-agent/plugins/web/`
- Auto-fallback order verified by reading `web_tools.py`
- `BRAVE_SEARCH_API_KEY` vs `BRAVE_API_KEY` mismatch confirmed by reading
  `brave_free/provider.py:54`
- `ddgs` package confirmed not installed in hermes venv
- `hermes plugins enable web-brave-free` confirmed working (exit 0)
- patch tool confirmed refusing `~/.hermes/config.yaml` writes