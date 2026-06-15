# Codex Switcher

Windows TUI for switching Codex Desktop between the official subscription mode and OpenAI-compatible API gateways.

The script is meant to live next to Codex Desktop config files in `%USERPROFILE%\.codex`, but this repository keeps the maintainable source copy.

## What It Does

- Switches Codex Desktop to subscription mode (`openai` provider).
- Switches Codex Desktop to API gateway mode through one stable provider id: `custom`.
- Loads API gateways from local JSON files in an `apis/` folder next to the script.
- Adds API gateways from the terminal, stores keys either inline in ignored JSON or in Windows User environment variables, and fetches `/models`.
- Keeps local chats visible by syncing thread provider metadata when changing modes.
- Restores user-created chats hidden under a different provider.
- Switches API gateway models and writes a Codex model catalog.
- Tests selected gateway models and prints short response bodies.
- Creates backups before write actions and rolls back on failure where possible.
- Refuses write actions while `Codex.exe` or `codex.exe` is running.

## Files

```text
codex_provider_menu.cmd   Double-click launcher for Explorer
codex_provider_menu.py    Main TUI implementation
codex_provider_menu.ps1   PowerShell compatibility launcher
apis/README.md            Local API config guide
apis/schema.json          JSON Schema for API configs
apis/openai.example.json  Copyable OpenAI API example
README.md                 This file
```

## Install

Copy these files and folders to `%USERPROFILE%\.codex`:

```text
apis/
codex_provider_menu.cmd
codex_provider_menu.py
codex_provider_menu.ps1
```

Installed path example:

```text
C:\Users\K\.codex\codex_provider_menu.cmd
```

## Run

Close Codex Desktop first, then double-click:

```text
C:\Users\K\.codex\codex_provider_menu.cmd
```

Write actions are disabled while Codex is running because the desktop app can lock database and JSONL session files.

## Menu

Menus use arrow-key navigation:

```text
↑/↓ move
Enter selects
Esc goes back
```

Main menu:

```text
> Subscription / OpenAI account
  API gateways loaded from apis/*.json
  Change model
  Tools
  Diagnostics
  Quit
```

Tools:

```text
> Add API gateway
  Open API gateways folder
  Test API gateways
  Restore hidden user chats
  Sync all chats to current active provider
  Back
```

Diagnostics shows the current config, Codex process status, and thread provider counts.

## API Gateway Configs

API gateways are stored as local JSON files next to the script:

```text
<codex_provider_menu.py directory>\apis\*.json
```

When installed normally, that is:

```text
%USERPROFILE%\.codex\apis\*.json
```

Real `apis/*.json` files are ignored by git. The repository only tracks:

```text
apis/README.md
apis/schema.json
apis/openai.example.json
```

Use `Tools > Add API gateway` to create `apis/<gateway-id>.json` from the terminal. It asks for the gateway id, base URL, key mode, API key, context window, then tries to fetch `/models` and stores the selected default model.

Use `Tools > Open API gateways folder` if you prefer editing JSON directly.

OpenAI API example using the default inline key mode:

```json
{
  "$schema": "./schema.json",
  "id": "openai",
  "name": "OpenAI API",
  "label": "API gateway: OpenAI API",
  "base_url": "https://api.openai.com/v1",
  "key_mode": "inline",
  "api_key": "sk-your-openai-api-key",
  "models": ["gpt-4.1", "gpt-4.1-mini"],
  "default_model": "gpt-4.1",
  "context_window": 128000,
  "input_modalities": ["text", "image"]
}
```

All API gateways still write `model_provider = "custom"` to Codex config. The gateway-specific `base_url`, model list, and context metadata come from the JSON files. For inline keys, `env_key` is optional; if omitted, the script generates an internal Desktop bridge env var from the gateway id and persists it when applying.

## Modes

Subscription mode writes:

```toml
model_provider = "openai"
```

API gateway modes write:

```toml
model_provider = "custom"

[model_providers.custom]
name = "<gateway name>"
base_url = "<gateway url>"
env_key = "<gateway env var>"
wire_api = "responses"
requires_openai_auth = false
```

The script intentionally uses only one API provider id, `custom`, so chats do not disappear when switching between gateways.

## API Keys

Default key mode is `inline`: the key is stored in the local ignored gateway JSON file as `api_key`, with no manual env var setup required.

Optional key mode is `env`: set `key_mode` to `env`, set `env_key`, and store the key in Windows User environment variables.

The script never prints API keys. Do not commit local `apis/*.json` files.

## Chat Recovery

`Tools > Restore hidden user chats` is the safer recovery action. It updates only user-created chats whose stored provider differs from the current provider. Archived chats are optional.

`Tools > Sync all chats to current active provider` is broader. It updates every local thread row and affected JSONL session to the active provider.

## Models

`Change model` uses the active gateway's `models` list from its local API JSON file and changes only:

```toml
model = "<selected model>"
model_catalog_json = "C:\\Users\\K\\.codex\\codex_provider_models.json"
```

It does not modify chat database rows.

In subscription mode the script removes `model_catalog_json` so Codex Desktop can use its built-in model catalog.

Generated API model entries are written for the active gateway, not for every configured gateway at once. This matters because the same model slug can exist behind multiple gateways with different limits.

For the active gateway, entries advertise metadata from the JSON file:

```json
"context_window": 128000,
"max_context_window": 128000,
"truncation_policy": {"mode": "tokens", "limit": 128000},
"input_modalities": ["text", "image"]
```

The actual usable context and image support still depend on the selected gateway and model.

## API Tests

`Tools > Test API gateways` can:

- Fetch live `/models` for a selected gateway.
- Test a selected model with a text-only `/responses` request.
- Test a selected model with a tiny generated PNG image through `/responses`.
- Test default models across all configured gateways.

The test prints HTTP status and a short response body. It never prints API keys.

## Backups

Before write actions, the script backs up important Codex Desktop state:

```text
state_5.sqlite
config.toml
session_index.jsonl
.codex-global-state.json
affected JSONL sessions
```

Backups are written under `%USERPROFILE%\.codex` with names like:

```text
backup-*-provider-menu-to-*
backup-*-provider-menu-model-*
```

## Safety Notes

- Close Codex Desktop before applying changes.
- Keep backups until you have verified chats and provider switching.
- Do not commit real API keys, `auth.json`, SQLite databases, session JSONL files, or local `apis/*.json` gateway files.
- This tool is local and Windows-focused.

