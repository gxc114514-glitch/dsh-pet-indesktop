from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from pet.chat.acp.client import AcpClient
from pet.chat.acp.protocol import (
    AcpError,
    AcpModeOption,
    AcpModelOption,
    AcpPermissionRequest,
    AcpSessionMeta,
    AcpUpdateEvent,
    format_permission_response,
    parse_permission_request,
    parse_session_meta,
    parse_session_update,
)
from pet.chat.models import AcpAgentConfig


MOCK_ACP_SERVER_CODE = textwrap.dedent(
    """\
    import json
    import os
    import sys
    import time

    mode = os.environ.get("MOCK_ACP_MODE", "normal")
    pid_file = os.environ.get("MOCK_ACP_PID_FILE", "")
    if pid_file:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    def send(obj):
        line = (json.dumps(obj, ensure_ascii=False) + "\\n").encode("utf-8")
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()

    def write_raw_stdout(data: bytes):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    def write_stderr(text: str):
        sys.stderr.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stderr.buffer.flush()

    if mode == "hang_init":
        time.sleep(60)
        sys.exit(0)

    if mode == "noisy_banner":
        write_raw_stdout(b"Non-JSON startup banner with invalid utf8: \\xff\\xfe\\n")
        write_raw_stdout(b"Another plain log line\\n")
        for i in range(300):
            write_stderr(f"stderr line {i}\\n")

    current_mode = "ask"
    current_model = "model-a"
    last_init_params = None
    pending_perm_id = None
    pending_prompt_id = None
    cancelled_seen = False

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

        # Response from client to an agent->client request (e.g. permission)
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
                            "sessionId": "sess-1",
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": f"perm:{option_id}"},
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
                        "result": {"stopReason": "cancelled", "cancelledSeen": cancelled_seen},
                    })
                pending_perm_id = None
                pending_prompt_id = None
            continue

        method = msg.get("method")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            last_init_params = params
            send({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "MockACP", "version": "0.9.1"},
                    "agentCapabilities": {
                        "loadSession": mode in ("load_ok", "load_fail"),
                    },
                    "echoClientCapabilities": params.get("clientCapabilities"),
                },
            })
        elif method == "session/new":
            if mode == "config_options":
                send({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "sessionId": "sess-cfg-1",
                        "configOptions": [
                            {
                                "id": "cfg-mode",
                                "name": "Mode",
                                "category": "mode",
                                "type": "select",
                                "currentValue": current_mode,
                                "options": [
                                    {"value": "ask", "name": "Ask", "description": "Ask mode"},
                                    {"value": "code", "name": "Code", "description": "Code mode"},
                                ],
                            },
                            {
                                "id": "cfg-model",
                                "name": "Model",
                                "category": "model",
                                "type": "select",
                                "currentValue": current_model,
                                "options": [
                                    {
                                        "group": "Primary",
                                        "name": "Primary",
                                        "options": [
                                            {"value": "model-a", "name": "Model A"},
                                            {"value": "model-b", "name": "Model B"},
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                })
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "sessionId": "sess-1",
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
                                {"modelId": "model-a", "name": "Model A", "description": "Fast"},
                                {"modelId": "model-b", "name": "Model B", "description": "Smart"},
                            ],
                        },
                    },
                })
        elif method == "session/load":
            if mode == "load_fail":
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
                            "currentModeId": "code",
                            "availableModes": [
                                {"id": "ask", "name": "Ask"},
                                {"id": "code", "name": "Code"},
                            ],
                        },
                        "models": {
                            "currentModelId": "model-b",
                            "availableModels": [
                                {"modelId": "model-a", "name": "Model A"},
                                {"modelId": "model-b", "name": "Model B"},
                            ],
                        },
                    },
                })
        elif method == "session/set_mode":
            current_mode = params.get("modeId", current_mode)
            send({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": params.get("sessionId", "sess-1"),
                    "update": {
                        "sessionUpdate": "current_mode_update",
                        "currentModeId": current_mode,
                    },
                },
            })
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
        elif method == "session/set_model":
            current_model = params.get("modelId", current_model)
            send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})
        elif method == "session/set_config_option":
            cid = params.get("configId")
            val = params.get("value")
            if cid == "cfg-mode":
                current_mode = val
            elif cid == "cfg-model":
                current_model = val
            send({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "configOptions": [
                        {
                            "id": "cfg-mode",
                            "name": "Mode",
                            "category": "mode",
                            "type": "select",
                            "currentValue": current_mode,
                            "options": [
                                {"value": "ask", "name": "Ask"},
                                {"value": "code", "name": "Code"},
                            ],
                        },
                        {
                            "id": "cfg-model",
                            "name": "Model",
                            "category": "model",
                            "type": "select",
                            "currentValue": current_model,
                            "options": [
                                {"value": "model-a", "name": "Model A"},
                                {"value": "model-b", "name": "Model B"},
                            ],
                        },
                    ]
                },
            })
        elif method == "session/cancel":
            cancelled_seen = True
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

            if mode == "crash_mid_turn":
                write_stderr("fatal: agent segfaulted\\n")
                sys.exit(42)

            if "ask_permission" in prompt_text:
                pending_prompt_id = rpc_id
                pending_perm_id = "perm-rpc-1"
                send({
                    "jsonrpc": "2.0",
                    "id": pending_perm_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": sid,
                        "toolCall": {
                            "toolCallId": "tc-99",
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
                # Also emit a status update right after requesting permission to verify
                # the client reader thread is NOT blocked by the permission callback!
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "tc-99",
                            "title": "Run pytest",
                            "status": "pending",
                        },
                    },
                })
                continue

            # Normal streaming updates
            updates = [
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "thinking..."},
                },
                {
                    "sessionUpdate": "plan",
                    "entries": [
                        {"content": "Inspect code", "status": "completed"},
                        {"content": "Run tests", "status": "in_progress"},
                    ],
                },
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": "Read file",
                    "kind": "read",
                    "status": "in_progress",
                },
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-1",
                    "title": "Read file",
                    "status": "completed",
                },
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Hello "},
                },
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "world!"},
                },
            ]
            for upd in updates:
                send({
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": sid, "update": upd},
                })
            send({
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {"stopReason": "end_turn"},
            })
    """
)


