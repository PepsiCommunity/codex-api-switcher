# Codex Switcher

Windows TUI for switching Codex Desktop between the official subscription mode and OpenAI-compatible API gateways.

The script is meant to live next to Codex Desktop config files in `%USERPROFILE%\.codex`, but this repository keeps the maintainable source copy.

## What It Does

- Switches Codex Desktop to subscription mode (`openai` provider).
- Switches Codex Desktop to API gateway mode through one stable provider id: `custom`.
- Keeps local chats visible by syncing thread provider metadata when changing modes.
- Restores user-created chats hidden under a different provider.
- Switches API gateway models and writes a Codex model catalog.
- Refreshes API model catalog metadata for context window and image input.
- Tests API gateways with text requests and the current gateway with a tiny image.
- Can write experimental API FAST tier catalog/config metadata.
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
2. API gateway: codexcn
3. API gateway: modelhub
T. Test API gateways
M. Change model
C. Refresh API model catalog
F. Toggle API FAST tier
R. Restore hidden user chats
S. Sync all chats to current active provider
Q. Quit
```

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

API keys are not stored in this repository or in the script.

Set keys as Windows User environment variables:

```text
CODEXCN_API_KEY
MODELHUB_API_KEY
```

The script reads those variables at runtime.

## Chat Recovery

`R. Restore hidden user chats` is the safer recovery action. It updates only user-created chats whose stored provider differs from the current provider. Archived chats are optional.

`S. Sync all chats to current active provider` is broader. It updates every local thread row and affected JSONL session to the active provider.

## Models

`M. Change model` changes only:

```toml
model = "<selected model>"
model_catalog_json = "C:\\Users\\K\\.codex\\codex_provider_models.json"
```

It does not modify chat database rows.

In subscription mode the script removes `model_catalog_json` so Codex Desktop can use its built-in model catalog.

`C. Refresh API model catalog` rewrites the generated catalog without changing chats, provider, or model. Use it after updating this script or when Codex Desktop appears to use an unexpectedly small context or refuses image attachments in API mode.

Generated API model entries are written for the active gateway, not for every configured gateway at once. This matters because the same model slug can exist behind multiple gateways with different limits.

Configured context windows:

```text
codexcn   128000
modelhub  400000
```

For the active gateway, entries advertise matching context metadata:

```json
"context_window": "<active gateway limit>",
"max_context_window": "<active gateway limit>",
"truncation_policy": {"mode": "tokens", "limit": "<active gateway limit>"},
"input_modalities": ["text", "image"]
```

The actual usable context and image support still depend on the selected gateway and model.

## API Tests

`T. Test API gateways` can send:

- Text-only `/responses` requests to one or all configured gateways.
- A tiny generated PNG image to the current API gateway through `/responses`.

The test prints HTTP status and a short response body. It never prints API keys.

## API FAST Tier

`F. Toggle API FAST tier` updates the generated model catalog so API gateway models advertise FAST metadata:

```json
"additional_speed_tiers": ["fast"],
"service_tiers": [{"id": "priority", "name": "Fast", "description": "1.5x speed, increased usage"}]
```

When enabled, it also writes:

```toml
service_tier = "priority"
```

When disabled, it writes `service_tier = "default"`.

Important: current Codex Desktop builds gate FAST behind ChatGPT subscription auth. In pure API gateway mode (`requires_openai_auth = false`), Desktop hides the FAST UI and sends `serviceTier = null` for turns, even when this catalog/config metadata exists.

This toggle is kept as an experimental and reversible config option for future Desktop builds, CLI behavior, or manual testing. It is not a pure API Desktop UI unlock.

The toggle state is stored locally in:

```text
%USERPROFILE%\.codex\codex_provider_menu_state.json
```

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
- Do not commit real API keys, `auth.json`, SQLite databases, or session JSONL files.
- This tool is local and Windows-focused.
