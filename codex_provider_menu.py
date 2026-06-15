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
import getpass
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

try:
    import questionary
    from questionary import Choice as QuestionaryChoice
    from questionary import Style as QuestionaryStyle
    from prompt_toolkit.keys import Keys as PromptKeys
except Exception:
    questionary = None
    QuestionaryChoice = None
    QuestionaryStyle = None
    PromptKeys = None

try:
    from rich.console import Console as RichConsole
    from rich.text import Text as RichText
except Exception:
    RichConsole = None
    RichText = None


APP_TITLE = "Codex Provider Menu"
TECHNICAL_TITLE_PREFIX = (
    "The following is the Codex agent history whose request action you are assessing"
)
DEFAULT_MODEL_CONTEXT_WINDOW = 128000
MODEL_INPUT_MODALITIES = ["text", "image"]
MODEL_EXCLUDE_FROM_AGENT_MENU = {"gpt-image-2"}
API_CONFIG_VERSION = 1
API_TEST_USER_AGENT = "Codex Provider Menu/1.0"
DEFAULT_API_ID = "openai"
DEFAULT_API_NAME = "OpenAI API"
DEFAULT_API_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_ENV_KEY = "OPENAI_API_KEY"
DEFAULT_API_MODELS = ["gpt-4.1", "gpt-4.1-mini"]


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


USE_ANSI = bool(sys.stdout.isatty() and enable_windows_colors())
USE_COLOR = bool(USE_ANSI and not os.environ.get("NO_COLOR"))
USE_RICH = bool(RichConsole and RichText and sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
USE_QUESTIONARY = bool(questionary and QuestionaryChoice and QuestionaryStyle and PromptKeys)


QUESTIONARY_STYLE = (
    QuestionaryStyle(
        [
            ("qmark", "fg:#00ffff bold"),
            ("question", "bold"),
            ("answer", "fg:#00ff00 bold"),
            ("pointer", "fg:#00ffff bold"),
            ("highlighted", "fg:#00ffff bold"),
            ("instruction", "fg:#888888"),
            ("text", ""),
        ]
    )
    if QuestionaryStyle
    else None
)


def color(text: str, *styles: str) -> str:
    if not USE_COLOR or not styles:
        return text
    return "".join(styles) + text + Style.RESET


def print_heading(title: str) -> None:
    if USE_RICH and RichConsole and RichText:
        RichConsole(file=sys.stdout, highlight=False).print(RichText(title, style="bold cyan"))
        return
    print(color(title, Style.CYAN, Style.BOLD))


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    provider: str
    kind: str
    name: str | None = None
    base_url: str | None = None
    env_key: str | None = None
    api_key: str = ""
    key_mode: str = "inline"
    models: tuple[str, ...] = ()
    default_model: str = ""
    context_window: int = DEFAULT_MODEL_CONTEXT_WINDOW
    input_modalities: tuple[str, ...] = ("text", "image")


@dataclass(frozen=True)
class MenuItem:
    label: str
    value: Any
    active: bool = False
    shortcut: str = ""


SUBSCRIPTION_PROFILE = Profile(
    id="subscription",
    label="Subscription / OpenAI account",
    provider="openai",
    kind="subscription",
)


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
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = CODEX_HOME / "config.toml"
DB_PATH = CODEX_HOME / "sqlite" / "state_5.sqlite"
if not DB_PATH.exists():
    DB_PATH = CODEX_HOME / "state_5.sqlite"
SESSION_INDEX_PATH = CODEX_HOME / "session_index.jsonl"
MODEL_CATALOG_PATH = CODEX_HOME / "codex_provider_models.json"
API_CONFIG_DIR = SCRIPT_DIR / "apis"
API_SCHEMA_PATH = API_CONFIG_DIR / "schema.json"


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


def list_codex_processes_winapi() -> list[dict[str, str]] | None:
    if os.name != "nt":
        return []
    try:
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_ulong),
                ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = ctypes.c_int
        kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            return None

        processes: list[dict[str, str]] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return []
            while True:
                image = entry.szExeFile
                if image.lower() == "codex.exe":
                    processes.append({"image": image, "pid": str(entry.th32ProcessID)})
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)
        return processes
    except Exception:
        return None


def list_codex_processes() -> list[dict[str, str]]:
    winapi_processes = list_codex_processes_winapi()
    if winapi_processes is not None:
        return winapi_processes

    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Codex.exe", "/FO", "CSV", "/NH"],
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
    for row in csv.reader(io.StringIO(result.stdout)):
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


def api_config_template() -> dict[str, Any]:
    return {
        "version": API_CONFIG_VERSION,
        "gateways": [],
    }


def ensure_api_config_dir() -> None:
    API_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def is_ignored_api_config_file(path: Path) -> bool:
    name = path.name.lower()
    return name == "schema.json" or name.endswith(".example.json")


