from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from pet.chat.acp.manager import AcpManager
from pet.chat.acp_provider import AcpProvider
from pet.chat.lifecycle import release_window_when_idle
from pet.chat.models import AcpAgentConfig, ChatSession, ProviderConfig
from pet.chat.pet_link import PetChatLink
from pet.chat.service import ChatService


def _get_qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(app: QApplication, predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


MOCK_ACP_SERVICE_SERVER_CODE = textwrap.dedent(
    """\
    import json
    import os
    import sys

    log_file = os.environ.get("MOCK_ACP_LOG_FILE", "")
    crash_flag_file = os.environ.get("MOCK_ACP_CRASH_FLAG_FILE", "")
    load_mode = os.environ.get("MOCK_ACP_LOAD_MODE", "unsupported")

    def log_event(obj):
        if not log_file:
            return
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\\n")

    def send(obj):
        line = (json.dumps(obj, ensure_ascii=False) + "\\n").encode("utf-8")
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

    def write_stderr(text: str):
        sys.stderr.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stderr.buffer.flush()

    current_mode = "ask"
    current_model = "model-a"
    session_counter = 0
    pending_perm_id = None
    pending_prompt_id = None
    pending_session_id = "sess-1"

    while True:
        raw = sys.stdin.buffer.readline()
        if not raw:
            break
        try:
            msg = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue

        # Response from client to agent->client permission request
        if "id" in msg and "method" not in msg:
            if msg["id"] == pending_perm_id and pending_prompt_id is not None:
                outcome = (msg.get("result") or {}).get("outcome") or {}
                outcome_kind = outcome.get("outcome", "")
                option_id = outcome.get("optionId", "")
                if outcome_kind == "selected":
                    send({
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": pending_session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": f"Approved:{option_id}"},
                            },
                        },
                    })
                    send({
                        "jsonrpc": "2.0",
                        "id": pending_prompt_id,
                        "result": {"stopReason": "end_turn"},
                    })
                else:
                    send({
                        "jsonrpc": "2.0",
                        "id": pending_prompt_id,
                        "result": {"stopReason": "cancelled"},
                    })
                pending_perm_id = None
                pending_prompt_id = None
            continue

        method = msg.get("method")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "MockACP", "version": "1.2.3"},
                    "agentCapabilities": {
                        "loadSession": load_mode in ("ok", "fail"),
                    },
                },
            })
        elif method == "session/new":
            session_counter += 1
            sid = f"sess-{os.getpid()}-{session_counter}"
            send({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "sessionId": sid,
                    "modes": {
                        "currentModeId": current_mode,
                        "availableModes": [
                            {"id": "ask", "name": "Ask", "description": "Ask first"},
                            {"id": "code", "name": "Code", "description": "Edit code"},
                        ],
                    },
                    "models": {
                        "currentModelId": current_model,
                        "availableModels": [
                            {"modelId": "model-a", "name": "Model A"},
                            {"modelId": "model-b", "name": "Model B"},
                        ],
                    },
                },
            })
        elif method == "session/load":
            if load_mode == "fail":
                send({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32001, "message": "session not found"},
                })
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "modes": {
                            "currentModeId": current_mode,
                            "availableModes": [
                                {"id": "ask", "name": "Ask"},
                                {"id": "code", "name": "Code"},
                            ],
                        },
                        "models": {
                            "currentModelId": current_model,
                            "availableModels": [
                                {"modelId": "model-a", "name": "Model A"},
                                {"modelId": "model-b", "name": "Model B"},
                            ],
                        },
                    },
                })
        elif method == "session/set_mode":
            current_mode = params.get("modeId", current_mode)
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
        elif method == "session/set_model":
            current_model = params.get("modelId", current_model)
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
        elif method == "session/cancel":
            if pending_prompt_id is not None and pending_perm_id is None:
                send({
                    "jsonrpc": "2.0",
                    "id": pending_prompt_id,
                    "result": {"stopReason": "cancelled"},
                })
                pending_prompt_id = None
        elif method == "session/prompt":
            sid = params.get("sessionId", "sess-1")
            prompt_blocks = params.get("prompt") or []
            prompt_text = ""
            if prompt_blocks and isinstance(prompt_blocks[0], dict):
                prompt_text = str(prompt_blocks[0].get("text", ""))
            log_event({"pid": os.getpid(), "sessionId": sid, "text": prompt_text})

            if crash_flag_file and os.path.exists(crash_flag_file):
                os.remove(crash_flag_file)
                write_stderr("fatal: agent segfaulted during turn\\n")
                sys.exit(42)

            if "ask_permission" in prompt_text:
                pending_session_id = sid
                pending_prompt_id = rpc_id
                pending_perm_id = "perm-rpc-1"
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_thought_chunk",
                            "content": {"type": "text", "text": "Need to run tests"},
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tc-1",
                            "title": "Run pytest",
                            "kind": "execute",
                            "status": "in_progress",
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "id": pending_perm_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": sid,
                        "toolCall": {
                            "toolCallId": "tc-1",
                            "title": "Run pytest",
                            "kind": "execute",
                            "rawInput": {"command": "pytest -q"},
                        },
                        "options": [
                            {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                        ],
                    },
                })
                continue

            if "tool_only" in prompt_text:
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tc-2",
                            "title": "Write file",
                            "kind": "edit",
                            "status": "completed",
                        },
                    },
                })
                send({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"stopReason": "end_turn"},
                })
                continue

            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"Echo:{prompt_text}"},
                    },
                },
            })
            send({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"stopReason": "end_turn"},
            })
    """
)


@pytest.fixture
def mock_acp_script(tmp_path: Path) -> Path:
    script = tmp_path / "mock_acp_service_server.py"
    script.write_text(MOCK_ACP_SERVICE_SERVER_CODE, encoding="utf-8")
    return script


def _make_acp_provider_config(
    script: Path,
    workspace: Path,
    *,
    provider_id: str = "acp-mock",
    env: dict[str, str] | None = None,
    tone_enabled: bool = True,
    tone_prompt: str = "【桌宠语气提示】请保持友好。",
    idle_timeout_minutes: int = 30,
) -> ProviderConfig:
    acp_cfg = AcpAgentConfig(
        preset="custom",
        command=sys.executable,
        args=["-u", str(script)],
        env=dict(env or {}),
        default_cwd=str(workspace),
        tone_enabled=tone_enabled,
        tone_prompt=tone_prompt,
        startup_timeout=10.0,
        idle_timeout_minutes=idle_timeout_minutes,
    )
    return ProviderConfig(
        provider_id=provider_id,
        name="Mock ACP",
        kind="acp",
        acp=acp_cfg,
    )


def _read_prompt_logs(log_file: Path) -> list[dict]:
    if not log_file.exists():
        return []
    lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def test_full_acp_turn_with_activity_and_permission_flow(mock_acp_script: Path, tmp_path: Path):
    app = _get_qapp()
    manager = AcpManager()
    service = ChatService(acp_provider=AcpProvider(manager=manager))
    cfg = _make_acp_provider_config(mock_acp_script, tmp_path)
    session = ChatSession.create("pet-1", cfg.provider_id, "")
    session.workspace_dir = str(tmp_path)

    events: list[tuple] = []
    service.started.connect(lambda rid: events.append(("started", rid)))
    service.session_meta_updated.connect(lambda sid, meta: events.append(("meta", sid, meta)))
    service.activity.connect(lambda rid, kind, text: events.append(("activity", rid, kind, text)))

    def _on_perm(rid, req):
        events.append(("permission", rid, req))
        ok = service.resolve_permission(req.request_id, "allow_once")
        assert ok is True

    service.permission_requested.connect(_on_perm)
    service.delta.connect(lambda rid, text: events.append(("delta", rid, text)))
    service.finished.connect(lambda rid, text: events.append(("finished", rid, text)))
    service.error.connect(lambda rid, text: events.append(("error", rid, text)))

    try:
        rid = service.send([{"role": "user", "content": "please ask_permission"}], cfg, session=session)
        assert _wait_until(app, lambda: any(e[0] == "finished" for e in events), timeout=8.0)

        kinds = [e[0] for e in events]
        assert "error" not in kinds
        assert kinds[0] == "started"
        assert "meta" in kinds
        assert "activity" in kinds
        assert "permission" in kinds
        assert "delta" in kinds
        assert kinds[-1] == "finished"

        # Verify activity captured both reasoning and tool status
        activities = [(e[2], e[3]) for e in events if e[0] == "activity"]
        assert any(k == "reasoning" and "Need to run tests" in t for k, t in activities)
        assert any(k == "status" and "Run pytest" in t for k, t in activities)

        # Verify ordering: started < meta < permission < delta < finished
        assert kinds.index("started") < kinds.index("meta")
        assert kinds.index("meta") < kinds.index("permission")
        assert kinds.index("permission") < kinds.index("delta")
        assert kinds.index("delta") < kinds.index("finished")

        finished_event = next(e for e in events if e[0] == "finished")
        assert finished_event[1] == rid
        assert finished_event[2] == "Approved:allow_once"
        assert session.acp_session_id != ""
    finally:
        service.shutdown()
        manager.shutdown_all()


def test_global_finished_fires_and_memory_extract_skipped_for_acp_only(
    mock_acp_script: Path, tmp_path: Path
):
    app = _get_qapp()
    manager = AcpManager()

    class _FakeOpenAI:
        def stream(self, messages, config, cancel):
            yield "openai-reply"

    service = ChatService(provider=_FakeOpenAI(), acp_provider=AcpProvider(manager=manager))
    global_replies: list[str] = []
    memory_calls: list[tuple] = []

    def _global_cb(text: str):
        global_replies.append(text)

    ChatService.register_global_finished(_global_cb)
    service.memory_extract_hook = lambda user, reply, prov, cfg: memory_calls.append(
        (user, reply, cfg.kind)
    )

    try:
        # 1) ACP turn: global finished fires, memory_extract_hook NOT called
        acp_cfg = _make_acp_provider_config(mock_acp_script, tmp_path, tone_enabled=False)
        session = ChatSession.create("pet-1", acp_cfg.provider_id, "")
        session.workspace_dir = str(tmp_path)

        acp_done: list[str] = []
        service.finished.connect(lambda _rid, txt: acp_done.append(txt))
        service.send([{"role": "user", "content": "hi acp"}], acp_cfg, session=session)
        assert _wait_until(app, lambda: len(acp_done) == 1, timeout=8.0)

        assert global_replies == ["Echo:hi acp"]
        assert memory_calls == []

        # 2) OpenAI turn: both global finished AND memory_extract_hook fire
        openai_cfg = ProviderConfig(
            provider_id="openai-test",
            name="OpenAI",
            base_url="http://invalid",
            model="gpt-test",
        )
        service.send([{"role": "user", "content": "hi openai"}], openai_cfg)
        assert _wait_until(app, lambda: len(acp_done) == 2, timeout=5.0)

        assert global_replies == ["Echo:hi acp", "openai-reply"]
        assert len(memory_calls) == 1
        assert memory_calls[0] == ("hi openai", "openai-reply", "openai")
    finally:
        ChatService.unregister_global_finished(_global_cb)
        service.shutdown()
        manager.shutdown_all()


def test_tone_preamble_injected_only_on_first_turn_of_fresh_session(
    mock_acp_script: Path, tmp_path: Path
):
    app = _get_qapp()
    manager = AcpManager()
    log_file = tmp_path / "prompts.jsonl"
    tone = "【桌宠语气】简洁可爱"

    service = ChatService(acp_provider=AcpProvider(manager=manager))
    done: list[str] = []
    meta_events: list = []
    service.finished.connect(lambda _rid, txt: done.append(txt))
    service.session_meta_updated.connect(lambda sid, meta: meta_events.append((sid, meta)))

    try:
        # Case A: fresh session with tone_enabled=True -> turn 1 gets tone, turn 2 does NOT
        cfg_tone = _make_acp_provider_config(
            mock_acp_script,
            tmp_path,
            env={"MOCK_ACP_LOG_FILE": str(log_file)},
            tone_enabled=True,
            tone_prompt=tone,
        )
        session1 = ChatSession.create("pet-1", cfg_tone.provider_id, "")
        session1.workspace_dir = str(tmp_path)

        service.send([{"role": "user", "content": "turn one"}], cfg_tone, session=session1)
        assert _wait_until(app, lambda: len(done) == 1, timeout=8.0)

        service.send([{"role": "user", "content": "turn two"}], cfg_tone, session=session1)
        assert _wait_until(app, lambda: len(done) == 2, timeout=8.0)

        logs = _read_prompt_logs(log_file)
        assert len(logs) == 2
        assert logs[0]["text"].startswith(tone)
        assert "turn one" in logs[0]["text"]
        assert logs[1]["text"] == "turn two"

        # Case B: tone_enabled=False -> turn 1 does NOT get tone
        cfg_no_tone = _make_acp_provider_config(
            mock_acp_script,
            tmp_path,
            env={"MOCK_ACP_LOG_FILE": str(log_file)},
            tone_enabled=False,
            tone_prompt=tone,
        )
        session2 = ChatSession.create("pet-1", cfg_no_tone.provider_id, "")
        session2.workspace_dir = str(tmp_path)

        service.send([{"role": "user", "content": "quiet turn"}], cfg_no_tone, session=session2)
        assert _wait_until(app, lambda: len(done) == 3, timeout=8.0)

        logs = _read_prompt_logs(log_file)
        assert len(logs) == 3
        assert logs[2]["text"] == "quiet turn"

        # Case C: eager prepare_acp_session() before turn 1 -> turn 1 STILL gets tone, turn 2 does NOT
        session3 = ChatSession.create("pet-1", cfg_tone.provider_id, "")
        session3.workspace_dir = str(tmp_path)
        prev_meta_count = len(meta_events)
        service.prepare_acp_session(cfg_tone, session3)
        assert _wait_until(app, lambda: len(meta_events) > prev_meta_count, timeout=8.0)
        assert session3.acp_session_id != ""

        service.send([{"role": "user", "content": "after prepare 1"}], cfg_tone, session=session3)
        assert _wait_until(app, lambda: len(done) == 4, timeout=8.0)

        service.send([{"role": "user", "content": "after prepare 2"}], cfg_tone, session=session3)
        assert _wait_until(app, lambda: len(done) == 5, timeout=8.0)

        logs = _read_prompt_logs(log_file)
        assert len(logs) == 5
        assert logs[3]["text"].startswith(tone)
        assert "after prepare 1" in logs[3]["text"]
        assert logs[4]["text"] == "after prepare 2"
    finally:
        service.shutdown()
        manager.shutdown_all()


def test_tool_only_turn_yields_fallback_completion_text(mock_acp_script: Path, tmp_path: Path):
    app = _get_qapp()
    manager = AcpManager()
    service = ChatService(acp_provider=AcpProvider(manager=manager))
    cfg = _make_acp_provider_config(mock_acp_script, tmp_path, tone_enabled=False)
    session = ChatSession.create("pet-1", cfg.provider_id, "")
    session.workspace_dir = str(tmp_path)

    finished: list[str] = []
    errors: list[str] = []
    activities: list[tuple[str, str]] = []
    service.finished.connect(lambda _rid, txt: finished.append(txt))
    service.error.connect(lambda _rid, txt: errors.append(txt))
    service.activity.connect(lambda _rid, kind, txt: activities.append((kind, txt)))

    try:
        service.send([{"role": "user", "content": "run tool_only"}], cfg, session=session)
        assert _wait_until(app, lambda: len(finished) == 1 or len(errors) == 1, timeout=8.0)

        assert errors == []
        assert finished == ["已完成操作。"]
        assert any(k == "status" and "Write file" in t for k, t in activities)
    finally:
        service.shutdown()
        manager.shutdown_all()


def test_detach_while_permission_pending_unblocks_and_preserves_reusable_client(
    mock_acp_script: Path, tmp_path: Path
):
    app = _get_qapp()
    manager = AcpManager()
    acp_provider = AcpProvider(manager=manager)
    service = ChatService(acp_provider=acp_provider)
    cfg = _make_acp_provider_config(mock_acp_script, tmp_path, tone_enabled=False)
    session = ChatSession.create("pet-1", cfg.provider_id, "")
    session.workspace_dir = str(tmp_path)

    perm_received: list = []
    post_detach_signals: list = []
    detached = [False]

    service.permission_requested.connect(lambda rid, req: perm_received.append((rid, req)))
    service.stopped.connect(lambda rid: post_detach_signals.append(("stopped", rid)) if detached[0] else None)
    service.error.connect(lambda rid, txt: post_detach_signals.append(("error", rid, txt)) if detached[0] else None)
    service.finished.connect(lambda rid, txt: post_detach_signals.append(("finished", rid, txt)) if detached[0] else None)

    dlg = QDialog()
    destroyed = [False]
    dlg.destroyed.connect(lambda: destroyed.__setitem__(0, True))

    try:
        service.send([{"role": "user", "content": "please ask_permission"}], cfg, session=session)
        assert _wait_until(app, lambda: len(perm_received) == 1, timeout=8.0)

        # Grab live client reference before detaching
        client_before = manager.acquire(cfg.provider_id, cfg.acp, str(tmp_path))
        assert client_before.is_alive

        # Simulate close-destroys-window while permission card is pending
        detached[0] = True
        service.detach()
        release_window_when_idle(dlg, service)

        assert _wait_until(app, lambda: not service.has_running_workers(), timeout=8.0)
        assert _wait_until(app, lambda: destroyed[0], timeout=5.0)
        assert post_detach_signals == []

        # The underlying ACP client must remain alive and reusable in AcpManager
        client_after = manager.acquire(cfg.provider_id, cfg.acp, str(tmp_path))
        assert client_after is client_before
        assert client_after.is_alive

        # A new ChatService using the same manager can immediately run a turn on it
        service2 = ChatService(acp_provider=AcpProvider(manager=manager))
        done2: list[str] = []
        service2.finished.connect(lambda _rid, txt: done2.append(txt))
        try:
            service2.send([{"role": "user", "content": "still alive"}], cfg, session=session)
            assert _wait_until(app, lambda: len(done2) == 1, timeout=8.0)
            assert done2 == ["Echo:still alive"]
        finally:
            service2.shutdown()
    finally:
        service.shutdown()
        manager.shutdown_all()


def test_agent_crash_mid_turn_emits_error_with_stderr_and_evicts_client(
    mock_acp_script: Path, tmp_path: Path
):
    app = _get_qapp()
    manager = AcpManager()
    crash_flag = tmp_path / "crash_once.flag"
    crash_flag.write_text("1", encoding="utf-8")

    service = ChatService(acp_provider=AcpProvider(manager=manager))
    cfg = _make_acp_provider_config(
        mock_acp_script,
        tmp_path,
        env={"MOCK_ACP_CRASH_FLAG_FILE": str(crash_flag)},
        tone_enabled=False,
    )
    session = ChatSession.create("pet-1", cfg.provider_id, "")
    session.workspace_dir = str(tmp_path)

    errors: list[str] = []
    finished: list[str] = []
    service.error.connect(lambda _rid, txt: errors.append(txt))
    service.finished.connect(lambda _rid, txt: finished.append(txt))

    try:
        # Turn 1 crashes mid-turn
        service.send([{"role": "user", "content": "trigger crash"}], cfg, session=session)
        assert _wait_until(app, lambda: len(errors) == 1, timeout=8.0)
        assert "fatal: agent segfaulted during turn" in errors[0]
        assert finished == []

        # Turn 2 automatically respawns a fresh ACP process and succeeds
        service.send([{"role": "user", "content": "recover after crash"}], cfg, session=session)
        assert _wait_until(app, lambda: len(finished) == 1, timeout=8.0)
        assert finished == ["Echo:recover after crash"]
    finally:
        service.shutdown()
        manager.shutdown_all()


def test_acp_manager_reap_idle_and_shutdown_all(mock_acp_script: Path, tmp_path: Path):
    manager = AcpManager()
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()

    cfg_timeout = _make_acp_provider_config(
        mock_acp_script, ws1, provider_id="acp-1", idle_timeout_minutes=1
    )
    cfg_never = _make_acp_provider_config(
        mock_acp_script, ws2, provider_id="acp-2", idle_timeout_minutes=0
    )

    try:
        c1 = manager.acquire(cfg_timeout.provider_id, cfg_timeout.acp, str(ws1))
        c2 = manager.acquire(cfg_never.provider_id, cfg_never.acp, str(ws2))
        assert c1.is_alive
        assert c2.is_alive

        # Not yet timed out
        assert manager.reap_idle(now=c1.last_activity_at + 10.0) == 0
        assert c1.is_alive
        assert c2.is_alive

        # Busy client should NOT be reaped even past timeout
        manager.mark_busy((cfg_timeout.provider_id, str(ws1)), True)
        assert manager.reap_idle(now=c1.last_activity_at + 120.0) == 0
        assert c1.is_alive
        manager.mark_busy((cfg_timeout.provider_id, str(ws1)), False)

        # Past timeout -> c1 reaped, c2 (idle_timeout_minutes == 0) kept alive
        reaped = manager.reap_idle(now=c1.last_activity_at + 120.0)
        assert reaped == 1
        assert not c1.is_alive
        assert c2.is_alive

        # probe_connection works cleanly without leaking into manager
        ok, msg = manager.probe_connection(cfg_timeout)
        assert ok is True
        assert "MockACP" in msg

        # shutdown_all terminates remaining clients cleanly
        manager.shutdown_all()
        assert not c2.is_alive
    finally:
        manager.shutdown_all()


def test_context_reset_and_pet_link_waiting_permission(mock_acp_script: Path, tmp_path: Path):
    app = _get_qapp()
    manager = AcpManager()
    service = ChatService(acp_provider=AcpProvider(manager=manager))
    cfg = _make_acp_provider_config(
        mock_acp_script,
        tmp_path,
        env={"MOCK_ACP_LOAD_MODE": "fail"},
        tone_enabled=False,
    )
    session = ChatSession.create("pet-1", cfg.provider_id, "")
    session.workspace_dir = str(tmp_path)
    session.acp_session_id = "stale-session-id"

    resets: list[tuple[str, str]] = []
    finished: list[str] = []
    service.context_reset.connect(lambda rid, notice: resets.append((rid, notice)))
    service.finished.connect(lambda _rid, txt: finished.append(txt))

    try:
        rid = service.send([{"role": "user", "content": "resume me"}], cfg, session=session)
        assert _wait_until(app, lambda: len(finished) == 1, timeout=8.0)

        assert len(resets) == 1
        assert resets[0][0] == rid
        assert "已开启新的 Agent 上下文" in resets[0][1]
        assert session.acp_session_id != "stale-session-id"
    finally:
        service.shutdown()
        manager.shutdown_all()

    # PetChatLink.waiting_permission
    recorded: list[tuple[str, str]] = []

    class _FakePetWin:
        def set_chat_status(self, state, text):
            recorded.append((state, text))

    link = PetChatLink(_FakePetWin())
    link.waiting_permission()
    assert recorded == [("waiting_permission", "需要你确认权限～")]


def test_async_set_mode_and_model_and_pure_python_manager_import(
    mock_acp_script: Path, tmp_path: Path
):
    import ast
    import inspect
    import pet.chat.acp.manager as manager_mod

    # Verify pet/chat/acp/manager.py does not import PySide6 at module top level
    tree = ast.parse(inspect.getsource(manager_mod))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("PySide6")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("PySide6")

    app = _get_qapp()
    manager = AcpManager()
    service = ChatService(acp_provider=AcpProvider(manager=manager))
    cfg = _make_acp_provider_config(mock_acp_script, tmp_path, tone_enabled=False)
    session = ChatSession.create("pet-1", cfg.provider_id, "")
    session.workspace_dir = str(tmp_path)

    metas: list = []
    service.session_meta_updated.connect(lambda sid, meta: metas.append((sid, meta)))

    try:
        service.set_acp_mode(cfg, session, "code")
        assert _wait_until(
            app,
            lambda: any(m.current_mode_id == "code" for _, m in metas),
            timeout=8.0,
        )
        assert session.acp_mode_id == "code"

        service.set_acp_model(cfg, session, "model-b")
        assert _wait_until(
            app,
            lambda: any(m.current_model_id == "model-b" for _, m in metas),
            timeout=8.0,
        )
        assert session.acp_model_id == "model-b"

        assert _wait_until(app, lambda: len(service._workers) == 0, timeout=5.0)
    finally:
        service.shutdown()
        manager.shutdown_all()