@pytest.fixture
def mock_server_script(tmp_path: Path) -> Path:
    script = tmp_path / "mock_acp_server.py"
    script.write_text(MOCK_ACP_SERVER_CODE, encoding="utf-8")
    return script


def _make_config(
    script: Path,
    *,
    mode: str = "normal",
    startup_timeout: float = 10.0,
    extra_env: dict[str, str] | None = None,
) -> AcpAgentConfig:
    env = {"MOCK_ACP_MODE": mode}
    if extra_env:
        env.update(extra_env)
    return AcpAgentConfig(
        preset="custom",
        command=sys.executable,
        args=["-u", str(script)],
        env=env,
        startup_timeout=startup_timeout,
    )


def _pid_is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        res = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in res.stdout
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# Pure protocol parser unit tests
# ---------------------------------------------------------------------------


def test_parse_session_meta_legacy_and_config_options():
    legacy = parse_session_meta(
        "s1",
        {
            "modes": {
                "currentModeId": "ask",
                "availableModes": [
                    {"id": "ask", "name": "Ask", "description": "Ask first"},
                    {"id": "code", "name": "Code"},
                ],
            },
            "models": {
                "currentModelId": "m1",
                "availableModels": [
                    {"modelId": "m1", "name": "Model 1", "description": "Fast"},
                ],
            },
        },
    )
    assert legacy.session_id == "s1"
    assert legacy.current_mode_id == "ask"
    assert [m.mode_id for m in legacy.modes] == ["ask", "code"]
    assert legacy.current_model_id == "m1"
    assert [m.model_id for m in legacy.models] == ["m1"]
    assert legacy.mode_config_id == ""
    assert legacy.model_config_id == ""

    modern = parse_session_meta(
        "s2",
        {
            "configOptions": [
                {
                    "id": "mode-opt",
                    "category": "mode",
                    "currentValue": "plan",
                    "options": [
                        {"value": "plan", "name": "Plan", "description": "Plan only"},
                        {"value": "act", "name": "Act"},
                    ],
                },
                {
                    "id": "model-opt",
                    "category": "model",
                    "currentValue": "gpt-5",
                    "options": [
                        {
                            "group": "OpenAI",
                            "options": [
                                {"value": "gpt-5", "name": "GPT-5"},
                                {"value": "o3", "name": "o3"},
                            ],
                        }
                    ],
                },
            ]
        },
    )
    assert modern.mode_config_id == "mode-opt"
    assert modern.current_mode_id == "plan"
    assert [m.mode_id for m in modern.modes] == ["plan", "act"]
    assert modern.model_config_id == "model-opt"
    assert modern.current_model_id == "gpt-5"
    assert [m.model_id for m in modern.models] == ["gpt-5", "o3"]


