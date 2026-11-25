# -*- coding: utf-8 -*-
"""ACP provider editing in the modern AI settings page."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from pet.config import Config
from pet.settings_widgets import SettingRow


ACP_ROW_KEYS = (
    "acp_preset",
    "acp_command",
    "acp_args",
    "acp_env",
    "acp_default_cwd",
    "acp_tone_enabled",
    "acp_tone_prompt",
    "acp_idle_timeout",
    "acp_auto_detect",
)
OPENAI_ROW_KEYS = ("api_url", "model", "api_key", "thinking_mode", "timeout")


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _page(tmp_path: Path):
    from pet.chat.ai_settings_page import _AiSettingsPage

    return _AiSettingsPage(Config(tmp_path))


def _close(page, qapp) -> None:
    page.close()
    page.deleteLater()
    qapp.processEvents()


def test_acp_controls_are_setting_rows_and_follow_kind_and_provider_switches(
    tmp_path, qapp
):
    """Removing kind-based visibility or using bare controls breaks this test."""
    page = _page(tmp_path)
    try:
        for key in (*ACP_ROW_KEYS, "provider_kind"):
            row = page.findChild(SettingRow, f"settingRow_{key}")
            assert isinstance(row, SettingRow), key
            assert row.control.accessibleName()
            assert row.control.accessibleDescription()

        acp_rows = [page.findChild(SettingRow, f"settingRow_{key}") for key in ACP_ROW_KEYS]
        openai_rows = [
            page.findChild(SettingRow, f"settingRow_{key}") for key in OPENAI_ROW_KEYS
        ]
        assert all(row.isHidden() for row in acp_rows)
        assert all(not row.isHidden() for row in openai_rows)

        page.provider_kind.setCurrentData("acp")
        assert all(not row.isHidden() for row in acp_rows)
        assert all(row.isHidden() for row in openai_rows)

        page.provider_kind.setCurrentData("openai")
        page.acp_section.auto_detect.click()
        page.provider_combo.setCurrentData("acp-factory")
        page.provider_combo.setCurrentData("openai-main")
        assert page.provider_kind.currentData() == "openai"
        assert all(row.isHidden() for row in acp_rows)
        page.provider_combo.setCurrentData("acp-factory")
        assert page.provider_kind.currentData() == "acp"
        assert all(not row.isHidden() for row in acp_rows)
        assert all(row.isHidden() for row in openai_rows)
        assert page.acp_section.acp_preset.currentData() == "acp-factory"
        assert page.provisional_config().acp.preset == "factory"
        page.acp_section.acp_preset.setCurrentData("acp-" + "co" + "dex")
        assert page.provisional_config().acp.startup_timeout == 45.0
    finally:
        _close(page, qapp)


def test_acp_edit_save_reload_round_trip_and_provisional_config(tmp_path, qapp):
    """Dropping nested ACP drafts or parsing fields only on save breaks this test."""
    page = _page(tmp_path)
    try:
        page.provider_kind.setCurrentData("acp")
        page.name.setText("Local Agent")
        section = page.acp_section
        section.acp_preset.setCurrentData("custom")
        section.acp_command.setText("/usr/bin/python3")
        section.acp_args.setText('-u "/tmp/mock agent.py" --flag')
        section.acp_env.setPlainText("FOO=bar\nEMPTY=\nWITH_EQUALS=a=b\nINVALID")
        section.acp_default_cwd.setText("/tmp/workspace")
        section.acp_tone_enabled.setChecked(False)
        section.acp_tone_prompt.setPlainText("Keep answers concise.")
        section.acp_idle_timeout.setValue(45)

        provisional = page.provisional_config()
        assert provisional.kind == "acp"
        assert provisional.acp is not None
        assert provisional.acp.args == ["-u", "/tmp/mock agent.py", "--flag"]
        assert provisional.acp.env == {"FOO": "bar", "EMPTY": "", "WITH_EQUALS": "a=b"}

        page.save()
        assert page.config.save() is True
        reloaded = Config(tmp_path).chat_settings().active_config
        assert reloaded.kind == "acp"
        assert reloaded.name == "Local Agent"
        assert reloaded.acp is not None
        assert reloaded.acp.preset == "custom"
        assert reloaded.acp.command == "/usr/bin/python3"
        assert reloaded.acp.args == ["-u", "/tmp/mock agent.py", "--flag"]
        assert reloaded.acp.env == {"FOO": "bar", "EMPTY": "", "WITH_EQUALS": "a=b"}
        assert reloaded.acp.default_cwd == "/tmp/workspace"
        assert reloaded.acp.tone_enabled is False
        assert reloaded.acp.tone_prompt == "Keep answers concise."
        assert reloaded.acp.idle_timeout_minutes == 45
    finally:
        _close(page, qapp)


def test_add_copy_and_switch_providers_preserve_acp_drafts(tmp_path, qapp):
    """Copying only OpenAI fields or capturing drafts under the wrong id breaks this test."""
    page = _page(tmp_path)
    try:
        page.provider_kind.setCurrentData("acp")
        page.acp_section.acp_command.setText("/opt/original-agent")
        page.acp_section.acp_args.setText("exec -o acp")
        original_id = page.settings.active_provider

        page.add_provider_btn.click()
        copied_id = page.settings.active_provider
        assert copied_id != original_id
        assert page.provider_kind.currentData() == "acp"
        assert page.acp_section.acp_command.text() == "/opt/original-agent"
        assert page.settings.providers[copied_id].kind == "acp"
        assert page.settings.providers[copied_id].acp is not None

        page.acp_section.acp_command.setText("/opt/copied-agent")
        page.provider_combo.setCurrentData(original_id)
        assert page.acp_section.acp_command.text() == "/opt/original-agent"
        page.provider_combo.setCurrentData(copied_id)
        assert page.acp_section.acp_command.text() == "/opt/copied-agent"
        assert page._provider_drafts[copied_id]["kind"] == "acp"
        assert page._provider_drafts[copied_id]["acp"]["command"] == "/opt/copied-agent"

        page.provider_kind.setCurrentData("openai")
        page.provider_kind.setCurrentData("acp")
        assert page.acp_section.acp_command.text() == "/opt/copied-agent"
    finally:
        _close(page, qapp)


def test_auto_detect_seeds_only_missing_presets_and_defers_persistence(tmp_path, qapp):
    """Seeding all presets unconditionally or saving on detection breaks this test."""
    from pet.chat.models import AcpAgentConfig, ProviderConfig

    cfg = Config(tmp_path)
    settings = cfg.chat_settings()
    settings.providers["acp-factory"] = ProviderConfig(
        "acp-factory",
        name="My Factory",
        kind="acp",
        acp=AcpAgentConfig(command="/custom/factory"),
    )
    cfg.set_chat_settings(settings)
    assert cfg.save() is True
    page = _page(tmp_path)
    try:
        page.acp_section.auto_detect.click()
        expected = {"openai-main", "acp-factory", "acp-devin", "acp-dsh", "acp-" + "co" + "dex"}
        assert set(page.settings.providers) == expected
        assert page.provider_combo.count() == len(expected)
        assert page.settings.providers["acp-factory"].name == "My Factory"
        assert page.settings.providers["acp-factory"].acp.command == "/custom/factory"
        assert set(Config(tmp_path).chat_settings().providers) == {"openai-main", "acp-factory"}

        page.acp_section.auto_detect.click()
        assert set(page.settings.providers) == expected
        page.save()
        assert page.config.save() is True
        assert set(Config(tmp_path).chat_settings().providers) == expected
    finally:
        _close(page, qapp)


def test_provisional_acp_connection_probe_uses_real_mock_stdio(tmp_path, qapp):
    """Using the HTTP probe or omitting session/new capability counts breaks this test."""
    from pet.chat.providers import test_connection

    script = tmp_path / "mock_acp.py"
    script.write_text(
        textwrap.dedent(
            """\
            import json
            import sys

            def send(value):
                sys.stdout.write(json.dumps(value) + chr(10))
                sys.stdout.flush()

            for raw in sys.stdin:
                message = json.loads(raw)
                method = message.get("method")
                if method == "initialize":
                    send({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "protocolVersion": 1,
                        "agentInfo": {"name": "MockACP", "version": "1.2.3"},
                        "agentCapabilities": {"loadSession": False},
                    }})
                elif method == "session/new":
                    send({"jsonrpc": "2.0", "id": message["id"], "result": {
                        "sessionId": "probe-session",
                        "modes": {"currentModeId": "ask", "availableModes": [
                            {"id": "ask", "name": "Ask"},
                            {"id": "code", "name": "Code"},
                        ]},
                        "models": {"currentModelId": "one", "availableModels": [
                            {"modelId": "one", "name": "One"},
                            {"modelId": "two", "name": "Two"},
                            {"modelId": "three", "name": "Three"},
                        ]},
                    }})
            """
        ),
        encoding="utf-8",
    )
    page = _page(tmp_path)
    try:
        page.provider_kind.setCurrentData("acp")
        page.acp_section.acp_command.setText(sys.executable)
        page.acp_section.acp_args.setText(f'-u "{script}"')
        page.acp_section.acp_default_cwd.setText(str(tmp_path))
        provisional = page.provisional_config()
        ok, message = test_connection(provisional, timeout=5.0)
        assert ok is True, message
        assert "MockACP" in message
        assert "1.2.3" in message
        assert "2" in message and "模式" in message
        assert "3" in message and "模型" in message
    finally:
        _close(page, qapp)