def api_gateway_path(gateway_id: str) -> Path:
    return API_CONFIG_DIR / f"{normalize_gateway_id(gateway_id)}.json"


def gateway_entries_from_file(path: Path) -> list[Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except Exception as exc:
        raise ProviderMenuError(f"Cannot read API config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderMenuError(f"API config must be a JSON object: {path}")
    if "gateways" in data:
        gateways = data.get("gateways")
        if not isinstance(gateways, list):
            raise ProviderMenuError(f"API config field 'gateways' must be a list: {path}")
        return list(gateways)
    return [data]


def read_api_config() -> dict[str, Any]:
    if not API_CONFIG_DIR.exists():
        return api_config_template()
    gateways: list[Any] = []
    for path in sorted(API_CONFIG_DIR.glob("*.json")):
        if is_ignored_api_config_file(path):
            continue
        gateways.extend(gateway_entries_from_file(path))
    return {
        "version": API_CONFIG_VERSION,
        "gateways": gateways,
    }


def write_api_gateway(entry: dict[str, Any]) -> Path:
    ensure_api_config_dir()
    path = api_gateway_path(str(entry.get("id") or "gateway"))
    atomic_write_text(path, json.dumps(entry, ensure_ascii=False, indent=2) + "\n")
    return path


def unique_strings(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return tuple(output)


def normalize_gateway_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", value.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    return normalized


def default_env_key(gateway_id: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "_", gateway_id).strip("_").upper()
    return f"{token or 'GATEWAY'}_API_KEY"


def gateway_profile_from_config(item: Any, index: int) -> Profile:
    if not isinstance(item, dict):
        raise ProviderMenuError(f"API gateway #{index} must be a JSON object")
    gateway_id = normalize_gateway_id(str(item.get("id") or item.get("name") or ""))
    name = str(item.get("name") or gateway_id).strip()
    base_url = str(item.get("base_url") or "").strip().rstrip("/")
    key_mode = str(item.get("key_mode") or "inline").strip().lower()
    if key_mode not in {"inline", "env"}:
        key_mode = "inline"
    env_key = str(item.get("env_key") or "").strip()
    if not env_key:
        env_key = default_env_key(gateway_id)
    api_key = str(item.get("api_key") or "").strip()
    if not gateway_id:
        raise ProviderMenuError(f"API gateway #{index} is missing id")
    if not base_url:
        raise ProviderMenuError(f"API gateway {gateway_id!r} is missing base_url")
    if key_mode == "env" and not env_key:
        raise ProviderMenuError(f"API gateway {gateway_id!r} is missing env_key")

    try:
        context_window = int(item.get("context_window") or DEFAULT_MODEL_CONTEXT_WINDOW)
    except (TypeError, ValueError):
        context_window = DEFAULT_MODEL_CONTEXT_WINDOW
    if context_window <= 0:
        context_window = DEFAULT_MODEL_CONTEXT_WINDOW

    models = tuple(model for model in unique_strings(item.get("models")) if model not in MODEL_EXCLUDE_FROM_AGENT_MENU)
    default_model = str(item.get("default_model") or "").strip()
    if default_model and default_model not in models:
        models = (default_model, *models)

    input_modalities = unique_strings(item.get("input_modalities")) or tuple(MODEL_INPUT_MODALITIES)
    label = str(item.get("label") or f"API gateway: {name}").strip()
    return Profile(
        id=gateway_id,
        label=label,
        provider="custom",
        kind="api",
        name=name,
        base_url=base_url,
        env_key=env_key,
        api_key=api_key,
        key_mode=key_mode,
        models=models,
        default_model=default_model,
        context_window=context_window,
        input_modalities=input_modalities,
    )


def api_profiles() -> list[Profile]:
    config = read_api_config()
    output: list[Profile] = []
    seen: set[str] = set()
    for index, item in enumerate(config.get("gateways", []), 1):
        profile = gateway_profile_from_config(item, index)
        if profile.id in seen:
            raise ProviderMenuError(f"Duplicate API gateway id in {API_CONFIG_DIR}: {profile.id}")
        output.append(profile)
        seen.add(profile.id)
    return output


def profiles() -> list[Profile]:
    return [SUBSCRIPTION_PROFILE, *api_profiles()]


def api_profile_by_id(gateway_id: str) -> Profile | None:
    for profile in api_profiles():
        if profile.id == gateway_id:
            return profile
    return None


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
    for profile in profiles():
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


def format_menu_item(label: str, selected: bool = False, active: bool = False) -> str:
    pointer = color(">", Style.CYAN, Style.BOLD) if selected else " "
    label_text = color(label, Style.BOLD) if selected or active else label
    marker = f" {color('[active]', Style.GREEN, Style.BOLD)}" if active else ""
    return f"  {pointer} {label_text}{marker}"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def menu_body_lines(items: list[MenuItem], selected: int, help_text: str) -> list[str]:
    lines = [
        format_menu_item(item.label, selected=index == selected, active=item.active)
        for index, item in enumerate(items)
    ]
    lines.append("")
    lines.append(color(help_text, Style.DIM))
    return lines


def can_repaint_menu_body(lines: list[str]) -> bool:
    if not USE_ANSI:
        return False
    columns = shutil.get_terminal_size((120, 30)).columns
    return all(len(strip_ansi(line)) < columns for line in lines)


def print_menu_body(lines: list[str]) -> None:
    for line in lines:
        print(line)


def repaint_menu_body(lines: list[str]) -> None:
    sys.stdout.write(f"\x1b[{len(lines)}F")
    for line in lines:
        sys.stdout.write("\x1b[2K" + line + "\n")
    sys.stdout.flush()


def read_menu_key(prompt: str = "Select") -> str:
    if not sys.stdin.isatty():
        try:
            return input(f"{prompt}: ").strip().lower()
        except EOFError:
            return "escape"

    if os.name == "nt":
        import msvcrt

        char = msvcrt.getwch()
        if char == "\x03":
            raise KeyboardInterrupt
        if char in {"\r", "\n"}:
            return "enter"
        if char == "\x1b":
            return "escape"
        if char in {"\x00", "\xe0"}:
            code = msvcrt.getwch()
            return {
                "H": "up",
                "P": "down",
                "K": "left",
                "M": "right",
                "G": "home",
                "O": "end",
                "I": "pageup",
                "Q": "pagedown",
            }.get(code, "")
        return char.lower()

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = sys.stdin.read(1)
        if char == "\x03":
            raise KeyboardInterrupt
        if char in {"\r", "\n"}:
            return "enter"
        if char == "\x1b":
            seq = sys.stdin.read(2)
            return {
                "[A": "up",
                "[B": "down",
                "[D": "left",
                "[C": "right",
                "[H": "home",
                "[F": "end",
                "[5": "pageup",
                "[6": "pagedown",
            }.get(seq, "escape")
        return char.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def first_active_index(items: list[MenuItem]) -> int:
    for index, item in enumerate(items):
        if item.active:
            return index
    return 0


_QUESTIONARY_UNAVAILABLE = object()


def can_use_questionary() -> bool:
    return bool(USE_QUESTIONARY and sys.stdin.isatty() and sys.stdout.isatty())


def select_menu_questionary(
    items: list[MenuItem],
    render_header,
    *,
    selected: int,
    cancel_value: Any,
    help_text: str,
) -> Any:
    if not can_use_questionary():
        return _QUESTIONARY_UNAVAILABLE

    render_header()
    choices = [
        QuestionaryChoice(
            title=item.label + ("  [active]" if item.active else ""),
            value=item.value,
        )
        for item in items
    ]

    try:
        question = questionary.select(
            "Select",
            choices=choices,
            default=items[selected].value,
            qmark="",
            pointer=">",
            style=QUESTIONARY_STYLE,
            use_shortcuts=False,
            use_arrow_keys=True,
            use_jk_keys=False,
            use_emacs_keys=True,
            show_selected=False,
            instruction=f"({help_text})",
        )
        if cancel_value is not None:
            bindings = question.application.key_bindings

            def cancel(event) -> None:
                event.app.exit(result=cancel_value)

            bindings.add("q", eager=True)(cancel)
            bindings.add("Q", eager=True)(cancel)
            bindings.add(PromptKeys.Escape, eager=True)(cancel)

        result = question.ask(kbi_msg="")
    except Exception:
        return _QUESTIONARY_UNAVAILABLE

    if result is None and cancel_value is not None:
        return cancel_value
    return result


def select_menu_builtin(
    items: list[MenuItem],
    render_header,
    *,
    selected: int,
    cancel_value: Any,
    help_text: str,
) -> Any:
    shortcuts = {item.shortcut.lower(): item.value for item in items if item.shortcut}
    lines = menu_body_lines(items, selected, help_text)
    fast_repaint = can_repaint_menu_body(lines)

    render_header()
    print_menu_body(lines)

    while True:
        key = read_menu_key()
        next_selected = selected
        if key in {"up", "left"}:
            next_selected = (selected - 1) % len(items)
        elif key in {"down", "right"}:
            next_selected = (selected + 1) % len(items)
        elif key == "home":
            next_selected = 0
        elif key == "end":
            next_selected = len(items) - 1
        elif key == "pageup":
            next_selected = max(0, selected - 10)
        elif key == "pagedown":
            next_selected = min(len(items) - 1, selected + 10)
        elif key == "enter":
            return items[selected].value
        elif key in shortcuts:
            return shortcuts[key]
        elif key in {"escape", "q"} and cancel_value is not None:
            return cancel_value
        elif not sys.stdin.isatty():
            print("Invalid choice.")
            time.sleep(1)
            render_header()
            print_menu_body(lines)

        if next_selected != selected:
            selected = next_selected
            lines = menu_body_lines(items, selected, help_text)
            if fast_repaint:
                repaint_menu_body(lines)
            else:
                render_header()
                print_menu_body(lines)


def select_menu(
    items: list[MenuItem],
    render_header,
    *,
    initial_index: int = 0,
    cancel_value: Any = None,
    help_text: str = "Use arrow keys and Enter. Esc/Q goes back.",
) -> Any:
    if not items:
        raise ProviderMenuError("Menu has no items.")

    selected = max(0, min(initial_index, len(items) - 1))
    choice = select_menu_questionary(
        items,
        render_header,
        selected=selected,
        cancel_value=cancel_value,
        help_text=help_text,
    )
    if choice is not _QUESTIONARY_UNAVAILABLE:
        return choice
    return select_menu_builtin(
        items,
        render_header,
        selected=selected,
        cancel_value=cancel_value,
        help_text=help_text,
    )


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


def models_for_active_profile(status: dict[str, Any]) -> tuple[Profile | None, list[str]]:
    profile = active_profile(status)
    if not profile or profile.kind != "api":
        return None, []
    return profile, list(profile.models)


def models_for_catalog_profile(profile: Profile) -> list[str]:
    return [model for model in profile.models if model not in MODEL_EXCLUDE_FROM_AGENT_MENU]


def model_catalog_entry(
    model: str,
    priority: int,
    profile: Profile,
) -> dict[str, Any]:
    context_window = profile.context_window or DEFAULT_MODEL_CONTEXT_WINDOW
    return {
        "slug": model,
        "display_name": display_model_name(model),
        "description": f"Model exposed by the configured {profile.name or profile.id} gateway.",
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
        "input_modalities": list(profile.input_modalities or tuple(MODEL_INPUT_MODALITIES)),
        "supports_search_tool": True,
        "use_responses_lite": False,
    }


def build_model_catalog(profile: Profile) -> dict[str, Any]:
    return {
        "models": [
            model_catalog_entry(
                model,
                priority=100 + index,
                profile=profile,
            )
            for index, model in enumerate(models_for_catalog_profile(profile))
        ]
    }


def write_model_catalog(profile: Profile) -> None:
    content = json.dumps(
        build_model_catalog(profile),
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
        current_model = parse_config_status().get("model") or ""
        available_models = list(profile.models)
        fallback_model = profile.default_model or (available_models[0] if available_models else "")
        if fallback_model and available_models and current_model not in available_models:
            lines = set_top_level_value(lines, "model", fallback_model)
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


def set_user_env_var(name: str, value: str) -> None:
    os.environ[name] = value
    if os.name != "nt":
        raise ProviderMenuError(
            f"Saved for this process only. Persist it in your shell profile: export {name}=<key>"
        )
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def get_api_key(profile: Profile) -> str:
    if profile.key_mode == "inline":
        return profile.api_key.strip()
    if not profile.env_key:
        return ""
    return get_user_env_var(profile.env_key).strip()


def prepare_api_key_for_codex(profile: Profile) -> None:
    if profile.key_mode == "inline" and profile.env_key and profile.api_key:
        os.environ[profile.env_key] = profile.api_key
        if os.name == "nt":
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                winreg.SetValueEx(key, profile.env_key, 0, winreg.REG_SZ, profile.api_key)


def key_source_label(profile: Profile) -> str:
    if profile.key_mode == "inline":
        return f"inline key (local JSON, Desktop bridge: {profile.env_key})"
    return f"User env var: {profile.env_key}"


def apply_profile(profile: Profile) -> None:
    assert_codex_not_running_for_writes()
    affected = fetch_thread_rows(provider_not=profile.provider)
    preflight_writable(affected)

    config_text = build_config_for_profile(profile)
    backup_dir = create_backup(affected, profile.provider)
    print(f"Backup: {backup_dir}")

    try:
        if profile.kind == "api":
            prepare_api_key_for_codex(profile)
            write_model_catalog(profile)
            if not get_api_key(profile):
                raise ProviderMenuError(
                    f"API key is missing for gateway: {profile.id}"
                )
            print(f"Using API key source: {key_source_label(profile)}")

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
    profile, available_models = models_for_active_profile(status)
    if not profile:
        raise ProviderMenuError("Current provider is not one of the configured TUI profiles.")
    if model not in available_models:
        raise ProviderMenuError(f"Model {model!r} is not listed for the current gateway: {profile.id}")

    assert_codex_not_running_for_writes()
    if not CONFIG_PATH.exists():
        raise ProviderMenuError(f"Missing config: {CONFIG_PATH}")

    backup_dir = backup_model_settings()
    print(f"Backup: {backup_dir}")

    try:
        prepare_api_key_for_codex(profile)
        write_model_catalog(profile)
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
    print(f"Current gateway: {profile.id}")
    print(f"Active model: {model}")
    print(f"Model catalog: {MODEL_CATALOG_PATH}")
    print("Restart Codex Desktop before using the new model.")


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
    print_heading(APP_TITLE)
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


def status_text(include_thread_providers: bool = False) -> str:
    original_stdout = sys.stdout
    buffer = io.StringIO()
    try:
        sys.stdout = buffer
        show_status(include_thread_providers=include_thread_providers)
    finally:
        sys.stdout = original_stdout
    return buffer.getvalue()


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
        source = "inline api_key" if profile.key_mode == "inline" else f"User environment variable: {profile.env_key}"
        return {
            "profile": profile.id,
            "status": "error",
            "elapsed": 0,
            "error": f"Missing API key ({source})",
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


def default_model_for_profile(profile: Profile) -> str:
    return profile.default_model or (profile.models[0] if profile.models else "")


def test_api_profile(
    profile: Profile,
    model: str | None = None,
    timeout: int = 30,
    include_image: bool = False,
) -> dict[str, Any]:
    if profile.kind != "api":
        return {"profile": profile.id, "status": "skipped", "message": "subscription mode has no API key test"}
    assert profile.base_url
    model = model or default_model_for_profile(profile)
    if not model:
        return {"profile": profile.id, "status": "error", "error": "No model selected or configured"}
    if not include_image:
        result = post_api_json(
            profile,
            "/responses",
            {"model": model, "input": "Reply with OK only."},
            timeout,
        )
        result["model"] = model
        return result

    image_url = make_test_png_data_url()
    result = post_api_json(
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
    result["model"] = model
    return result


def fetch_api_models(profile: Profile, timeout: int = 30) -> dict[str, Any]:
    if profile.kind != "api":
        return {"profile": profile.id, "status": "skipped", "models": []}
    assert profile.base_url
    api_key = get_api_key(profile)
    if not api_key:
        source = "inline api_key" if profile.key_mode == "inline" else f"User environment variable: {profile.env_key}"
        return {
            "profile": profile.id,
            "status": "error",
            "elapsed": 0,
            "models": [],
            "error": f"Missing API key ({source})",
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


def parse_models_text(text: str) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[\n,]+", text):
        model = item.strip()
        if not model or model in seen or model in MODEL_EXCLUDE_FROM_AGENT_MENU:
            continue
        models.append(model)
        seen.add(model)
    return models


def choose_model(models: list[str], prompt: str = "Model", default: str = "") -> str:
    visible = models[:50]
    if not sys.stdin.isatty():
        for index, model in enumerate(visible, 1):
            marker = " [default]" if model == default else ""
            print(f"  {index}. {model}{marker}")
        if len(models) > len(visible):
            print(f"  ... {len(models) - len(visible)} more; type an exact model slug to use one of them")
        if default:
            print(f"  Enter. {default}")
        answer = input(f"{prompt}: ").strip()
        if not answer:
            return default
        if answer.isdigit():
            index = int(answer) - 1
            if 0 <= index < len(visible):
                return visible[index]
        return answer

    items = [
        MenuItem(model, ("model", model), active=model == default, shortcut=str(index))
        for index, model in enumerate(visible, 1)
    ]
    if len(models) > len(visible):
        items.append(MenuItem("Type exact model slug...", ("manual", ""), shortcut="m"))
    if not items:
        return input(f"{prompt}: ").strip() or default

    def render() -> None:
        clear_screen()
        print_heading(prompt)
        if len(models) > len(visible):
            print(color(f"Showing first {len(visible)} of {len(models)} models.", Style.DIM))
        print()

    choice = select_menu(
        items,
        render,
        initial_index=first_active_index(items),
        cancel_value=("model", default),
        help_text="Use arrow keys and Enter. Esc keeps the current/default model.",
    )
    kind, value = choice
    if kind == "manual":
        return input("Model slug: ").strip() or default
    return value


def prompt_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return input(prompt).strip()


def api_gateway_entry(
    gateway_id: str,
    name: str,
    base_url: str,
    env_key: str,
    key_mode: str,
    api_key: str,
    models: list[str],
    default_model: str,
    context_window: int,
) -> dict[str, Any]:
    entry = {
        "id": gateway_id,
        "name": name,
        "label": f"API gateway: {name}",
        "base_url": base_url.rstrip("/"),
        "key_mode": key_mode,
        "models": models,
        "default_model": default_model,
        "context_window": context_window,
        "input_modalities": list(MODEL_INPUT_MODALITIES),
    }
    if key_mode == "inline":
        entry["api_key"] = api_key
        if env_key != default_env_key(gateway_id):
            entry["env_key"] = env_key
    else:
        entry["env_key"] = env_key
    return entry


def upsert_api_gateway(entry: dict[str, Any]) -> None:
    write_api_gateway(entry)


def add_api_gateway_menu() -> None:
    clear_screen()
    print_heading("Add API Gateway")
    print()
    print(f"Config folder: {API_CONFIG_DIR}")
    print("Default key mode stores the API key in the local ignored JSON file.")
    print()

    raw_id = input(f"Gateway id [{DEFAULT_API_ID}]: ").strip() or DEFAULT_API_ID
    gateway_id = normalize_gateway_id(raw_id)
    if not gateway_id:
        print("Gateway id is required.")
        pause()
        return
    name = input(f"Display name [{DEFAULT_API_NAME}]: ").strip() or DEFAULT_API_NAME
    base_url = (input(f"Base URL ending with /v1 [{DEFAULT_API_BASE_URL}]: ").strip() or DEFAULT_API_BASE_URL).rstrip("/")
    if not base_url:
        print("Base URL is required.")
        pause()
        return
    if sys.stdin.isatty():
        def render_key_mode() -> None:
            clear_screen()
            print_heading("API key storage")
            print(f"Gateway: {name}")
            print(f"Base URL: {base_url}")
            print()

        key_mode = select_menu(
            [
                MenuItem("Inline key in local JSON (default)", "inline", shortcut="i"),
                MenuItem("Environment variable", "env", shortcut="e"),
            ],
            render_key_mode,
            cancel_value="inline",
            help_text="Use arrow keys and Enter. Esc keeps inline JSON.",
        )
    else:
        key_mode_answer = input("Key mode: Enter = inline JSON, E = environment variable: ").strip().lower()
        key_mode = "env" if key_mode_answer == "e" else "inline"
    default_key = DEFAULT_API_ENV_KEY if gateway_id == DEFAULT_API_ID else default_env_key(gateway_id)
    if key_mode == "env":
        env_key = input(f"API key env var [{default_key}]: ").strip() or default_key
    else:
        env_key = default_key
        print(f"Desktop env bridge: {env_key}")

    try:
        context_text = input(f"Context window [{DEFAULT_MODEL_CONTEXT_WINDOW}]: ").strip()
        context_window = int(context_text) if context_text else DEFAULT_MODEL_CONTEXT_WINDOW
    except ValueError:
        context_window = DEFAULT_MODEL_CONTEXT_WINDOW

    key = prompt_secret("API key (optional, hidden): ")
    if key and key_mode == "env":
        try:
            set_user_env_var(env_key, key)
            print(f"Saved API key to User env var: {env_key}")
        except Exception as exc:
            print(f"Could not persist API key: {exc}")
    api_key = key if key_mode == "inline" else ""

    temp_profile = Profile(
        id=gateway_id,
        label=f"API gateway: {name}",
        provider="custom",
        kind="api",
        name=name,
        base_url=base_url,
        env_key=env_key,
        api_key=api_key,
        key_mode=key_mode,
    )

    models: list[str] = []
    if get_api_key(temp_profile):
        ok, result = run_with_spinner(
            f"Fetching {gateway_id} /models",
            lambda: fetch_api_models(temp_profile),
            timeout=35,
        )
        if ok and result.get("status") == "ok":
            models = [model for model in result.get("models", []) if model not in MODEL_EXCLUDE_FROM_AGENT_MENU]
            print(f"Fetched models: {len(models)}")
        else:
            print("Could not fetch /models.")
            if ok:
                print_api_result(result)
            else:
                print(result)

    if not models:
        default_models_text = ", ".join(DEFAULT_API_MODELS) if gateway_id == DEFAULT_API_ID else ""
        prompt = (
            f"Models (comma-separated) [{default_models_text}]: "
            if default_models_text
            else "Models (comma-separated, required for model picker): "
        )
        manual = input(prompt).strip() or default_models_text
        models = parse_models_text(manual)
    if not models:
        print("No models were configured. Gateway was not saved.")
        pause()
        return

    default_model = choose_model(models, prompt="Default model", default=models[0])
    if default_model not in models:
        models.insert(0, default_model)

    upsert_api_gateway(
        api_gateway_entry(
            gateway_id=gateway_id,
            name=name,
            base_url=base_url,
            env_key=env_key,
            key_mode=key_mode,
            api_key=api_key,
            models=models,
            default_model=default_model,
            context_window=context_window,
        )
    )
    saved_path = api_gateway_path(gateway_id)
    print()
    print(f"Saved API gateway: {gateway_id}")
    print(f"Config file: {saved_path}")
    print(f"Models stored: {len(models)}")
    print(f"Default model: {default_model}")
    pause()


def open_api_config_menu() -> None:
    ensure_api_config_dir()
    clear_screen()
    print_heading("API Gateway Config")
    print()
    print(f"Config folder: {API_CONFIG_DIR}")
    print(f"Schema:        {API_SCHEMA_PATH}")
    if os.name == "nt":
        subprocess.Popen(["explorer", str(API_CONFIG_DIR)])
        print("Opened the API config folder. Add or edit local *.json files there.")
    print()
    pause()


def models_for_test(profile: Profile) -> list[str]:
    ok, result = run_with_spinner(
        f"Fetching {profile.id} /models",
        lambda p=profile: fetch_api_models(p),
        timeout=35,
    )
    if ok and result.get("status") == "ok":
        models = [model for model in result.get("models", []) if model not in MODEL_EXCLUDE_FROM_AGENT_MENU]
        if models:
            return models
    if ok:
        print_models_result(result)
    else:
        print(f"{profile.id}: {result}")
    return list(profile.models)


def test_single_api_menu(profile: Profile) -> None:
    clear_screen()
    print_heading(f"Test API Gateway: {profile.label}")
    print()
    print(f"Base URL: {profile.base_url}")
    print(f"Key:      {key_source_label(profile)}")
    print()
    models = models_for_test(profile)
    if not models:
        model = input("Model slug: ").strip()
    else:
        model = choose_model(models, prompt="Test model", default=default_model_for_profile(profile) or models[0])
    if not model:
        print("No model selected.")
        pause()
        return
    if sys.stdin.isatty():
        def render_test_mode() -> None:
            clear_screen()
            print_heading(f"Test API Gateway: {profile.label}")
            print()
            print(f"Base URL: {profile.base_url}")
            print(f"Key:      {key_source_label(profile)}")
            print(f"Model:    {model}")
            print()

        mode = select_menu(
            [
                MenuItem("Text response test", "text", shortcut="t"),
                MenuItem("Image input test", "image", shortcut="i"),
                MenuItem("Back", "back", shortcut="q"),
            ],
            render_test_mode,
            cancel_value="back",
        )
        if mode == "back":
            return
        include_image = mode == "image"
    else:
        mode = input("Press Enter for text test, or type I for image test: ").strip().lower()
        include_image = mode == "i"

    print()
    label = f"Testing {profile.id} / {model}"
    ok, result = run_with_spinner(
        label,
        lambda p=profile, m=model, image=include_image: test_api_profile(p, model=m, include_image=image),
        timeout=65 if include_image else 35,
    )
    if not ok:
        print(f"{profile.id}: {result}")
    else:
        print_api_result(result)
    print()
    pause()


def test_apis_menu() -> None:
    while True:
        gateways = api_profiles()
        if not gateways:
            items = [MenuItem("Back", "back", shortcut="q")]
        else:
            items = []
            for index, profile in enumerate(gateways, 1):
                default_model = default_model_for_profile(profile)
                suffix = f" ({default_model})" if default_model else ""
                items.append(MenuItem(f"Test selected model: {profile.label}{suffix}", ("profile", profile), shortcut=str(index)))
            items.extend(
                [
                    MenuItem("Test default model on all gateways", ("all", None), shortcut="a"),
                    MenuItem("Back", ("back", None), shortcut="q"),
                ]
            )

        def render() -> None:
            clear_screen()
            print_heading("API Test")
            print()
            if not gateways:
                print(f"No API gateways configured. Add one in Tools > Add API gateway.")
                print(f"Config folder: {API_CONFIG_DIR}")
                print()

        choice = select_menu(items, render, cancel_value=("back", None) if gateways else "back")
        if not gateways:
            return
        kind, value = choice
        if kind == "back":
            return
        if kind == "all":
            print()
            for profile in gateways:
                model = default_model_for_profile(profile)
                if not model:
                    print(f"{profile.id}: skipped, no default model configured")
                    continue
                ok, result = run_with_spinner(
                    f"Testing {profile.id} / {model}",
                    lambda p=profile, m=model: test_api_profile(p, model=m),
                    timeout=35,
                )
                if not ok:
                    print(f"{profile.id}: {result}")
                    continue
                print_api_result(result)
            print()
            pause()
            continue
        test_single_api_menu(value)


def print_api_result(result: dict[str, Any]) -> None:
    profile = result.get("profile")
    status = result.get("status")
    print(f"{profile}: {status}")
    if "model" in result:
        print(f"  model: {result['model']}")
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
    print_heading("Available API Models")
    print()
    gateways = api_profiles()
    if not gateways:
        print(f"No API gateways configured: {API_CONFIG_DIR}")
        pause()
        return
    for profile in gateways:
        ok, result = run_with_spinner(
            f"Fetching {profile.id} /models",
            lambda p=profile: fetch_api_models(p),
            timeout=35,
        )
        if not ok:
            print(f"{profile.id}: {result}")
            continue
        print_models_result(result)
        known_models = [m for m in profile.models if m not in MODEL_EXCLUDE_FROM_AGENT_MENU]
        live_models = [m for m in result.get("models", []) if m not in MODEL_EXCLUDE_FROM_AGENT_MENU]
        missing = sorted(set(live_models) - set(known_models))
        removed = sorted(set(known_models) - set(live_models))
        if missing:
            print("  New models not in API config:")
            for model in missing:
                print(f"    + {model}")
        if removed:
            print("  Configured models not returned by /models now:")
            for model in removed:
                print(f"    - {model}")
        print()
    pause()


def change_model_menu() -> None:
    while True:
        status = parse_config_status()
        current_model = status.get("model") or ""
        profile, models = models_for_active_profile(status)

        def render() -> None:
            clear_screen()
            print_heading("Change Model")
            print()
            print(f"Current gateway: {profile.id if profile else 'unknown'}")
            print(f"Current model:   {current_model}")
            print()

        if not models:
            render()
            print("No model list is configured for the current provider.")
            print(f"Add/edit API models in: {API_CONFIG_DIR}")
            pause()
            return

        items = [
            MenuItem(
                f"{display_model_name(model)} ({model})",
                ("model", model),
                active=model == current_model,
                shortcut=str(index),
            )
            for index, model in enumerate(models, 1)
        ]
        items.append(MenuItem("List live /models from gateways", ("list", ""), shortcut="l"))
        items.append(MenuItem("Back", ("back", ""), shortcut="q"))

        choice = select_menu(
            items,
            render,
            initial_index=first_active_index(items),
            cancel_value=("back", ""),
        )
        kind, selected = choice
        if kind == "back":
            return
        if kind == "list":
            refresh_models_menu()
            continue

        clear_screen()
        print_heading("Apply model")
        print(f"Gateway: {profile.id if profile else 'unknown'}")
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


def apply_profile_menu(profile: Profile) -> None:
    clear_screen()
    print_heading(f"Apply profile: {profile.label}")
    print(f"Target provider: {profile.provider}")
    if profile.kind == "api":
        print(f"Gateway: {profile.base_url}")
        print(f"Key: {key_source_label(profile)}")
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
    print_heading(f"Sync all chats to current provider: {provider!r}")
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

    print_heading("Restore hidden user chats")
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
        items = [
            MenuItem("Add API gateway", "add", shortcut="a"),
            MenuItem("Open API gateways folder", "open", shortcut="e"),
            MenuItem("Test API gateways", "test", shortcut="t"),
            MenuItem("Restore hidden user chats", "restore", shortcut="r"),
            MenuItem("Sync all chats to current active provider", "sync", shortcut="s"),
            MenuItem("Back", "back", shortcut="q"),
        ]

        def render() -> None:
            clear_screen()
            print_heading("Tools")
            print()

        choice = select_menu(items, render, cancel_value="back")
        if choice == "back":
            return
        if choice == "add":
            add_api_gateway_menu()
            continue
        if choice == "open":
            open_api_config_menu()
            continue
        if choice == "test":
            test_apis_menu()
            continue
        if choice == "restore":
            restore_hidden_chats_menu()
            continue
        if choice == "sync":
            sync_current_menu()
            continue


def diagnostics_menu() -> None:
    show_status(include_thread_providers=True)
    pause()


def main_menu() -> None:
    while True:
        header = status_text()
        status = parse_config_status() if CONFIG_PATH.exists() else {}
        available_profiles = profiles()
        items = [
            MenuItem(profile.label, ("profile", profile), is_profile_active(profile, status), shortcut=str(index))
            for index, profile in enumerate(available_profiles, 1)
        ]
        items.extend(
            [
                MenuItem("Change model", ("model", None), shortcut="m"),
                MenuItem("Tools", ("tools", None), shortcut="t"),
                MenuItem("Diagnostics", ("diagnostics", None), shortcut="d"),
                MenuItem("Quit", ("quit", None), shortcut="q"),
            ]
        )

        def render() -> None:
            clear_screen()
            print(header, end="")
            print(color("Choose mode:", Style.BOLD))

        choice = select_menu(
            items,
            render,
            initial_index=first_active_index(items),
            cancel_value=("quit", None),
            help_text="Use arrow keys and Enter. Esc/Q quits.",
        )
        kind, value = choice
        if kind == "quit":
            return
        if kind == "tools":
            tools_menu()
            continue
        if kind == "model":
            change_model_menu()
            continue
        if kind == "diagnostics":
            diagnostics_menu()
            continue
        apply_profile_menu(value)


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