def test_parse_permission_and_format_response():
    req = parse_permission_request(
        42,
        {
            "sessionId": "s1",
            "toolCall": {
                "toolCallId": "tc-1",
                "title": "Run shell",
                "kind": "execute",
                "rawInput": {"cmd": "ls"},
            },
            "options": [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Deny", "kind": "reject_once"},
            ],
        },
    )
    assert isinstance(req, AcpPermissionRequest)
    assert req.rpc_id == 42
    assert req.tool_call_id == "tc-1"
    assert req.title == "Run shell"
    assert "ls" in req.detail
    assert len(req.options) == 2

    assert format_permission_response("allow_once") == {
        "outcome": {"outcome": "selected", "optionId": "allow_once"}
    }
    assert format_permission_response(None) == {
        "outcome": {"outcome": "cancelled"}
    }


def test_parse_session_update_config_option_update():
    base = AcpSessionMeta(
        session_id="s1",
        modes=[AcpModeOption("ask", "Ask")],
        current_mode_id="ask",
        models=[AcpModelOption("m1", "M1")],
        current_model_id="m1",
        mode_config_id="cfg-mode",
    )
    events = parse_session_update(
        {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {
                        "id": "cfg-mode",
                        "category": "mode",
                        "currentValue": "code",
                        "options": [
                            {"value": "ask", "name": "Ask"},
                            {"value": "code", "name": "Code"},
                        ],
                    }
                ],
            },
        },
        current_meta=base,
    )
    assert len(events) == 1
    assert events[0].kind == "meta"
    assert events[0].session_meta is not None
    assert events[0].session_meta.current_mode_id == "code"
    assert events[0].session_meta.current_model_id == "m1"


# ---------------------------------------------------------------------------
# End-to-end AcpClient tests against real subprocess mock ACP server
# ---------------------------------------------------------------------------


def test_initialize_handshake_capabilities_and_agent_info(
    mock_server_script: Path, tmp_path: Path
):
    client = AcpClient(_make_config(mock_server_script), str(tmp_path))
    try:
        init_res = client.start()
        assert client.is_alive
        assert client.agent_name == "MockACP"
        assert client.agent_version == "0.9.1"
        assert client.supports_load_session is False
        caps = init_res.get("echoClientCapabilities")
        assert caps == {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
        }
    finally:
        client.close()
    assert not client.is_alive


def test_ensure_session_legacy_config_options_and_load_fallback(
    mock_server_script: Path, tmp_path: Path
):
    # 1. Legacy schema + new session
    c1 = AcpClient(_make_config(mock_server_script, mode="normal"), str(tmp_path))
    try:
        c1.start()
        meta, is_fresh, reset = c1.ensure_session()
        assert meta.session_id == "sess-1"
        assert is_fresh is True
        assert reset is False
        assert meta.current_mode_id == "ask"
        assert meta.current_model_id == "model-a"
        # Re-ensuring same session returns cached without reset
        meta2, is_fresh2, reset2 = c1.ensure_session("sess-1")
        assert meta2.session_id == "sess-1"
        assert is_fresh2 is False
        assert reset2 is False
    finally:
        c1.close()

    # 2. configOptions schema
    c2 = AcpClient(
        _make_config(mock_server_script, mode="config_options"), str(tmp_path)
    )
    try:
        c2.start()
        meta, is_fresh, reset = c2.ensure_session()
        assert meta.session_id == "sess-cfg-1"
        assert meta.mode_config_id == "cfg-mode"
        assert meta.model_config_id == "cfg-model"
        assert [m.model_id for m in meta.models] == ["model-a", "model-b"]
    finally:
        c2.close()

    # 3. session/load failure falls back to session/new with context_was_reset=True
    c3 = AcpClient(_make_config(mock_server_script, mode="load_fail"), str(tmp_path))
    try:
        c3.start()
        assert c3.supports_load_session is True
        meta, is_fresh, reset = c3.ensure_session("stale-session-id")
        assert meta.session_id == "sess-1"
        assert is_fresh is True
        assert reset is True
    finally:
        c3.close()

    # 4. session/load success preserves session without reset
    c4 = AcpClient(_make_config(mock_server_script, mode="load_ok"), str(tmp_path))
    try:
        c4.start()
        assert c4.supports_load_session is True
        meta, is_fresh, reset = c4.ensure_session("existing-session-id")
        assert meta.session_id == "existing-session-id"
        assert is_fresh is False
        assert reset is False
        assert meta.current_mode_id == "code"
        assert meta.current_model_id == "model-b"
    finally:
        c4.close()


