#!/usr/bin/env python3
"""
Windows TUI for switching Codex Desktop providers.

Operational model:
- The active provider id controls which local Desktop chats are visible.
- To keep all chats visible after each switch, this script syncs all local
  thread metadata to the selected provider id.
- Writes are refused while Codex.exe/codex.exe is running for the real
  %USERPROFILE%\\.codex, because active rollout JSONL files can be locked.
"""

from __future__ import annotations

import ctypes
import csv
import io
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import winreg
import zipfile
import binascii
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_TITLE = "Codex Provider Menu"
TECHNICAL_TITLE_PREFIX = (
    "The following is the Codex agent history whose request action you are assessing"
)


class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"


def enable_windows_colors() -> bool:
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


USE_COLOR = bool(sys.stdout.isatty() and not os.environ.get("NO_COLOR") and enable_windows_colors())


def color(text: str, *styles: str) -> str:
    if not USE_COLOR or not styles:
        return text
    return "".join(styles) + text + Style.RESET


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    provider: str
    kind: str
    name: str | None = None
    base_url: str | None = None
    env_key: str | None = None


PROFILES: list[Profile] = [
    Profile(
        id="subscription",
        label="Subscription / OpenAI account",
        provider="openai",
        kind="subscription",
    ),
    Profile(
        id="codexcn",
        label="API gateway: codexcn",
        provider="custom",
        kind="api",
        name="codexcn",
        base_url="https://codexcn.top/ai/v1",
        env_key="CODEXCN_API_KEY",
    ),
    Profile(
        id="modelhub",
        label="API gateway: modelhub",
        provider="custom",
        kind="api",
        name="modelhub",
        base_url="https://modelhub.my/v1",
        env_key="MODELHUB_API_KEY",
    ),
]


GATEWAY_MODELS: dict[str, list[str]] = {
    "codexcn": [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.5",
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M3",
    ],
    "modelhub": [
        "claude-haiku-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-7-fast",
        "claude-opus-4-8",
        "claude-sonnet-4-6",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "glm-5.1",
        "gpt-5.3-codex-spark",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.5",
        "gpt-5.5-xhigh-fast",
        "kimi-k2.5",
        "kimi-k2.6",
        "kimi-k2.7-code",
        "qwen3.6-plus",
    ],
}

MODEL_EXCLUDE_FROM_AGENT_MENU = {"gpt-image-2"}
GATEWAY_CONTEXT_WINDOWS = {
    "codexcn": 128000,
    "modelhub": 400000,
}
DEFAULT_MODEL_CONTEXT_WINDOW = 128000
MODEL_INPUT_MODALITIES = ["text", "image"]
API_TEST_USER_AGENT = "Codex Provider Menu/1.0"


class ProviderMenuError(RuntimeError):
    pass


def default_codex_home() -> Path:
    return Path.home() / ".codex"


def get_codex_home() -> Path:
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    script_dir = Path(__file__).resolve().parent
    if script_dir.name.lower() == ".codex" and (script_dir / "config.toml").exists():
        return script_dir
    return default_codex_home()


CODEX_HOME = get_codex_home()
CONFIG_PATH = CODEX_HOME / "config.toml"
DB_PATH = CODEX_HOME / "sqlite" / "state_5.sqlite"
if not DB_PATH.exists():
    DB_PATH = CODEX_HOME / "state_5.sqlite"
SESSION_INDEX_PATH = CODEX_HOME / "session_index.jsonl"
MODEL_CATALOG_PATH = CODEX_HOME / "codex_provider_models.json"


def clear_screen() -> None:
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


def pause(message: str = "Press Enter to continue...") -> None:
    try:
        input(message)
    except EOFError:
        pass


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        try:
            answer = input(prompt + suffix).strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print(color("Enter y or n.", Style.YELLOW))


def normalize_windows_extended_path(path: str) -> str:
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def is_real_codex_home() -> bool:
    try:
        return CODEX_HOME.resolve().samefile(default_codex_home().resolve())
    except Exception:
        return str(CODEX_HOME).lower() == str(default_codex_home()).lower()


def list_codex_processes() -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Codex.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        result2 = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq codex.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return []

    processes: list[dict[str, str]] = []
    seen_pids: set[str] = set()
    for output in [result.stdout, result2.stdout]:
        for row in csv.reader(io.StringIO(output)):
            if len(row) < 2:
                continue
            image = row[0].strip()
            pid = row[1].strip()
            if image.lower() in {"codex.exe"} and pid not in seen_pids:
                processes.append({"image": image, "pid": pid})
                seen_pids.add(pid)
    return processes


def assert_codex_not_running_for_writes() -> None:
    if not is_real_codex_home():
        return
    processes = list_codex_processes()
    if not processes:
        return

    details = ", ".join(f"{p['image']}:{p['pid']}" for p in processes)
    raise ProviderMenuError(
        "Codex is running. Close Codex Desktop and all codex.exe processes, "
        f"then run this menu again. Running processes: {details}"
    )


def read_config_lines() -> list[str]:
    if not CONFIG_PATH.exists():
        raise ProviderMenuError(f"config.toml not found: {CONFIG_PATH}")
    return CONFIG_PATH.read_text(encoding="utf-8", errors="strict").splitlines()


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def set_top_level_value(lines: list[str], key: str, value: Any) -> list[str]:
    output = list(lines)
    first_section = len(output)
    for index, line in enumerate(output):
        if line.lstrip().startswith("["):
            first_section = index
            break

    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    for index in range(first_section):
        if pattern.match(output[index].strip()):
            output[index] = f"{key} = {toml_value(value)}"
            return output

    output.insert(first_section, f"{key} = {toml_value(value)}")
    return output


def remove_top_level_value(lines: list[str], key: str) -> list[str]:
    output = list(lines)
    first_section = len(output)
    for index, line in enumerate(output):
        if line.lstrip().startswith("["):
            first_section = index
            break

    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    return [
        line
        for index, line in enumerate(output)
        if not (index < first_section and pattern.match(line.strip()))
    ]


