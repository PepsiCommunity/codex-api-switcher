# Codex Switcher

Windows TUI for switching Codex Desktop between the official subscription mode and OpenAI-compatible API gateways.

The script is meant to live next to Codex Desktop config files in `%USERPROFILE%\.codex`, but this repository keeps the maintainable source copy.

## What It Does

- Switches Codex Desktop to subscription mode (`openai` provider).
- Switches Codex Desktop to API gateway mode through one stable provider id: `custom`.
- Loads API gateways from an editable local JSON file instead of hardcoding them in the script.
- Adds API gateways from the terminal, saves keys to Windows User environment variables, and fetches `/models`.
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
README.md                 This file
```

## Install

Copy these files to `%USERPROFILE%\.codex`:

```text
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

```text
1. Subscription / OpenAI account
2..N. API gateways loaded from codex_switcher_apis.json
M. Change model
T. Tools
D. Diagnostics
Q. Quit
```

Tools:

```text
A. Add API gateway
E. Open API gateways file
T. Test API gateways
R. Restore hidden user chats
S. Sync all chats to current active provider
Q. Back
```

Diagnostics shows the current config, Codex process status, and thread provider counts.

## API Gateway Config

API gateways are stored locally in:

```text
%USERPROFILE%\.codex\codex_switcher_apis.json
```

Use `Tools > A. Add API gateway` to create an entry from the terminal. It asks for the gateway id, base URL, env var name, optional API key, context window, then tries to fetch `/models` and stores the selected default model.

Use `Tools > E. Open API gateways file` if you prefer editing JSON directly. Example format:

```json
{
  "version": 1,
  "gateways": [
    {
      "id": "my-gateway",
      "name": "My Gateway",
      "label": "API gateway: My Gateway",
      "base_url": "https://example.com/v1",
      "env_key": "MY_GATEWAY_API_KEY",
      "models": ["gpt-5.5", "gpt-5.5-mini"],
      "default_model": "gpt-5.5",
      "context_window": 128000,
      "input_modalities": ["text", "image"]
    }
  ]
}
```

All API gateways still write `model_provider = "custom"` to Codex config. The gateway-specific `base_url`, `env_key`, model list, and context metadata come from the JSON file.

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

API keys are not stored in this repository or in `codex_switcher_apis.json`.

`Tools > A. Add API gateway` can save a pasted key into a Windows User environment variable. You can also set it manually:

```text
MY_GATEWAY_API_KEY
```

The script reads those variables at runtime.

## Chat Recovery

`Tools > R. Restore hidden user chats` is the safer recovery action. It updates only user-created chats whose stored provider differs from the current provider. Archived chats are optional.

`Tools > S. Sync all chats to current active provider` is broader. It updates every local thread row and affected JSONL session to the active provider.

## Models

`M. Change model` uses the active gateway's `models` list from `codex_switcher_apis.json` and changes only:

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

`Tools > T. Test API gateways` can:

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
- Do not commit real API keys, `auth.json`, SQLite databases, session JSONL files, or your local `codex_switcher_apis.json`.
- This tool is local and Windows-focused.