def test_set_mode_and_set_model_legacy_and_config_options(
    mock_server_script: Path, tmp_path: Path
):
    # Legacy set_mode / set_model + current_mode_update notification
    c1 = AcpClient(_make_config(mock_server_script, mode="normal"), str(tmp_path))
    try:
        c1.start()
        meta, _, _ = c1.ensure_session()
        updated = c1.set_mode(meta.session_id, "code")
        assert updated.current_mode_id == "code"
        updated2 = c1.set_model(meta.session_id, "model-b")
        assert updated2.current_model_id == "model-b"
    finally:
        c1.close()

    # configOptions set_config_option
    c2 = AcpClient(
        _make_config(mock_server_script, mode="config_options"), str(tmp_path)
    )
    try:
        c2.start()
        meta, _, _ = c2.ensure_session()
        updated = c2.set_mode(meta.session_id, "code")
        assert updated.current_mode_id == "code"
        updated2 = c2.set_model(meta.session_id, "model-b")
        assert updated2.current_model_id == "model-b"
    finally:
        c2.close()


def test_prompt_streaming_updates(mock_server_script: Path, tmp_path: Path):
    client = AcpClient(_make_config(mock_server_script), str(tmp_path))
    events: list[AcpUpdateEvent] = []
    try:
        client.start()
        meta, _, _ = client.ensure_session()
        res = client.prompt(
            meta.session_id,
            "say hello",
            on_event=events.append,
            on_permission=lambda _req: None,
            cancel_event=threading.Event(),
        )
        assert res.get("stopReason") == "end_turn"
    finally:
        client.close()

    texts = "".join(e.text for e in events if e.kind == "text")
    thoughts = "".join(e.text for e in events if e.kind == "thought")
    statuses = [e for e in events if e.kind == "status"]
    assert texts == "Hello world!"
    assert thoughts == "thinking..."
    assert any("Inspect code" in e.text for e in statuses)
    assert any(e.tool_call_id == "tc-1" for e in statuses)


