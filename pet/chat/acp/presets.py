from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pet.chat.models import ProviderConfig

DEFAULT_ACP_TONE_PROMPT = (
    "你正在作为桌面宠物的编程助手与用户对话。请保持代码修改和技术结论 100% 严谨准确；"
    "最终回复请简洁、自然、友善，适合语音朗读，避免冗长客套或重复罗列工具日志。"
)

_DARWIN_FACTORY_BIN = "/Applications/Factory.app/Contents/Resources/bin/droid"
_DARWIN_DEVIN_BIN = (
    "/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin"
)
_DARWIN_DSH_BIN_JS = (
    "/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js"
)

ACP_PRESETS: dict[str, dict[str, Any]] = {
    "acp-factory": {
        "preset": "factory",
        "name": "Factory (Droid)",
        "default_command": "droid",
        "args": ["exec", "-o", "acp"],
        "startup_timeout": 20.0,
        "idle_timeout_minutes": 30,
    },
    "acp-devin": {
        "preset": "devin",
        "name": "Devin",
        "default_command": "devin",
        "args": ["acp"],
        "startup_timeout": 20.0,
        "idle_timeout_minutes": 30,
    },
    "acp-dsh": {
        "preset": "dsh",
        "name": "DeepSeek Harness (DSH)",
        "default_command": "dsh",
        "args": ["--profile", "acp"],
        "startup_timeout": 20.0,
        "idle_timeout_minutes": 30,
    },
    "acp-codex": {
        "preset": "codex",
        "name": "Codex",
        "default_command": "npx",
        "args": ["-y", "@agentclientprotocol/codex-acp"],
        "startup_timeout": 45.0,
        "idle_timeout_minutes": 30,
    },
}


def _semver_sort_key(path: Path) -> tuple[int, ...]:
    raw = path.name.lstrip("vV")
    parts: list[int] = []
    for seg in raw.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            break
    return tuple(parts)


def _nvm_node_bin_dirs() -> list[str]:
    nvm_root = Path.home() / ".nvm" / "versions" / "node"
    try:
        if not nvm_root.is_dir():
            return []
        versions = sorted(nvm_root.iterdir(), key=lambda p: (_semver_sort_key(p), p.name), reverse=True)
    except OSError:
        return []
    bins: list[str] = []
    for entry in versions:
        bin_dir = entry / "bin"
        try:
            if bin_dir.is_dir():
                bins.append(str(bin_dir))
        except OSError:
            continue
    return bins


def augmented_env(
    user_env: dict[str, str] | None = None,
    executable: str = "",
    platform_name: str | None = None,
) -> dict[str, str]:
    """Build a child-process environment with GUI-safe PATH augmentation.

    Preserves explicit ``user_env`` variables (keeping user-supplied PATH entries
    at the front) and appends standard user/runtime directories without
    duplicating entries.
    """
    plat = (platform_name or sys.platform).lower()
    is_win = plat.startswith("win")
    sep = ";" if is_win else ":"

    env: dict[str, str] = dict(os.environ)
    user_path: str | None = None
    if user_env:
        for k, v in user_env.items():
            ks = str(k)
            vs = str(v)
            if (is_win and ks.lower() == "path") or (not is_win and ks == "PATH"):
                user_path = vs
            else:
                env[ks] = vs

    raw_parts: list[str] = []
    if user_path:
        raw_parts.extend(user_path.split(sep))

    existing_path = env.get("PATH", "")
    if existing_path:
        raw_parts.extend(existing_path.split(os.pathsep))

    if executable:
        exec_dir = os.path.dirname(str(executable))
        if exec_dir:
            raw_parts.append(exec_dir)

    home = Path.home()
    if plat == "darwin":
        raw_parts.extend(
            [
                "/opt/homebrew/bin",
                "/usr/local/bin",
                str(home / ".local" / "bin"),
                str(home / ".cargo" / "bin"),
                *_nvm_node_bin_dirs(),
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
            ]
        )
    elif is_win:
        raw_parts.extend(
            [
                str(home / ".local" / "bin"),
                str(home / ".cargo" / "bin"),
            ]
        )
    else:
        raw_parts.extend(
            [
                str(home / ".local" / "bin"),
                str(home / ".cargo" / "bin"),
                "/usr/local/bin",
                *_nvm_node_bin_dirs(),
            ]
        )

    seen: set[str] = set()
    deduped: list[str] = []
    for item in raw_parts:
        entry = item.strip()
        if not entry:
            continue
        key = entry.lower() if is_win else entry
        if key not in seen:
            seen.add(key)
            deduped.append(entry)

    env["PATH"] = sep.join(deduped)
    return env


