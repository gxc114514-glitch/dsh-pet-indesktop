# -*- coding: utf-8 -*-
"""ACP provider editing in the modern AI settings page."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

import pet.modern_settings_dialog as settings_mod
from pet.config import Config
from pet.settings_widgets import SettingRow, SettingsSection


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


def _real_dialog(tmp_path: Path, monkeypatch, *, acp: bool = False):
    monkeypatch.setattr(settings_mod.autostart_mod, "is_enabled", lambda: False)
    config = Config(tmp_path)
    if acp:
        from pet.chat.models import AcpAgentConfig, ProviderConfig

        settings = config.chat_settings()
        settings.providers["acp-factory"] = ProviderConfig(
            "acp-factory",
            name="Local Factory",
            kind="acp",
            acp=AcpAgentConfig(command="/opt/factory", preset="factory"),
        )
        settings.active_provider = "acp-factory"
        config.set_chat_settings(settings)
        assert config.save() is True
    dialog = settings_mod.ModernSettingsDialog(config, include_ai=True)
    dialog.show()
    assert dialog.select_page("AI 与对话")
    QApplication.instance().processEvents()
    QApplication.instance().processEvents()
    return dialog


def _domain(dialog):
    domain = dialog.findChild(settings_mod.QWidget, "settingsDomain_ai")
    assert domain is not None
    return domain


def _mounted_row(dialog, key: str) -> SettingRow:
    row = dialog.findChild(SettingRow, f"settingRow_{key}")
    assert row is not None, key
    assert _domain(dialog).isAncestorOf(row), key
    return row


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


def test_real_dialog_mounts_openai_and_acp_controls_and_switches(tmp_path, monkeypatch, qapp):
    """Leaving new rows under the hidden controller breaks this real-dialog test."""
    dialog = _real_dialog(tmp_path, monkeypatch)
    try:
        page = dialog.ai_page
        kind_row = _mounted_row(dialog, "provider_kind")
        assert kind_row.isVisibleTo(_domain(dialog))
        assert all(_mounted_row(dialog, key).isHidden() for key in ACP_ROW_KEYS)
        assert not _mounted_row(dialog, "api_url").isHidden()

        page.provider_kind.setCurrentData("acp")
        qapp.processEvents()
        assert all(not _mounted_row(dialog, key).isHidden() for key in ACP_ROW_KEYS)
        assert _mounted_row(dialog, "api_url").isHidden()
        assert page.acp_section.section.parentWidget() is _domain(dialog)
        kind_section = kind_row.parentWidget()
        while kind_section is not None and not isinstance(kind_section, SettingsSection):
            kind_section = kind_section.parentWidget()
        assert kind_section is not None
        assert kind_section.parentWidget() is _domain(dialog)
        anchor_bar = dialog.pages.currentWidget().findChild(
            settings_mod.QWidget, "settingsPageAnchors"
        )
        chip_titles = [chip.text() for chip in anchor_bar._chips]
        assert "Provider 类型" in chip_titles
        assert "ACP Coding Agent" in chip_titles
        layout_count = _domain(dialog).layout().count()
        page._mount_integrated_sections()
        assert _domain(dialog).layout().count() == layout_count

        page.provider_kind.setCurrentData("openai")
        page.acp_section.auto_detect.click()
        page.provider_combo.setCurrentData("openai-main")
        assert page.provider_kind.currentData() == "openai"
        assert _mounted_row(dialog, "acp_command").isHidden()
        page.provider_combo.setCurrentData("acp-factory")
        assert page.provider_kind.currentData() == "acp"
        assert not _mounted_row(dialog, "acp_command").isHidden()
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_real_dialog_initial_acp_is_mounted_and_visible(tmp_path, monkeypatch, qapp):
    """Hiding the controller after domain assembly breaks initial ACP visibility."""
    dialog = _real_dialog(tmp_path, monkeypatch, acp=True)
    try:
        assert dialog.ai_page.provider_kind.currentData() == "acp"
        assert all(not _mounted_row(dialog, key).isHidden() for key in ACP_ROW_KEYS)
        assert _mounted_row(dialog, "acp_command").isVisibleTo(_domain(dialog))
        assert _mounted_row(dialog, "api_url").isHidden()
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_real_dialog_search_and_keyboard_reach_mounted_acp_controls(
    tmp_path, monkeypatch, qapp
):
    """Mounting without rebuilding search or accessible focus paths breaks this test."""
    dialog = _real_dialog(tmp_path, monkeypatch, acp=True)
    try:
        page = dialog.ai_page
        for key in ("provider_kind", *ACP_ROW_KEYS):
            row = _mounted_row(dialog, key)
            assert row.control.accessibleName(), key
            assert row.control.accessibleDescription(), key
            assert row.control.focusPolicy() != Qt.FocusPolicy.NoFocus, key

        page.provider_kind.setFocus(Qt.FocusReason.TabFocusReason)
        assert page.provider_kind.hasFocus()
        QTest.keyClick(page.provider_kind, Qt.Key.Key_Space)
        qapp.processEvents()
        assert page.provider_kind._popup is not None
        assert page.provider_kind._popup.isVisible()
        QTest.keyClick(page.provider_kind._popup, Qt.Key.Key_Escape)
        qapp.processEvents()
        assert page.provider_kind.currentData() == "acp"
        page.acp_section.acp_command.setFocus(Qt.FocusReason.TabFocusReason)
        QTest.keyClicks(page.acp_section.acp_command, "/tmp/agent")
        assert page.acp_section.acp_command.text().endswith("/tmp/agent")

        dialog.search_edit.setText("启动命令")
        qapp.processEvents()
        assert [row.objectName() for row in dialog._search_matches] == [
            "settingRow_acp_command"
        ]
        assert dialog._search_matches[0].isVisibleTo(_domain(dialog))
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("width", [720, 1000, 1400])
def test_real_dialog_acp_geometry_at_compact_standard_and_wide_widths(
    width, tmp_path, monkeypatch, qapp
):
    """Mounting with fixed or overflowing control geometry breaks these widths."""
    dialog = _real_dialog(tmp_path, monkeypatch, acp=True)
    try:
        dialog.resize(width, 700)
        qapp.processEvents()
        qapp.processEvents()
        domain = _domain(dialog)
        scroll = domain.parentWidget()
        while scroll is not None and not isinstance(scroll, QScrollArea):
            scroll = scroll.parentWidget()
        assert scroll is not None
        assert scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        for key in ACP_ROW_KEYS:
            row = _mounted_row(dialog, key)
            control = row.control
            top_left = control.mapTo(domain, QPoint(0, 0))
            assert top_left.x() >= 0, (width, key)
            assert top_left.x() + control.width() <= domain.width() + 1, (width, key)
            assert row.width() <= domain.width() + 1, (width, key)
            scroll.ensureWidgetVisible(row)
            qapp.processEvents()
            assert row.isVisibleTo(scroll.viewport()), (width, key)
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_copy_uses_unsaved_openai_draft_and_new_secret_references(tmp_path, qapp):
    """Constructing a copy from persisted provider values instead of the draft breaks this."""
    page = _page(tmp_path)
    try:
        original_id = page.settings.active_provider
        page.name.setText("Unsaved name")
        page.url.setText("https://draft.example/v1")
        page.model.setText("draft-model")
        page.key.setText("draft-secret")
        page.timeout.setValue(123)
        page.temperature.setValue(1.25)
        page.tokens.setValue(8192)
        page.vision_same.setChecked(False)
        page.vision_model.setText("draft-vision")
        page.vision_url.setText("https://vision.example/v1")
        page.vision_key.setText("draft-vision-secret")
        page.skip_ssl.setChecked(True)
        page.thinking_mode.setCurrentData("enabled")
        page.reasoning_effort.setCurrentData("low")

        page.add_provider_btn.click()
        copied_id = page.settings.active_provider
        copied = page.settings.providers[copied_id]
        draft = page._provider_drafts[copied_id]
        assert copied_id != original_id
        assert copied.name == "Unsaved name 副本"
        assert copied.base_url == "https://draft.example/v1"
        assert copied.model == "draft-model"
        assert copied.timeout == 123
        assert copied.temperature == 1.25
        assert copied.max_tokens == 8192
        assert copied.vision_same_as_chat is False
        assert copied.vision_model == "draft-vision"
        assert copied.vision_base_url == "https://vision.example/v1"
        assert copied.verify_ssl is False
        assert copied.thinking == "enabled"
        assert copied.reasoning_effort == "low"
        assert copied.api_key_ref == f"provider/{copied_id}"
        assert copied.vision_api_key_ref == f"provider/{copied_id}/vision"
        assert copied.api_key == copied.vision_api_key == ""
        assert draft["key"] == "draft-secret"
        assert draft["vision_key"] == "draft-vision-secret"
        assert page._provider_drafts[original_id]["key"] == "draft-secret"
    finally:
        _close(page, qapp)


def test_copy_after_switch_preserves_inactive_acp_draft(tmp_path, qapp):
    """Dropping ACP data when copying an OpenAI-selected provider breaks this test."""
    page = _page(tmp_path)
    try:
        page.provider_kind.setCurrentData("acp")
        page.acp_section.acp_command.setText("/opt/unsaved-agent")
        page.acp_section.acp_args.setText('exec "--draft value"')
        page.provider_kind.setCurrentData("openai")
        page.url.setText("https://inactive-acp.example")
        page.add_provider_btn.click()

        assert page.provider_kind.currentData() == "openai"
        assert page.url.text() == "https://inactive-acp.example"
        page.provider_kind.setCurrentData("acp")
        assert page.acp_section.acp_command.text() == "/opt/unsaved-agent"
        assert page.provisional_config().acp.args == ["exec", "--draft value"]
    finally:
        _close(page, qapp)


@pytest.mark.parametrize(
    ("platform_name", "text", "expected"),
    [
        ("win32", r"C:\tools\agent.exe --profile acp", [r"C:\tools\agent.exe", "--profile", "acp"]),
        ("win32", r'"C:\Program Files\Agent\agent.exe" "C:\work dir"', [r"C:\Program Files\Agent\agent.exe", r"C:\work dir"]),
        ("posix", r"C:\tools\agent.exe", ["C:toolsagent.exe"]),
        ("posix", 'exec "two words"', ["exec", "two words"]),
    ],
)
def test_argument_parsing_respects_platform_without_losing_paths(
    platform_name, text, expected, tmp_path, qapp, monkeypatch
):
    """Always using POSIX shlex destroys Windows paths; non-POSIX quoting breaks POSIX."""
    monkeypatch.setattr(sys, "platform", platform_name)
    page = _page(tmp_path)
    try:
        page.provider_kind.setCurrentData("acp")
        page.acp_section.acp_args.setText(text)
        assert page.provisional_config().acp.args == expected
    finally:
        _close(page, qapp)
