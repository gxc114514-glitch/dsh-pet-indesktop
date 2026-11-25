from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from pet.chat.acp.manager import AcpManager
from pet.chat.acp.protocol import AcpSessionMeta
from pet.chat.acp_provider import AcpProvider
from pet.chat.models import AcpAgentConfig, ChatMessage, ProviderConfig
from pet.chat.permission_card import ContextResetBanner, PermissionCard
from pet.chat.service import ChatService
from pet.config import Config
from tests.test_acp_service import MOCK_ACP_SERVICE_SERVER_CODE


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def _timeline_widgets(window, widget_type):
    return [
        item.widget()
        for index in range(window.message_host_layout.count())
        if (item := window.message_host_layout.itemAt(index)) is not None
        and isinstance(item.widget(), widget_type)
    ]


def _activate_acp(window) -> None:
    index = window.provider_combo.findData("acp-mock")
    assert index >= 0
    window.provider_combo.setCurrentIndex(index)
    window.provider_combo.activated.emit(index)
    assert _wait_until(lambda: bool(window.session.acp_session_id))
    assert _wait_until(lambda: window.acp_mode_combo.count() == 2)


@pytest.fixture
def acp_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _app()
    script = tmp_path / "mock_acp_ui_server.py"
    script.write_text(MOCK_ACP_SERVICE_SERVER_CODE, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = Config(tmp_path / "config")
    settings = config.chat_settings()
    acp = ProviderConfig(
        provider_id="acp-mock",
        name="Mock Agent",
        kind="acp",
        acp=AcpAgentConfig(
            command=sys.executable,
            args=["-u", str(script)],
            default_cwd=str(workspace),
            tone_enabled=False,
            startup_timeout=10.0,
        ),
    )
    settings.providers[acp.provider_id] = acp
    settings.active_provider = "openai-main"
    config.set_chat_settings(settings)
    assert config.save()

    manager = AcpManager()
    import pet.chat.widgets as widgets

    def make_service(parent=None):
        return ChatService(parent=parent, acp_provider=AcpProvider(manager=manager))

    monkeypatch.setattr(widgets, "ChatService", make_service)

    class FakePet:
        def __init__(self):
            self.states = []

        def set_chat_status(self, state, text):
            self.states.append((state, text))

    pet = FakePet()
    window = widgets.ChatWindow(config, "shenshen", pet_window=pet)
    yield SimpleNamespace(
        app=app,
        window=window,
        config=config,
        manager=manager,
        pet=pet,
        tmp_path=tmp_path,
    )
    window.close()
    window.service.shutdown()
    manager.shutdown_all()
    window.store.flush()
    app.processEvents()


def test_openai_hides_acp_controls(acp_ui):
    window = acp_ui.window
    assert window.provider_combo.count() == 2
    assert window.provider_combo.currentText().startswith("[API]")
    assert window.workspace_bar.isHidden()
    assert window.acp_mode_combo.isHidden()
    assert window.acp_model_combo.isHidden()
    assert window.provider_combo.accessibleName()
    assert window.provider_combo.accessibleDescription()


def test_acp_switch_prepares_metadata_and_hides_empty_models(acp_ui):
    window = acp_ui.window
    _activate_acp(window)
    assert not window.workspace_bar.isHidden()
    assert not window.acp_mode_combo.isHidden()
    assert not window.acp_model_combo.isHidden()
    assert [window.acp_mode_combo.itemData(i) for i in range(2)] == ["ask", "code"]
    assert [window.acp_model_combo.itemData(i) for i in range(2)] == ["model-a", "model-b"]
    assert window.session.provider_id == "acp-mock"
    assert window.session.acp_mode_id == "ask"
    assert window.session.acp_model_id == "model-a"

    window.service.session_meta_updated.emit(
        window.session.session_id,
        AcpSessionMeta(session_id=window.session.acp_session_id, modes=[], models=[]),
    )
    acp_ui.app.processEvents()
    assert window.acp_mode_combo.isHidden()
    assert window.acp_model_combo.isHidden()


def test_workspace_updates_empty_session_and_forks_message_session(acp_ui):
    window = acp_ui.window
    _activate_acp(window)
    first = acp_ui.tmp_path / "first"
    second = acp_ui.tmp_path / "second"
    first.mkdir()
    second.mkdir()

    original_id = window.session.session_id
    window._change_workspace(str(first))
    assert window.session.session_id == original_id
    assert window.session.workspace_dir == str(first)
    assert window.settings.recent_workspaces[0] == str(first)
    assert _wait_until(lambda: window.session.acp_session_id != "")

    window.session.messages.append(ChatMessage("user", "existing"))
    window.store.save(window.session)
    window._change_workspace(str(second))
    assert window.session.session_id != original_id
    assert window.session.workspace_dir == str(second)
    assert window.session.messages == []
    assert window.settings.recent_workspaces[:2] == [str(second), str(first)]


def test_acp_permission_turn_streams_persists_and_notifies(acp_ui, monkeypatch):
    window = acp_ui.window
    _activate_acp(window)
    monkeypatch.setattr(
        window,
        "_memory_injection",
        lambda: (_ for _ in ()).throw(AssertionError("ACP must not read pet memory")),
    )
    monkeypatch.setattr(
        window.prompt_builder,
        "build_messages",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ACP must not build a roleplay prompt")
        ),
    )
    global_replies = []
    ChatService.register_global_finished(global_replies.append)
    try:
        window.input.setPlainText("please ask_permission")
        window.send_message()
        assert _wait_until(lambda: bool(_timeline_widgets(window, PermissionCard)))
        card = _timeline_widgets(window, PermissionCard)[0]
        assert card.request.request_id
        assert card.option_buttons[0].accessibleName()
        assert window._bubble.has_thinking
        assert any("Run pytest" in text for text in window._bubble.thinking_record[1])
        assert ("waiting_permission", "需要你确认权限～") in acp_ui.pet.states

        card.option_buttons[0].click()
        assert card.state == "allowed"
        assert card.option_buttons[0].isHidden()
        assert not card.audit_label.isHidden()
        assert "Allow once" in card.audit_label.text()
        assert _wait_until(
            lambda: bool(window.session.messages)
            and window.session.messages[-1].content == "Approved:allow_once"
        )
        assert global_replies == ["Approved:allow_once"]
        assert window.store.flush()
        stored = window.store.load(window.session.session_id, window.character_id)
        assert stored.acp_session_id == window.session.acp_session_id
        assert stored.acp_mode_id == "ask"
        assert stored.acp_model_id == "model-a"
    finally:
        ChatService.unregister_global_finished(global_replies.append)