def test_permission_allow_deny_and_cancel_unblocks_reader(
    mock_server_script: Path, tmp_path: Path
):
    client = AcpClient(_make_config(mock_server_script), str(tmp_path))
    try:
        client.start()
        meta, _, _ = client.ensure_session()

        # 1. Allow: also verify status update arrives WHILE permission callback is blocked
        status_arrived = threading.Event()
        events_allow: list[AcpUpdateEvent] = []

        def on_event_allow(ev: AcpUpdateEvent):
            events_allow.append(ev)
            if ev.kind == "status" and ev.tool_call_id == "tc-99":
                status_arrived.set()

        def on_perm_allow(req: AcpPermissionRequest) -> str | None:
            # Wait for the follow-up status notification to prove the stdout reader
            # thread is NOT blocked while we wait for user input!
            assert status_arrived.wait(timeout=5.0), "stdout reader was blocked by permission callback"
            return req.options[0].option_id

        res_allow = client.prompt(
            meta.session_id,
            "ask_permission allow",
            on_event=on_event_allow,
            on_permission=on_perm_allow,
            cancel_event=threading.Event(),
        )
        assert res_allow.get("stopReason") == "end_turn"
        assert "perm:allow_once" in "".join(
            e.text for e in events_allow if e.kind == "text"
        )

        # 2. Deny (select reject option)
        events_deny: list[AcpUpdateEvent] = []
        res_deny = client.prompt(
            meta.session_id,
            "ask_permission deny",
            on_event=events_deny.append,
            on_permission=lambda req: req.options[1].option_id,
            cancel_event=threading.Event(),
        )
        assert res_deny.get("stopReason") == "end_turn"
        assert "perm:reject_once" in "".join(
            e.text for e in events_deny if e.kind == "text"
        )

        # 3. cancel_event unblocks a stuck permission callback and sends session/cancel
        perm_entered = threading.Event()
        release_stuck = threading.Event()
        cancel_ev = threading.Event()

        def on_perm_stuck(_req: AcpPermissionRequest) -> str | None:
            perm_entered.set()
            release_stuck.wait(timeout=10.0)
            return "allow_once"

        def trigger_cancel():
            assert perm_entered.wait(timeout=5.0)
            cancel_ev.set()

        canceller = threading.Thread(target=trigger_cancel, daemon=True)
        canceller.start()
        try:
            t0 = time.monotonic()
            res_cancel = client.prompt(
                meta.session_id,
                "ask_permission cancel",
                on_event=lambda _ev: None,
                on_permission=on_perm_stuck,
                cancel_event=cancel_ev,
            )
            elapsed = time.monotonic() - t0
            assert res_cancel.get("stopReason") == "cancelled"
            assert res_cancel.get("cancelledSeen") is True
            assert elapsed < 5.0
        finally:
            release_stuck.set()
            canceller.join(timeout=2.0)
    finally:
        client.close()


def test_noisy_stdout_and_bounded_stderr(mock_server_script: Path, tmp_path: Path):
    client = AcpClient(
        _make_config(mock_server_script, mode="noisy_banner"), str(tmp_path)
    )
    try:
        client.start()
        meta, _, _ = client.ensure_session()
        assert meta.session_id == "sess-1"
        # Wait briefly for stderr thread to drain all 300 lines
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            tail = client._transport.stderr_tail(max_lines=250)  # noqa: SLF001
            if "stderr line 299" in tail:
                break
            time.sleep(0.02)
        all_retained = client._transport.stderr_tail(max_lines=500).splitlines()  # noqa: SLF001
        assert len(all_retained) <= 200
        assert all_retained[-1] == "stderr line 299"
    finally:
        client.close()


def test_crash_mid_turn_raises_acp_error_with_exit_code_and_stderr(
    mock_server_script: Path, tmp_path: Path
):
    client = AcpClient(
        _make_config(mock_server_script, mode="crash_mid_turn"), str(tmp_path)
    )
    try:
        client.start()
        meta, _, _ = client.ensure_session()
        with pytest.raises(AcpError) as exc_info:
            client.prompt(
                meta.session_id,
                "trigger crash",
                on_event=lambda _ev: None,
                on_permission=lambda _req: None,
                cancel_event=threading.Event(),
            )
        err = exc_info.value
        assert "42" in str(err) or err.code == 42
        assert "fatal: agent segfaulted" in err.stderr_tail
    finally:
        client.close()


def test_startup_timeout_kills_hung_process(mock_server_script: Path, tmp_path: Path):
    pid_file = tmp_path / "hung.pid"
    cfg = _make_config(
        mock_server_script,
        mode="hang_init",
        startup_timeout=1.0,
        extra_env={"MOCK_ACP_PID_FILE": str(pid_file)},
    )
    client = AcpClient(cfg, str(tmp_path))
    with pytest.raises(AcpError):
        client.start()

    assert not client.is_alive
    if pid_file.exists():
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_is_alive(pid):
            time.sleep(0.05)
        assert not _pid_is_alive(pid)


def test_close_leaves_zero_live_child_pids(mock_server_script: Path, tmp_path: Path):
    pid_file = tmp_path / "child.pid"
    cfg = _make_config(
        mock_server_script,
        mode="normal",
        extra_env={"MOCK_ACP_PID_FILE": str(pid_file)},
    )
    client = AcpClient(cfg, str(tmp_path))
    client.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not pid_file.exists():
        time.sleep(0.02)
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    assert _pid_is_alive(pid)

    client.close()
    assert not client.is_alive

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_is_alive(pid):
        time.sleep(0.05)
    assert not _pid_is_alive(pid)
