# API gateway configs

This folder is where local API gateway configs live.

Tracked files:

- `README.md` — this guide.
- `schema.json` — JSON Schema for gateway config files.
- `openai.example.json` — copyable OpenAI API example.

Local files:

- Create one JSON file per gateway, for example `openai.json` or `my-company-gateway.json`.
- Real `apis/*.json` files are ignored by git, except `schema.json` and `*.example.json`.
- Default key mode is `inline`: store `api_key` directly in the ignored local JSON file.
- Optional key mode is `env`: set `key_mode` to `env`, put the variable name in `env_key`, and store the key in Windows User environment variables.
- For inline configs, `env_key` is optional. If omitted, the menu creates an internal Desktop bridge env var from the gateway id.

Quick start:

1. Copy `openai.example.json` to `openai.json`.
2. Replace `api_key` with your real key, or switch to `key_mode: "env"` and set `OPENAI_API_KEY`.
3. Edit `models`, `default_model`, and `context_window` if needed.
4. Restart the menu.