def upsert_section_values(lines: list[str], section: str, values: dict[str, Any]) -> list[str]:
    output = list(lines)
    header = f"[{section}]"
    start = -1
    for index, line in enumerate(output):
        if line.strip() == header:
            start = index
            break

    if start < 0:
        if output and output[-1].strip():
            output.append("")
        output.append(header)
        for key, value in values.items():
            output.append(f"{key} = {toml_value(value)}")
        return output

    end = len(output)
    for index in range(start + 1, len(output)):
        if output[index].lstrip().startswith("["):
            end = index
            break

    done: set[str] = set()
    for index in range(start + 1, end):
        stripped = output[index].strip()
        for key, value in values.items():
            if re.match(rf"^{re.escape(key)}\s*=", stripped):
                output[index] = f"{key} = {toml_value(value)}"
                done.add(key)

    for key, value in values.items():
        if key not in done:
            output.insert(end, f"{key} = {toml_value(value)}")
            end += 1

    return output


def atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def parse_toml_bool(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None


def parse_config_status() -> dict[str, Any]:
    lines = read_config_lines()
    status = {
        "model_provider": "",
        "model": "",
        "model_catalog_json": "",
        "providers": {},
    }
    section = ""
    provider_id = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
            match = re.match(r"^\[model_providers\.([^\]]+)\]$", stripped)
            provider_id = match.group(1) if match else ""
            if provider_id:
                status["providers"].setdefault(provider_id, {})
            continue
        if not section:
            match = re.match(r'^model_provider\s*=\s*["\']([^"\']+)["\']', stripped)
            if match:
                status["model_provider"] = match.group(1)
            match = re.match(r'^model\s*=\s*["\']([^"\']+)["\']', stripped)
            if match:
                status["model"] = match.group(1)
            match = re.match(r'^model_catalog_json\s*=\s*["\']([^"\']+)["\']', stripped)
            if match:
                status["model_catalog_json"] = match.group(1)
        elif provider_id:
            provider = status["providers"].setdefault(provider_id, {})
            for key in ["name", "base_url", "env_key", "wire_api"]:
                match = re.match(rf'^{key}\s*=\s*["\']([^"\']+)["\']', stripped)
                if match:
                    provider[key] = match.group(1)
            match = re.match(r"^requires_openai_auth\s*=\s*(.+)$", stripped)
            if match:
                provider["requires_openai_auth"] = parse_toml_bool(match.group(1))
    return status


def active_provider_config(status: dict[str, Any]) -> dict[str, Any]:
    providers = status.get("providers")
    if not isinstance(providers, dict):
        return {}
    provider = providers.get(status.get("model_provider"))
    return provider if isinstance(provider, dict) else {}


def active_profile(status: dict[str, Any]) -> Profile | None:
    for profile in PROFILES:
        if is_profile_active(profile, status):
            return profile
    return None


def auth_mode_label(requires_openai_auth: bool | None) -> str:
    if requires_openai_auth is True:
        return "requires_openai_auth=true"
    if requires_openai_auth is False:
        return "requires_openai_auth=false"
    return "requires_openai_auth=unknown"


def is_profile_active(profile: Profile, status: dict[str, Any]) -> bool:
    if status.get("model_provider") != profile.provider:
        return False
    if profile.kind != "api":
        return True
    provider = active_provider_config(status)
    return (
        provider.get("base_url") == profile.base_url
        and provider.get("env_key") == profile.env_key
        and provider.get("requires_openai_auth") is False
    )


def format_menu_item(key: str, label: str, active: bool = False) -> str:
    marker = color("[active]", Style.GREEN, Style.BOLD) if active else color("[ ]", Style.DIM)
    key_text = color(key, Style.CYAN, Style.BOLD)
    label_text = color(label, Style.BOLD) if active else label
    return f"  {key_text}. {label_text} {marker}"


def display_model_name(model: str) -> str:
    known = {
        "gpt-5.5": "GPT-5.5",
        "gpt-5.4": "GPT-5.4",
        "gpt-5.4-mini": "GPT-5.4-Mini",
        "gpt-5.3-codex-spark": "GPT-5.3 Codex Spark",
        "gpt-5.5-xhigh-fast": "GPT-5.5 XHigh Fast",
        "MiniMax-M2.7": "MiniMax M2.7",
        "MiniMax-M2.7-highspeed": "MiniMax M2.7 Highspeed",
        "MiniMax-M3": "MiniMax M3",
    }
    if model in known:
        return known[model]
    return model.replace("-", " ").replace("_", " ").title()


def all_gateway_models() -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for gateway_models in GATEWAY_MODELS.values():
        for model in gateway_models:
            if model in MODEL_EXCLUDE_FROM_AGENT_MENU or model in seen:
                continue
            models.append(model)
            seen.add(model)
    return models


def models_for_active_profile(status: dict[str, Any]) -> tuple[str, list[str]]:
    profile = active_profile(status)
    if not profile or profile.kind != "api":
        return "", []
    return profile.id, list(GATEWAY_MODELS.get(profile.id, []))


def resolve_catalog_gateway_id(gateway_id: str | None = None) -> str:
    if gateway_id:
        return gateway_id
    try:
        status = parse_config_status()
    except Exception:
        return ""
    profile = active_profile(status)
    if profile and profile.kind == "api":
        return profile.id
    return ""


def models_for_catalog_gateway(gateway_id: str) -> list[str]:
    if gateway_id in GATEWAY_MODELS:
        return [
            model
            for model in GATEWAY_MODELS[gateway_id]
            if model not in MODEL_EXCLUDE_FROM_AGENT_MENU
        ]
    return all_gateway_models()


def context_window_for_gateway(gateway_id: str) -> int:
    return GATEWAY_CONTEXT_WINDOWS.get(gateway_id, DEFAULT_MODEL_CONTEXT_WINDOW)


def model_catalog_entry(
    model: str,
    priority: int,
    gateway_id: str,
) -> dict[str, Any]:
    context_window = context_window_for_gateway(gateway_id)
    return {
        "slug": model,
        "display_name": display_model_name(model),
        "description": f"Model exposed by the configured {gateway_id or 'API'} gateway.",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Fast responses with lighter reasoning"},
            {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
            {"effort": "high", "description": "Greater reasoning depth for complex problems"},
            {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
        ],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": "You are Codex, a coding agent.",
        "model_messages": {
            "instructions_template": "You are Codex, a coding agent.",
            "instructions_variables": {},
        },
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "medium",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": context_window},
        "supports_parallel_tool_calls": True,
        "supports_image_detail_original": True,
        "context_window": context_window,
        "max_context_window": context_window,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": MODEL_INPUT_MODALITIES,
        "supports_search_tool": True,
        "use_responses_lite": False,
    }


def build_model_catalog(gateway_id: str | None = None) -> dict[str, Any]:
    resolved_gateway_id = resolve_catalog_gateway_id(gateway_id)
    return {
        "models": [
            model_catalog_entry(
                model,
                priority=100 + index,
                gateway_id=resolved_gateway_id,
            )
            for index, model in enumerate(models_for_catalog_gateway(resolved_gateway_id))
        ]
    }


def write_model_catalog(gateway_id: str | None = None) -> None:
    content = json.dumps(
        build_model_catalog(gateway_id=gateway_id),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    atomic_write_text(MODEL_CATALOG_PATH, content)


def update_config_model(model: str) -> None:
    lines = read_config_lines()
    lines = set_top_level_value(lines, "model", model)
    lines = set_top_level_value(lines, "model_catalog_json", str(MODEL_CATALOG_PATH))
    lines = remove_top_level_value(lines, "service_tier")
    atomic_write_text(CONFIG_PATH, "\n".join(lines) + "\n")


def connect_db(readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_thread_rows(provider_not: str | None = None) -> list[sqlite3.Row]:
    conn = connect_db(readonly=True)
    try:
        if provider_not is None:
            rows = list(
                conn.execute(
                    """
                    select *
                    from threads
                    order by coalesce(updated_at_ms, updated_at * 1000), id
                    """
                )
            )
        else:
            rows = list(
                conn.execute(
                    """
                    select *
                    from threads
                    where model_provider != ?
                    order by coalesce(updated_at_ms, updated_at * 1000), id
                    """,
                    (provider_not,),
                )
            )
    finally:
        conn.close()
    return rows


def thread_timestamp_seconds(row: sqlite3.Row) -> float:
    ms = row["updated_at_ms"] if row["updated_at_ms"] is not None else row["updated_at"] * 1000
    return float(ms) / 1000


def timestamp_iso(row: sqlite3.Row) -> str:
    return (
        datetime.fromtimestamp(thread_timestamp_seconds(row), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def shorten_title(title: str, limit: int = 160) -> str:
    title = (title or "").replace("\r\n", "\n")
    parts = [part.strip() for part in title.split("\n") if part.strip()]
    short = parts[0] if parts else title.strip()
    if len(short) > limit:
        return short[: limit - 3].rstrip() + "..."
    return short


def safe_arcname(path: Path, thread_id: str) -> str:
    try:
        return str(path.relative_to(CODEX_HOME)).replace("\\", "/")
    except ValueError:
        return f"_outside_codex_home/{thread_id}-{path.name}"


def backup_sqlite(dest: Path) -> None:
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create_backup(affected_rows: list[sqlite3.Row], target_provider: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = CODEX_HOME / f"backup-{stamp}-provider-menu-to-{target_provider}"
    backup_dir.mkdir(parents=False, exist_ok=False)

    backup_sqlite(backup_dir / "state_5.sqlite")

    for name in [
        "config.toml",
        "session_index.jsonl",
        ".codex-global-state.json",
        "codex_provider_models.json",
    ]:
        src = CODEX_HOME / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)

    manifest: list[dict[str, Any]] = []
    with zipfile.ZipFile(backup_dir / "affected-session-jsonl.zip", "w", zipfile.ZIP_DEFLATED) as zipf:
        for row in affected_rows:
            path = Path(normalize_windows_extended_path(row["rollout_path"]))
            item: dict[str, Any] = {
                "id": row["id"],
                "rollout_path": row["rollout_path"],
                "normalized_path": str(path),
                "exists": path.exists(),
            }
            if path.exists():
                arcname = safe_arcname(path, row["id"])
                zipf.write(path, arcname)
                item["arcname"] = arcname
                item["size"] = path.stat().st_size
            manifest.append(item)

    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup_dir


def restore_backup(backup_dir: Path) -> None:
    db_backup = backup_dir / "state_5.sqlite"
    if db_backup.exists():
        shutil.copy2(db_backup, DB_PATH)

    for name in [
        "config.toml",
        "session_index.jsonl",
        ".codex-global-state.json",
        "codex_provider_models.json",
    ]:
        src = backup_dir / name
        if src.exists():
            shutil.copy2(src, CODEX_HOME / name)

    manifest_path = backup_dir / "manifest.json"
    zip_path = backup_dir / "affected-session-jsonl.zip"
    if manifest_path.exists() and zip_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with zipfile.ZipFile(zip_path, "r") as zipf:
            for item in manifest:
                if not item.get("exists") or "arcname" not in item:
                    continue
                dest = Path(item["normalized_path"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zipf.open(item["arcname"]) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def preflight_writable(affected_rows: list[sqlite3.Row]) -> None:
    if not CONFIG_PATH.exists():
        raise ProviderMenuError(f"Missing config: {CONFIG_PATH}")
    if not DB_PATH.exists():
        raise ProviderMenuError(f"Missing DB: {DB_PATH}")

    for path in [CONFIG_PATH, DB_PATH]:
        with path.open("rb"):
            pass

    for row in affected_rows:
        path = Path(normalize_windows_extended_path(row["rollout_path"]))
        if not path.exists():
            raise ProviderMenuError(f"Missing rollout JSONL for {row['id']}: {path}")
        try:
            with path.open("r+b"):
                pass
        except PermissionError as exc:
            raise ProviderMenuError(
                f"Rollout JSONL is locked or not writable: {path}. "
                "Close Codex Desktop and retry."
            ) from exc


def rewrite_jsonl_provider(row: sqlite3.Row, target_provider: str) -> bool:
    path = Path(normalize_windows_extended_path(row["rollout_path"]))
    with path.open("r", encoding="utf-8", errors="strict", newline="") as file:
        first = file.readline()
        rest = file.read()

    meta = json.loads(first)
    payload = meta.setdefault("payload", {})
    if payload.get("model_provider") == target_provider:
        os.utime(path, (thread_timestamp_seconds(row), thread_timestamp_seconds(row)))
        return False

    payload["model_provider"] = target_provider
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp-provider-menu", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + "\n")
            file.write(rest)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    os.utime(path, (thread_timestamp_seconds(row), thread_timestamp_seconds(row)))
    return True


def update_db_provider(target_provider: str) -> int:
    conn = connect_db(readonly=False)
    try:
        cur = conn.execute(
            "update threads set model_provider = ? where model_provider != ?",
            (target_provider, target_provider),
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def fetch_hidden_user_rows(target_provider: str, include_archived: bool = False) -> list[sqlite3.Row]:
    conn = connect_db(readonly=True)
    try:
        archived_sql = "" if include_archived else "and archived = 0"
        return list(
            conn.execute(
                f"""
                select *
                from threads
                where thread_source = 'user'
                  and model_provider != ?
                  {archived_sql}
                order by archived asc, coalesce(updated_at_ms, updated_at * 1000) desc, id
                """,
                (target_provider,),
            )
        )
    finally:
        conn.close()


def update_thread_rows_provider(rows: list[sqlite3.Row], target_provider: str) -> int:
    if not rows:
        return 0
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    conn = connect_db(readonly=False)
    try:
        cur = conn.execute(
            f"update threads set model_provider = ? where id in ({placeholders})",
            [target_provider, *ids],
        )
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def load_session_index() -> list[dict[str, Any]]:
    if not SESSION_INDEX_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in SESSION_INDEX_PATH.read_text(encoding="utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        thread_id = item.get("id")
        if isinstance(thread_id, str) and thread_id and thread_id not in seen:
            rows.append(item)
            seen.add(thread_id)
    return rows


def sync_session_index() -> tuple[int, int]:
    rows = load_session_index()
    by_id = {item.get("id"): item for item in rows}
    all_threads = fetch_thread_rows()
    added = 0
    normalized = 0

    for row in all_threads:
        title = row["title"] or ""
        if row["thread_source"] != "user":
            continue
        if title.startswith(TECHNICAL_TITLE_PREFIX):
            continue
        if row["id"] not in by_id:
            item = {
                "id": row["id"],
                "thread_name": shorten_title(title),
                "updated_at": timestamp_iso(row),
            }
            rows.append(item)
            by_id[row["id"]] = item
            added += 1

    for item in rows:
        old = str(item.get("thread_name") or "")
        new = shorten_title(old)
        if old != new:
            item["thread_name"] = new
            normalized += 1

    rows.sort(key=lambda item: str(item.get("updated_at") or ""))
    content = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in rows
    )
    atomic_write_text(SESSION_INDEX_PATH, content)
    return added, normalized


def build_config_for_profile(profile: Profile) -> str:
    lines = read_config_lines()
    lines = set_top_level_value(lines, "model_provider", profile.provider)
    lines = remove_top_level_value(lines, "service_tier")
    if profile.kind == "api":
        lines = set_top_level_value(lines, "model_catalog_json", str(MODEL_CATALOG_PATH))
        if not (profile.name and profile.base_url and profile.env_key):
            raise ProviderMenuError(f"API profile is incomplete: {profile.id}")
        current_model = parse_config_status().get("model") or "gpt-5.5"
        available_models = GATEWAY_MODELS.get(profile.id, [])
        if current_model not in available_models:
            lines = set_top_level_value(lines, "model", "gpt-5.5")
        lines = upsert_section_values(
            lines,
            "model_providers.custom",
            {
                "name": profile.name,
                "base_url": profile.base_url,
                "env_key": profile.env_key,
                "wire_api": "responses",
                "requires_openai_auth": False,
            },
        )
    else:
        lines = remove_top_level_value(lines, "model_catalog_json")
    return "\n".join(lines) + "\n"


def get_user_env_var(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    if os.name != "nt":
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            raw, _ = winreg.QueryValueEx(key, name)
        return str(raw)
    except FileNotFoundError:
        return ""


def get_api_key(profile: Profile) -> str:
    if not profile.env_key:
        return ""
    return get_user_env_var(profile.env_key).strip()


def apply_profile(profile: Profile) -> None:
    assert_codex_not_running_for_writes()
    affected = fetch_thread_rows(provider_not=profile.provider)
    preflight_writable(affected)

    config_text = build_config_for_profile(profile)
    backup_dir = create_backup(affected, profile.provider)
    print(f"Backup: {backup_dir}")

    try:
        if profile.kind == "api":
            write_model_catalog(gateway_id=profile.id)
            if not (profile.env_key and get_api_key(profile)):
                raise ProviderMenuError(
                    f"API key is missing. Set User environment variable: {profile.env_key}"
                )
            print(f"Using User env var: {profile.env_key}")

        changed_jsonl = 0
        for row in affected:
            if rewrite_jsonl_provider(row, profile.provider):
                changed_jsonl += 1

        db_updated = update_db_provider(profile.provider)
        index_added, index_normalized = sync_session_index()
        atomic_write_text(CONFIG_PATH, config_text)
    except Exception:
        print("Apply failed. Restoring backup...")
        try:
            restore_backup(backup_dir)
            print("Rollback completed.")
        except Exception as rollback_error:
            print(f"Rollback failed: {rollback_error}")
            print(f"Manual backup: {backup_dir}")
        raise

    print("Applied.")
    print(f"Active provider: {profile.provider}")
    if profile.kind == "api":
        print(f"Active gateway: {profile.base_url}")
    print(f"Threads synced to provider: {db_updated}")
    print(f"JSONL files updated: {changed_jsonl}")
    print(f"session_index rows added: {index_added}")
    print(f"session_index names normalized: {index_normalized}")
    print("Restart Codex Desktop before using the new mode.")


def sync_to_current_provider() -> None:
    status = parse_config_status()
    provider = status["model_provider"]
    if not provider:
        raise ProviderMenuError("Cannot detect current model_provider in config.toml")
    profile = Profile(id="current", label=f"Current provider: {provider}", provider=provider, kind="current")
    apply_profile(profile)


def restore_hidden_user_chats(include_archived: bool = False) -> None:
    status = parse_config_status()
    target_provider = status["model_provider"]
    if not target_provider:
        raise ProviderMenuError("Cannot detect current model_provider in config.toml")

    affected = fetch_hidden_user_rows(target_provider, include_archived=include_archived)
    if not affected:
        print("No hidden user chats found for the current provider.")
        return

    assert_codex_not_running_for_writes()
    preflight_writable(affected)

    backup_dir = create_backup(affected, target_provider)
    print(f"Backup: {backup_dir}")

    try:
        changed_jsonl = 0
        for row in affected:
            if rewrite_jsonl_provider(row, target_provider):
                changed_jsonl += 1

        db_updated = update_thread_rows_provider(affected, target_provider)
        index_added, index_normalized = sync_session_index()
    except Exception:
        print("Restore failed. Restoring backup...")
        try:
            restore_backup(backup_dir)
            print("Rollback completed.")
        except Exception as rollback_error:
            print(f"Rollback failed: {rollback_error}")
            print(f"Manual backup: {backup_dir}")
        raise

    print("Restored hidden user chats.")
    print(f"Target provider: {target_provider}")
    print(f"Threads updated: {db_updated}")
    print(f"JSONL files updated: {changed_jsonl}")
    print(f"session_index rows added: {index_added}")
    print(f"session_index names normalized: {index_normalized}")
    print("Restart Codex Desktop before checking the sidebar.")


def backup_model_settings() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = CODEX_HOME / f"backup-{stamp}-provider-menu-model"
    backup_dir.mkdir(parents=False, exist_ok=False)
    for path in [CONFIG_PATH, MODEL_CATALOG_PATH]:
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)
    return backup_dir


def restore_model_settings(backup_dir: Path) -> None:
    for name in ["config.toml", "codex_provider_models.json"]:
        src = backup_dir / name
        dest = CODEX_HOME / name
        if src.exists():
            shutil.copy2(src, dest)
        elif dest.exists():
            dest.unlink()


def apply_model(model: str) -> None:
    status = parse_config_status()
    gateway_id, available_models = models_for_active_profile(status)
    if not gateway_id:
        raise ProviderMenuError("Current provider is not one of the configured TUI profiles.")
    if model not in available_models:
        raise ProviderMenuError(f"Model {model!r} is not listed for the current gateway: {gateway_id}")

    assert_codex_not_running_for_writes()
    if not CONFIG_PATH.exists():
        raise ProviderMenuError(f"Missing config: {CONFIG_PATH}")

    backup_dir = backup_model_settings()
    print(f"Backup: {backup_dir}")

    try:
        write_model_catalog(gateway_id=gateway_id)
        update_config_model(model)
    except Exception:
        print("Model switch failed. Restoring backup...")
        try:
            restore_model_settings(backup_dir)
            print("Rollback completed.")
        except Exception as rollback_error:
            print(f"Rollback failed: {rollback_error}")
            print(f"Manual backup: {backup_dir}")
        raise

    print("Model updated.")
    print(f"Current gateway: {gateway_id}")
    print(f"Active model: {model}")
    print(f"Model catalog: {MODEL_CATALOG_PATH}")
    print("Restart Codex Desktop before using the new model.")


def refresh_model_catalog() -> None:
    assert_codex_not_running_for_writes()
    status = parse_config_status()
    if status.get("model_provider") != "custom":
        raise ProviderMenuError("Switch to an API gateway profile before refreshing the API model catalog.")
    gateway_id, _ = models_for_active_profile(status)
    if not gateway_id:
        raise ProviderMenuError("Current API gateway is not one of the configured TUI profiles.")

    backup_dir = backup_model_settings()
    print(f"Backup: {backup_dir}")

    try:
        write_model_catalog(gateway_id=gateway_id)
        lines = read_config_lines()
        lines = set_top_level_value(lines, "model_catalog_json", str(MODEL_CATALOG_PATH))
        lines = remove_top_level_value(lines, "service_tier")
        atomic_write_text(CONFIG_PATH, "\n".join(lines) + "\n")
    except Exception:
        print("Catalog refresh failed. Restoring backup...")
        try:
            restore_model_settings(backup_dir)
            print("Rollback completed.")
        except Exception as rollback_error:
            print(f"Rollback failed: {rollback_error}")
            print(f"Manual backup: {backup_dir}")
        raise

    print("API model catalog refreshed.")
    print(f"Catalog gateway: {gateway_id}")
    print(f"Context truncation limit: {context_window_for_gateway(gateway_id)}")
    print(f"Input modalities: {', '.join(MODEL_INPUT_MODALITIES)}")
    print(f"Model catalog: {MODEL_CATALOG_PATH}")
    print("Restart Codex Desktop before checking context or image upload.")


def provider_counts() -> list[tuple[str, str, int, int]]:
    if not DB_PATH.exists():
        return []
    conn = connect_db(readonly=True)
    try:
        return [
            (row[0], row[1], int(row[2]), int(row[3]))
            for row in conn.execute(
                """
                select model_provider, thread_source, archived, count(*)
                from threads
                group by model_provider, thread_source, archived
                order by model_provider, thread_source, archived
                """
            )
        ]
    finally:
        conn.close()


def show_status(include_thread_providers: bool = False) -> None:
    clear_screen()
    print(color(APP_TITLE, Style.CYAN, Style.BOLD))
    print(f"{color('Codex home:', Style.DIM)} {CODEX_HOME}")
    print()

    if CONFIG_PATH.exists():
        status = parse_config_status()
        provider = active_provider_config(status)
        print(color("Current config:", Style.BOLD))
        print(f"  model_provider: {status['model_provider']}")
        print(f"  model:          {status['model']}")
        print(f"  provider name:  {provider.get('name', '')}")
        print(f"  provider url:   {provider.get('base_url', '')}")
        print(f"  provider env:   {provider.get('env_key', '')}")
        print(f"  auth mode:      {auth_mode_label(provider.get('requires_openai_auth'))}")
        print(f"  model catalog:  {status.get('model_catalog_json', '')}")
    else:
        print(color(f"config.toml not found: {CONFIG_PATH}", Style.RED, Style.BOLD))
    print()

    if include_thread_providers:
        counts = provider_counts()
        if counts:
            print(color("Thread providers:", Style.BOLD))
            for provider, source, archived, count in counts:
                print(f"  provider={provider!r} source={source!r} archived={archived}: {count}")
        else:
            print(color("Thread providers: unavailable", Style.YELLOW))
        print()

    processes = list_codex_processes()
    if processes and is_real_codex_home():
        print(color("Codex process status: RUNNING", Style.YELLOW, Style.BOLD))
        print(color("  Write actions are disabled until Codex is closed.", Style.YELLOW))
        print("  " + ", ".join(f"{p['image']}:{p['pid']}" for p in processes))
    else:
        print(color("Codex process status: not detected", Style.GREEN))
    print()


def run_with_spinner(label: str, func, timeout: int = 30) -> tuple[bool, Any]:
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue()

    def runner() -> None:
        try:
            result_queue.put((True, func()))
        except Exception as exc:
            result_queue.put((False, exc))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()

    start = time.monotonic()
    spinner = "|/-\\"
    index = 0
    while thread.is_alive():
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            print(f"\r{label}: timeout after {timeout}s".ljust(80))
            return False, TimeoutError(f"timeout after {timeout}s")
        print(f"\r{label}: running {spinner[index % len(spinner)]} {elapsed:0.1f}s", end="")
        time.sleep(0.2)
        index += 1
    print("\r" + " " * 80 + "\r", end="")
    return result_queue.get()


def make_test_png_data_url() -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        checksum = binascii.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    width = 2
    height = 2
    rgb = bytes((255, 0, 0))
    raw = b"".join(b"\x00" + rgb * width for _ in range(height))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + binascii.b2a_base64(png, newline=False).decode("ascii")


def post_api_json(profile: Profile, path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    assert profile.base_url
    api_key = get_api_key(profile)
    if not api_key:
        return {
            "profile": profile.id,
            "status": "error",
            "elapsed": 0,
            "error": f"Missing User environment variable: {profile.env_key}",
        }

    request = urllib.request.Request(
        profile.base_url.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": API_TEST_USER_AGENT,
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return {
                "profile": profile.id,
                "status": "ok",
                "http_status": response.status,
                "elapsed": round(time.monotonic() - started, 2),
                "body": body,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        return {
            "profile": profile.id,
            "status": "http_error",
            "http_status": exc.code,
            "elapsed": round(time.monotonic() - started, 2),
            "body": body,
        }
    except Exception as exc:
        return {
            "profile": profile.id,
            "status": "error",
            "elapsed": round(time.monotonic() - started, 2),
            "error": repr(exc),
        }


def test_api_profile(profile: Profile, timeout: int = 30, include_image: bool = False) -> dict[str, Any]:
    if profile.kind != "api":
        return {"profile": profile.id, "status": "skipped", "message": "subscription mode has no API key test"}
    assert profile.base_url
    model = parse_config_status().get("model") or "gpt-5.5"
    if not include_image:
        return post_api_json(
            profile,
            "/responses",
            {"model": model, "input": "Reply with OK only."},
            timeout,
        )

    image_url = make_test_png_data_url()
    return post_api_json(
        profile,
        "/responses",
        {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "This is a tiny solid color PNG. What color is it? Reply one word.",
                        },
                        {"type": "input_image", "image_url": image_url},
                    ],
                }
            ],
        },
        timeout,
    )


def fetch_api_models(profile: Profile, timeout: int = 30) -> dict[str, Any]:
    if profile.kind != "api":
        return {"profile": profile.id, "status": "skipped", "models": []}
    assert profile.base_url
    api_key = get_api_key(profile)
    if not api_key:
        return {
            "profile": profile.id,
            "status": "error",
            "elapsed": 0,
            "models": [],
            "error": f"Missing User environment variable: {profile.env_key}",
        }
    url = profile.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(256 * 1024).decode("utf-8", errors="replace")
            data = json.loads(body)
            models: list[str] = []
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                for item in data["data"]:
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        models.append(item["id"])
            elif isinstance(data, dict) and isinstance(data.get("models"), list):
                for item in data["models"]:
                    if isinstance(item, str):
                        models.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("id"), str):
                        models.append(item["id"])
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        models.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("id"), str):
                        models.append(item["id"])
            return {
                "profile": profile.id,
                "status": "ok",
                "http_status": response.status,
                "elapsed": round(time.monotonic() - started, 2),
                "models": sorted(set(models)),
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        return {
            "profile": profile.id,
            "status": "http_error",
            "http_status": exc.code,
            "elapsed": round(time.monotonic() - started, 2),
            "models": [],
            "body": body,
        }
    except Exception as exc:
        return {
            "profile": profile.id,
            "status": "error",
            "elapsed": round(time.monotonic() - started, 2),
            "models": [],
            "error": repr(exc),
        }


def test_apis_menu() -> None:
    while True:
        clear_screen()
        print("API Test")
        print()
        api_profiles = [profile for profile in PROFILES if profile.kind == "api"]
        for index, profile in enumerate(api_profiles, 1):
            print(f"  {index}. Test text: {profile.label}")
        print("  I. Test current gateway with image")
        print("  A. Test text on all API gateways")
        print("  Q. Back")
        print()
        choice = input("Select: ").strip().lower()
        if choice == "q":
            return
        if choice == "i":
            status = parse_config_status()
            profile = active_profile(status)
            if not profile or profile.kind != "api":
                print("Current profile is not an API gateway.")
                print()
                pause()
                continue
            selected = [profile]
            include_image = True
        elif choice == "a":
            selected = api_profiles
            include_image = False
        else:
            try:
                selected = [api_profiles[int(choice) - 1]]
                include_image = False
            except Exception:
                print("Invalid choice.")
                time.sleep(1)
                continue

        print()
        for profile in selected:
            test_label = "image" if include_image else "text"
            label = f"Testing {profile.id} ({test_label})"
            ok, result = run_with_spinner(
                label,
                lambda p=profile, image=include_image: test_api_profile(p, include_image=image),
                timeout=65 if include_image else 35,
            )
            if not ok:
                print(f"{profile.id}: {result}")
                continue
            print_api_result(result)
        print()
        pause()


def print_api_result(result: dict[str, Any]) -> None:
    profile = result.get("profile")
    status = result.get("status")
    print(f"{profile}: {status}")
    if "http_status" in result:
        print(f"  HTTP: {result['http_status']}")
    if "elapsed" in result:
        print(f"  elapsed: {result['elapsed']}s")
    if "error" in result:
        print(f"  error: {result['error']}")
    body = result.get("body")
    if body:
        body_text = str(body)
        if len(body_text) > 1600:
            body_text = body_text[:1600] + "... <truncated>"
        print("  body:")
        for line in body_text.splitlines()[:30]:
            print(f"    {line}")


def print_models_result(result: dict[str, Any]) -> None:
    profile = result.get("profile")
    status = result.get("status")
    models = result.get("models") or []
    print(f"{profile}: {status}")
    if "http_status" in result:
        print(f"  HTTP: {result['http_status']}")
    if "elapsed" in result:
        print(f"  elapsed: {result['elapsed']}s")
    if "error" in result:
        print(f"  error: {result['error']}")
    if "body" in result and result["body"]:
        print(f"  body: {str(result['body'])[:1000]}")
    if models:
        for model in models:
            marker = " (image, skipped)" if model in MODEL_EXCLUDE_FROM_AGENT_MENU else ""
            print(f"  - {model}{marker}")


def refresh_models_menu() -> None:
    clear_screen()
    print(color("Available API Models", Style.CYAN, Style.BOLD))
    print()
    api_profiles = [profile for profile in PROFILES if profile.kind == "api"]
    for profile in api_profiles:
        ok, result = run_with_spinner(
            f"Fetching {profile.id} /models",
            lambda p=profile: fetch_api_models(p),
            timeout=35,
        )
        if not ok:
            print(f"{profile.id}: {result}")
            continue
        print_models_result(result)
        known_models = [m for m in GATEWAY_MODELS.get(profile.id, []) if m not in MODEL_EXCLUDE_FROM_AGENT_MENU]
        live_models = [m for m in result.get("models", []) if m not in MODEL_EXCLUDE_FROM_AGENT_MENU]
        missing = sorted(set(live_models) - set(known_models))
        removed = sorted(set(known_models) - set(live_models))
        if missing:
            print("  New models not in this TUI build:")
            for model in missing:
                print(f"    + {model}")
        if removed:
            print("  TUI models not returned by /models now:")
            for model in removed:
                print(f"    - {model}")
        print()
    pause()


def change_model_menu() -> None:
    while True:
        clear_screen()
        status = parse_config_status()
        current_model = status.get("model") or ""
        gateway_id, models = models_for_active_profile(status)

        print(color("Change Model", Style.CYAN, Style.BOLD))
        print()
        print(f"Current gateway: {gateway_id or 'unknown'}")
        print(f"Current model:   {current_model}")
        print()

        if not models:
            print("No model list is configured for the current provider.")
            print("Switch to codexcn or modelhub first.")
            pause()
            return

        for index, model in enumerate(models, 1):
            active = model == current_model
            print(format_menu_item(str(index), f"{display_model_name(model)} ({model})", active))
        print(format_menu_item("L", "List live /models from gateways"))
        print(format_menu_item("Q", "Back"))
        print()
        choice = input("Select: ").strip().lower()
        if choice == "q":
            return
        if choice == "l":
            refresh_models_menu()
            continue
        try:
            selected = models[int(choice) - 1]
        except Exception:
            print("Invalid choice.")
            time.sleep(1)
            continue

        clear_screen()
        print(color("Apply model", Style.CYAN, Style.BOLD))
        print(f"Gateway: {gateway_id}")
        print(f"Current model: {current_model}")
        print(f"Target model:  {selected}")
        print()
        print("This changes only model and model_catalog_json in config.toml.")
        print("Codex Desktop must be closed before applying.")
        print()
        try:
            apply_model(selected)
        except Exception as exc:
            print()
            print(f"ERROR: {exc}")
        print()
        pause()


def refresh_model_catalog_menu() -> None:
    clear_screen()
    status = parse_config_status()
    gateway_id, models = models_for_active_profile(status)
    context_window = context_window_for_gateway(gateway_id) if gateway_id else DEFAULT_MODEL_CONTEXT_WINDOW
    print(color("Refresh API model catalog", Style.CYAN, Style.BOLD))
    print()
    print(f"Current provider: {status.get('model_provider') or 'unknown'}")
    print(f"Current gateway:  {gateway_id or 'unknown'}")
    print(f"Current model:    {status.get('model') or 'unknown'}")
    print(f"Catalog models:   {len(models)}")
    print()
    print("This rewrites only the generated API model catalog metadata.")
    print(f"Context truncation limit will be {context_window}.")
    print(f"Input modalities will be: {', '.join(MODEL_INPUT_MODALITIES)}.")
    print("Codex Desktop must be closed before applying.")
    print()
    try:
        refresh_model_catalog()
    except Exception as exc:
        print()
        print(f"ERROR: {exc}")
    print()
    pause()


def apply_profile_menu(profile: Profile) -> None:
    clear_screen()
    print(color(f"Apply profile: {profile.label}", Style.CYAN, Style.BOLD))
    print(f"Target provider: {profile.provider}")
    if profile.kind == "api":
        print(f"Gateway: {profile.base_url}")
        print(f"Env key: {profile.env_key}")
        print("Auth mode: requires_openai_auth=false")
    print()
    print("This will make all local chats visible under the selected provider.")
    print("Codex Desktop must be closed before applying.")
    print()
    try:
        apply_profile(profile)
    except Exception as exc:
        print()
        print(f"ERROR: {exc}")
    print()
    pause()


def sync_current_menu() -> None:
    clear_screen()
    status = parse_config_status()
    provider = status.get("model_provider") or ""
    print(color(f"Sync all chats to current provider: {provider!r}", Style.CYAN, Style.BOLD))
    print()
    print("Codex Desktop must be closed before applying.")
    print()
    if not confirm("Apply now?", default=False):
        print(color("Cancelled.", Style.YELLOW))
        pause()
        return
    try:
        sync_to_current_provider()
    except Exception as exc:
        print()
        print(f"ERROR: {exc}")
    print()
    pause()


def restore_hidden_chats_menu() -> None:
    clear_screen()
    status = parse_config_status()
    provider = status.get("model_provider") or ""
    if not provider:
        print(color("Cannot detect current model_provider in config.toml", Style.RED, Style.BOLD))
        pause()
        return

    active_rows = fetch_hidden_user_rows(provider, include_archived=False)
    all_rows = fetch_hidden_user_rows(provider, include_archived=True)
    archived_count = len(all_rows) - len(active_rows)

    print(color("Restore hidden user chats", Style.CYAN, Style.BOLD))
    print()
    print(f"Current provider: {provider}")
    print(f"Active hidden user chats: {len(active_rows)}")
    print(f"Archived hidden user chats: {archived_count}")
    print()

    rows_to_preview = active_rows[:20]
    if rows_to_preview:
        print(color("Preview:", Style.BOLD))
        for row in rows_to_preview:
            title = shorten_title(row["title"] or "", limit=90)
            provider_from = row["model_provider"]
            updated = timestamp_iso(row)
            print(f"  {updated}  {provider_from!r} -> {provider!r}  {title}")
        if len(active_rows) > len(rows_to_preview):
            print(f"  ... and {len(active_rows) - len(rows_to_preview)} more")
        print()

    if not active_rows and not archived_count:
        print("Nothing to restore.")
        pause()
        return

    include_archived = False
    if archived_count:
        include_archived = confirm("Include archived user chats too?", default=False)

    selected_count = len(all_rows) if include_archived else len(active_rows)
    print()
    print("This will only update user chats whose provider differs from the current provider.")
    print("Codex Desktop must be closed before applying.")
    print(f"Chats to update: {selected_count}")
    print()
    if not confirm("Restore now?", default=False):
        print(color("Cancelled.", Style.YELLOW))
        pause()
        return

    try:
        restore_hidden_user_chats(include_archived=include_archived)
    except Exception as exc:
        print()
        print(f"ERROR: {exc}")
    print()
    pause()


def tools_menu() -> None:
    while True:
        clear_screen()
        print(color("Tools", Style.CYAN, Style.BOLD))
        print()
        print(format_menu_item("T", "Test API gateways"))
        print(format_menu_item("C", "Refresh API model catalog"))
        print(format_menu_item("R", "Restore hidden user chats"))
        print(format_menu_item("S", "Sync all chats to current active provider"))
        print(format_menu_item("Q", "Back"))
        print()
        choice = input("Select: ").strip().lower()
        if choice == "q":
            return
        if choice == "t":
            test_apis_menu()
            continue
        if choice == "c":
            refresh_model_catalog_menu()
            continue
        if choice == "r":
            restore_hidden_chats_menu()
            continue
        if choice == "s":
            sync_current_menu()
            continue
        print("Invalid choice.")
        time.sleep(1)


def diagnostics_menu() -> None:
    show_status(include_thread_providers=True)
    pause()


def main_menu() -> None:
    while True:
        show_status()
        status = parse_config_status() if CONFIG_PATH.exists() else {}
        print(color("Choose mode:", Style.BOLD))
        for index, profile in enumerate(PROFILES, 1):
            print(format_menu_item(str(index), profile.label, is_profile_active(profile, status)))
        print(format_menu_item("M", "Change model"))
        print(format_menu_item("T", "Tools"))
        print(format_menu_item("D", "Diagnostics"))
        print(format_menu_item("Q", "Quit"))
        print()
        choice = input("Select: ").strip().lower()
        if choice == "q":
            return
        if choice == "t":
            tools_menu()
            continue
        if choice == "m":
            change_model_menu()
            continue
        if choice == "d":
            diagnostics_menu()
            continue
        try:
            profile = PROFILES[int(choice) - 1]
        except Exception:
            print("Invalid choice.")
            time.sleep(1)
            continue
        apply_profile_menu(profile)


def main() -> int:
    try:
        main_menu()
        return 0
    except KeyboardInterrupt:
        print()
        print("Cancelled.")
        return 130
    except Exception as exc:
        print()
        print(f"Fatal error: {exc}")
        pause()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
