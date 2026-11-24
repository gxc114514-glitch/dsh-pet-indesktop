from __future__ import annotations

import os
from pathlib import Path

from pet.chat.acp.presets import (
    ACP_PRESETS,
    DEFAULT_ACP_TONE_PROMPT,
    augmented_env,
    build_preset_provider,
    detect_builtin_acp_providers,
)
from pet.chat.models import AcpAgentConfig, ChatSession, ChatSettings, ProviderConfig


def test_legacy_openai_provider_backward_compatibility():
    cfg = ProviderConfig.from_dict("openai-main", {"name": "DeepSeek"})
    assert cfg.kind == "openai"
    assert cfg.acp is None
    assert cfg.is_acp is False

    data = cfg.to_dict()
    assert "acp" not in data
    assert "kind" not in data
    assert data["name"] == "DeepSeek"
    assert data["base_url"] == "https://api.deepseek.com"
    assert data["chat_path"] == "/v1/chat/completions"
    assert data["model"] == "deepseek-v4-flash"


def test_acp_provider_round_trip():
    acp = AcpAgentConfig(
        preset="factory",
        command="/Applications/Factory.app/Contents/Resources/bin/droid",
        args=["exec", "-o", "acp"],
        env={"FOO": "bar"},
        default_cwd="/tmp/project",
        default_mode_id="ask",
        default_model_id="claude-sonnet",
        tone_enabled=False,
        tone_prompt="Keep it brief.",
        startup_timeout=35.0,
        idle_timeout_minutes=45,
    )
    cfg = ProviderConfig(
        provider_id="acp-factory",
        name="Factory (Droid)",
        kind="acp",
        acp=acp,
    )
    assert cfg.is_acp is True

    raw = cfg.to_dict()
    assert raw["kind"] == "acp"
    assert isinstance(raw["acp"], dict)

    loaded = ProviderConfig.from_dict("acp-factory", raw)
    assert loaded.kind == "acp"
    assert loaded.is_acp is True
    assert loaded.acp is not None
    assert loaded.acp.preset == "factory"
    assert loaded.acp.command == "/Applications/Factory.app/Contents/Resources/bin/droid"
    assert loaded.acp.args == ["exec", "-o", "acp"]
    assert loaded.acp.env == {"FOO": "bar"}
    assert loaded.acp.default_cwd == "/tmp/project"
    assert loaded.acp.default_mode_id == "ask"
    assert loaded.acp.default_model_id == "claude-sonnet"
    assert loaded.acp.tone_enabled is False
    assert loaded.acp.tone_prompt == "Keep it brief."
    assert loaded.acp.startup_timeout == 35.0
    assert loaded.acp.idle_timeout_minutes == 45


def test_acp_agent_config_defaults():
    acp = AcpAgentConfig()
    assert acp.preset == "custom"
    assert acp.command == ""
    assert acp.args == []
    assert acp.env == {}
    assert acp.tone_enabled is True
    assert acp.tone_prompt == DEFAULT_ACP_TONE_PROMPT
    assert acp.tone_prompt.strip() != ""
    assert acp.startup_timeout == 20.0
    assert acp.idle_timeout_minutes == 30


def test_chat_session_acp_fields_omitted_when_empty_and_round_trip():
    empty_session = ChatSession.create("char-1", "openai-main", "sys")
    empty_dict = empty_session.to_dict()
    assert "workspace_dir" not in empty_dict
    assert "acp_session_id" not in empty_dict
    assert "acp_mode_id" not in empty_dict
    assert "acp_model_id" not in empty_dict

    loaded_empty = ChatSession.from_dict(empty_dict)
    assert loaded_empty.workspace_dir == ""
    assert loaded_empty.acp_session_id == ""
    assert loaded_empty.acp_mode_id == ""
    assert loaded_empty.acp_model_id == ""

    acp_session = ChatSession.create("char-1", "acp-factory", "")
    acp_session.workspace_dir = "/Users/demo/repo"
    acp_session.acp_session_id = "sess-123"
    acp_session.acp_mode_id = "plan"
    acp_session.acp_model_id = "gpt-5"

    acp_dict = acp_session.to_dict()
    assert acp_dict["workspace_dir"] == "/Users/demo/repo"
    assert acp_dict["acp_session_id"] == "sess-123"
    assert acp_dict["acp_mode_id"] == "plan"
    assert acp_dict["acp_model_id"] == "gpt-5"

    loaded_acp = ChatSession.from_dict(acp_dict)
    assert loaded_acp.workspace_dir == "/Users/demo/repo"
    assert loaded_acp.acp_session_id == "sess-123"
    assert loaded_acp.acp_mode_id == "plan"
    assert loaded_acp.acp_model_id == "gpt-5"