def _normalize_preset_id(preset_id: str) -> str:
    pid = str(preset_id or "").strip().lower()
    if pid in ACP_PRESETS:
        return pid
    prefixed = f"acp-{pid}"
    if prefixed in ACP_PRESETS:
        return prefixed
    raise KeyError(f"Unknown ACP preset: {preset_id!r}")


def _darwin_dsh_bundled_node() -> str:
    return str(
        Path.home()
        / ".dsh"
        / "dsh-runtimes"
        / "dsh-plugin-desktop-primary-runtime"
        / "dependencies"
        / "node"
    )


def build_preset_provider(
    preset_id: str,
    platform_name: str | None = None,
) -> ProviderConfig:
    """Build a ProviderConfig for one of the 4 built-in ACP agent presets."""
    from pet.chat.models import AcpAgentConfig, ProviderConfig

    pid = _normalize_preset_id(preset_id)
    meta = ACP_PRESETS[pid]
    plat = (platform_name or sys.platform).lower()
    is_darwin = plat == "darwin"
    is_win = plat.startswith("win")

    search_path = augmented_env(platform_name=plat).get("PATH", "")

    command = str(meta["default_command"])
    args = list(meta["args"])
    env: dict[str, str] = {}

    if pid == "acp-factory":
        if is_darwin and os.path.exists(_DARWIN_FACTORY_BIN):
            command = _DARWIN_FACTORY_BIN
        else:
            command = shutil.which("droid", path=search_path) or "droid"
    elif pid == "acp-devin":
        if is_darwin and os.path.exists(_DARWIN_DEVIN_BIN):
            command = _DARWIN_DEVIN_BIN
        else:
            command = shutil.which("devin", path=search_path) or "devin"
    elif pid == "acp-dsh":
        bundled_node = _darwin_dsh_bundled_node()
        if is_darwin and os.path.exists(bundled_node) and os.path.exists(_DARWIN_DSH_BIN_JS):
            command = bundled_node
            args = [_DARWIN_DSH_BIN_JS, "--profile", "acp"]
        else:
            command = shutil.which("dsh", path=search_path) or "dsh"
            args = ["--profile", "acp"]
    elif pid == "acp-codex":
        if is_win:
            command = (
                shutil.which("npx.cmd", path=search_path)
                or shutil.which("npx", path=search_path)
                or "npx.cmd"
            )
        else:
            command = shutil.which("npx", path=search_path) or "npx"
            codex_local = str(Path.home() / ".local" / "bin" / "codex")
            if os.path.exists(codex_local):
                env["CODEX_PATH"] = codex_local

    acp_cfg = AcpAgentConfig(
        preset=str(meta["preset"]),
        command=command,
        args=args,
        env=env,
        startup_timeout=float(meta["startup_timeout"]),
        idle_timeout_minutes=int(meta["idle_timeout_minutes"]),
    )
    return ProviderConfig(
        provider_id=pid,
        name=str(meta["name"]),
        kind="acp",
        acp=acp_cfg,
    )


def detect_builtin_acp_providers(
    platform_name: str | None = None,
) -> dict[str, ProviderConfig]:
    """Detect and build all 4 built-in ACP provider presets for the given platform."""
    return {
        pid: build_preset_provider(pid, platform_name=platform_name)
        for pid in ACP_PRESETS
    }