def test_stop_cancels_permission_and_busy_switch_respects_confirmation(
    acp_ui, monkeypatch
):
    window = acp_ui.window
    _activate_acp(window)
    window.input.setPlainText("please ask_permission")
    window.send_message()
    assert _wait_until(lambda: bool(_timeline_widgets(window, PermissionCard)))
    card = _timeline_widgets(window, PermissionCard)[0]
    other = window._new_session()
    current_id = window.session.session_id

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No),
    )
    window.select_session(other.session_id)
    assert window.session.session_id == current_id
    assert window.service.busy
    assert card.state == "pending"

    window.send.click()
    assert card.state == "cancelled"
    assert _wait_until(lambda: not window.service.busy)
    assert not card.audit_label.isHidden()


def test_context_reset_inserts_banner(acp_ui):
    window = acp_ui.window
    _activate_acp(window)
    window.service.context_reset.emit("", "已开启新的 Agent 上下文")
    acp_ui.app.processEvents()
    banners = _timeline_widgets(window, ContextResetBanner)
    assert len(banners) == 1
    assert "新的 Agent 上下文" in banners[0].text


@pytest.mark.parametrize("action", ["delete", "clear"])
def test_busy_destructive_action_rejection_preserves_turn_and_permission(
    acp_ui, monkeypatch, action
):
    window = acp_ui.window
    _activate_acp(window)
    window.input.setPlainText("please ask_permission")
    window.send_message()
    assert _wait_until(lambda: bool(_timeline_widgets(window, PermissionCard)))
    card = _timeline_widgets(window, PermissionCard)[0]
    session_id = window.session.session_id
    request_id = window._active_request_id
    worker = window.service._worker

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No),
    )
    if action == "delete":
        monkeypatch.setattr(
            "pet.chat.widgets.DeleteConversationDialog.exec",
            lambda _dialog: QDialog.DialogCode.Accepted,
        )
        assert window._delete_sessions((session_id,)) is False
    else:
        window.clear_session()

    assert window.session.session_id == session_id
    assert window._active_request_id == request_id
    assert window.service._worker is worker
    assert window.service.busy
    assert card.state == "pending"
    assert card in _timeline_widgets(window, PermissionCard)


@pytest.mark.parametrize("action", ["delete", "clear"])
def test_busy_destructive_action_acceptance_cancels_turn_and_permission(
    acp_ui, monkeypatch, action
):
    window = acp_ui.window
    _activate_acp(window)
    window.input.setPlainText("please ask_permission")
    window.send_message()
    assert _wait_until(lambda: bool(_timeline_widgets(window, PermissionCard)))
    card = _timeline_widgets(window, PermissionCard)[0]
    session_id = window.session.session_id

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    if action == "delete":
        monkeypatch.setattr(
            "pet.chat.widgets.DeleteConversationDialog.exec",
            lambda _dialog: QDialog.DialogCode.Accepted,
        )
        assert window._delete_sessions((session_id,)) is True
        assert window.session.session_id != session_id
        assert window.store.load(session_id, window.character_id) is None
    else:
        window.clear_session()
        assert window.session.session_id == session_id
        assert window.session.messages == []

    assert card.state == "cancelled"
    assert not card.audit_label.isHidden()
    assert window._active_request_id is None
    assert _wait_until(lambda: not window.service.busy)