def test_chat_settings_record_workspace_and_ensure_acp_presets():
    settings = ChatSettings.defaults()
    assert settings.recent_workspaces == []
    assert "recent_workspaces" not in settings.to_dict()

    for i in range(12):
        settings.record_workspace(f"/tmp/ws-{i}")
    assert len(settings.recent_workspaces) == 10
    assert settings.recent_workspaces[0] == "/tmp/ws-11"
    assert settings.recent_workspaces[-1] == "/tmp/ws-2"

    # Deduplicate and move existing workspace to front
    settings.record_workspace("/tmp/ws-5")
    assert len(settings.recent_workspaces) == 10
    assert settings.recent_workspaces[0] == "/tmp/ws-5"
    assert settings.recent_workspaces.count("/tmp/ws-5") == 1

    # Round-trip recent_workspaces
    d = settings.to_dict()
    assert d["recent_workspaces"] == settings.recent_workspaces
    reloaded = ChatSettings.from_dict(d)
    assert reloaded.recent_workspaces == settings.recent_workspaces

    # Seed ACP presets only when no ACP providers exist
    seeded = settings.ensure_acp_presets()
    assert seeded is True
    for pid in ("acp-factory", "acp-devin", "acp-dsh", "acp-codex"):
        assert pid in settings.providers
        assert settings.providers[pid].is_acp is True

    # Mutate one ACP provider and ensure ensure_acp_presets() does not overwrite
    settings.providers["acp-factory"].name = "My Custom Factory"
    seeded_again = settings.ensure_acp_presets()
    assert seeded_again is False
    assert settings.providers["acp-factory"].name == "My Custom Factory"


def test_detect_builtin_acp_providers_darwin_bundled_and_fallback(monkeypatch):
    # Case 1: DSH bundled Node 24 + bin.js exist
    bundled_node = str(
        Path.home()
        / ".dsh"
        / "dsh-runtimes"
        / "dsh-plugin-desktop-primary-runtime"
        / "dependencies"
        / "node"
    )
    dsh_bin = "/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js"
    factory_bin = "/Applications/Factory.app/Contents/Resources/bin/droid"
    devin_bin = "/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin"
    codex_bin = str(Path.home() / ".local" / "bin" / "codex")

    existing = {bundled_node, dsh_bin, factory_bin, devin_bin, codex_bin}
    monkeypatch.setattr("os.path.exists", lambda p: str(p) in existing)
    monkeypatch.setattr("shutil.which", lambda name, path=None: "/opt/homebrew/bin/npx" if name == "npx" else None)

    providers = detect_builtin_acp_providers("darwin")
    assert set(providers.keys()) == {"acp-factory", "acp-devin", "acp-dsh", "acp-codex"}
    assert set(ACP_PRESETS.keys()) == {"acp-factory", "acp-devin", "acp-dsh", "acp-codex"}

    for pid, cfg in providers.items():
        assert cfg.is_acp is True
        assert cfg.acp is not None
        assert "--yolo" not in cfg.acp.args
        assert "--skip-permissions-unsafe" not in cfg.acp.args

    assert providers["acp-factory"].acp.command == factory_bin
    assert providers["acp-factory"].acp.args == ["exec", "-o", "acp"]

    assert providers["acp-devin"].acp.command == devin_bin
    assert providers["acp-devin"].acp.args == ["acp"]

    assert providers["acp-dsh"].acp.command == bundled_node
    assert providers["acp-dsh"].acp.args == [dsh_bin, "--profile", "acp"]

    assert providers["acp-codex"].acp.args == ["-y", "@agentclientprotocol/codex-acp"]
    assert providers["acp-codex"].acp.env.get("CODEX_PATH") == codex_bin
    assert providers["acp-codex"].acp.startup_timeout == 45.0

    # Case 2: DSH bundled Node 24 missing -> falls back to dsh --profile acp
    monkeypatch.setattr("os.path.exists", lambda p: False)
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    fallback_dsh = build_preset_provider("acp-dsh", platform_name="darwin")
    assert fallback_dsh.acp is not None
    assert fallback_dsh.acp.command == "dsh"
    assert fallback_dsh.acp.args == ["--profile", "acp"]


def test_detect_builtin_acp_providers_win32(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name, path=None: None)
    providers = detect_builtin_acp_providers("win32")
    assert set(providers.keys()) == {"acp-factory", "acp-devin", "acp-dsh", "acp-codex"}

    assert providers["acp-factory"].acp.command == "droid"
    assert providers["acp-factory"].acp.args == ["exec", "-o", "acp"]
    assert providers["acp-devin"].acp.command == "devin"
    assert providers["acp-devin"].acp.args == ["acp"]
    assert providers["acp-dsh"].acp.command == "dsh"
    assert providers["acp-dsh"].acp.args == ["--profile", "acp"]
    assert providers["acp-codex"].acp.command in ("npx", "npx.cmd")
    assert providers["acp-codex"].acp.args == ["-y", "@agentclientprotocol/codex-acp"]
    assert providers["acp-codex"].acp.startup_timeout == 45.0


def test_augmented_env_darwin_preserves_user_and_deduplicates(monkeypatch):
    fake_home = Path("/Users/testuser")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = augmented_env(
        {"CUSTOM": "1", "PATH": "/my/bin"},
        executable="/opt/homebrew/bin/foo",
        platform_name="darwin",
    )
    assert env["CUSTOM"] == "1"
    parts = env["PATH"].split(":")
    assert parts[0] == "/my/bin"
    assert "/opt/homebrew/bin" in parts
    assert "/usr/local/bin" in parts
    assert "/Users/testuser/.local/bin" in parts
    # No duplicate PATH entries
    assert len(parts) == len(set(parts))
