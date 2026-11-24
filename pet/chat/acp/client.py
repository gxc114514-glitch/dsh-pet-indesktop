from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Callable

from .presets import augmented_env
from .protocol import (
    AcpError,
    AcpPermissionRequest,
    AcpSessionMeta,
    AcpUpdateEvent,
    _apply_config_options,
    format_permission_response,
    parse_permission_request,
    parse_session_meta,
    parse_session_update,
)
from .transport import StdioJsonRpcTransport

if TYPE_CHECKING:
    from pet.chat.models import AcpAgentConfig

log = logging.getLogger(__name__)


class AcpClient:
    """High-level pure-Python client for a single local ACP Coding Agent process."""

    def __init__(self, config: AcpAgentConfig, workspace_dir: str) -> None:
        self._config = config
        self._workspace_dir = str(
            workspace_dir or config.default_cwd or os.getcwd()
        )
        self._transport: StdioJsonRpcTransport | None = None

        self._agent_name: str = ""
        self._agent_version: str = ""
        self._supports_load_session: bool = False
        self._last_activity_at: float = time.monotonic()

        self._state_lock = threading.Lock()
        self._sessions: dict[str, AcpSessionMeta] = {}

        # Active prompt turn state (protected by _turn_lock)
        self._turn_lock = threading.Lock()
        self._active_session_id: str | None = None
        self._active_on_event: Callable[[AcpUpdateEvent], None] | None = None
        self._active_on_permission: (
            Callable[[AcpPermissionRequest], str | None] | None
        ) = None
        self._active_cancel_event: threading.Event | None = None
        self._turn_cond: threading.Condition | None = None

    @property
    def is_alive(self) -> bool:
        t = self._transport
        return t is not None and t.is_alive

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def agent_version(self) -> str:
        return self._agent_version

    @property
    def supports_load_session(self) -> bool:
        return self._supports_load_session

    @property
    def last_activity_at(self) -> float:
        return self._last_activity_at

    def _touch(self) -> None:
        self._last_activity_at = time.monotonic()

    def _require_transport(self) -> StdioJsonRpcTransport:
        t = self._transport
        if t is None or not t.is_alive:
            raise AcpError("ACP client is not running")
        return t

    def start(self) -> dict[str, Any]:
        if not self._config.command:
            raise AcpError("ACP agent command is empty")

        env = augmented_env(
            self._config.env,
            executable=self._config.command,
        )
        transport = StdioJsonRpcTransport(
            command=self._config.command,
            args=list(self._config.args),
            cwd=self._workspace_dir,
            env=env,
            on_notification=self._on_notification,
            on_request=self._on_request,
            on_close=self._on_close,
        )
        self._transport = transport
        try:
            transport.start()
            result = transport.send_request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {
                            "readTextFile": False,
                            "writeTextFile": False,
                        },
                        "terminal": False,
                    },
                    "clientInfo": {
                        "name": "dsh-pet-indesktop",
                        "version": "1.0.0",
                    },
                },
                timeout=float(self._config.startup_timeout),
            )
        except Exception:
            self.close()
            raise

        agent_info = result.get("agentInfo")
        if isinstance(agent_info, dict):
            self._agent_name = str(
                agent_info.get("name") or agent_info.get("title") or ""
            )
            self._agent_version = str(agent_info.get("version") or "")

        agent_caps = result.get("agentCapabilities")
        if isinstance(agent_caps, dict):
            self._supports_load_session = bool(
                agent_caps.get("loadSession", False)
            )
        else:
            self._supports_load_session = False

        self._touch()
        return result

    def _create_new_session(self) -> AcpSessionMeta:
        transport = self._require_transport()
        result = transport.send_request(
            "session/new",
            {
                "cwd": self._workspace_dir,
                "mcpServers": [],
            },
            timeout=float(self._config.startup_timeout),
        )
        sid = str(result.get("sessionId") or "")
        meta = parse_session_meta(sid, result)
        with self._state_lock:
            self._sessions[meta.session_id] = meta
        self._touch()
        return meta

    def ensure_session(
        self, acp_session_id: str = ""
    ) -> tuple[AcpSessionMeta, bool, bool]:
        """Ensure an active ACP session exists.

        Returns ``(meta, is_fresh_new, context_was_reset)``.
        """
        transport = self._require_transport()
        sid = str(acp_session_id or "").strip()

        if sid:
            with self._state_lock:
                existing = self._sessions.get(sid)
            if existing is not None:
                self._touch()
                return (existing, False, False)

            if self._supports_load_session:
                try:
                    result = transport.send_request(
                        "session/load",
                        {
                            "sessionId": sid,
                            "cwd": self._workspace_dir,
                            "mcpServers": [],
                        },
                        timeout=float(self._config.startup_timeout),
                    )
                    meta = parse_session_meta(sid, result)
                    with self._state_lock:
                        self._sessions[sid] = meta
                    self._touch()
                    return (meta, False, False)
                except AcpError as exc:
                    log.debug(
                        "ACP session/load failed for %s (%s); falling back to session/new",
                        sid,
                        exc,
                    )
                    if not self.is_alive:
                        raise
                    meta = self._create_new_session()
                    return (meta, True, True)

            meta = self._create_new_session()
            return (meta, True, True)

        meta = self._create_new_session()
        return (meta, True, False)

    def set_mode(self, session_id: str, mode_id: str) -> AcpSessionMeta:
        transport = self._require_transport()
        with self._state_lock:
            current = self._sessions.get(session_id) or AcpSessionMeta(
                session_id=session_id
            )

        if current.mode_config_id:
            result = transport.send_request(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": current.mode_config_id,
                    "value": mode_id,
                },
                timeout=float(self._config.startup_timeout),
            )
            with self._state_lock:
                base = self._sessions.get(session_id) or current
                cfg_opts = result.get("configOptions")
                if isinstance(cfg_opts, list):
                    updated = _apply_config_options(base, cfg_opts)
                else:
                    updated = replace(base, current_mode_id=mode_id)
                self._sessions[session_id] = updated
        else:
            result = transport.send_request(
                "session/set_mode",
                {
                    "sessionId": session_id,
                    "modeId": mode_id,
                },
                timeout=float(self._config.startup_timeout),
            )
            with self._state_lock:
                base = self._sessions.get(session_id) or current
                if isinstance(result.get("modes"), dict):
                    parsed = parse_session_meta(session_id, result)
                    updated = replace(
                        base,
                        modes=parsed.modes or base.modes,
                        current_mode_id=parsed.current_mode_id or mode_id,
                    )
                else:
                    updated = replace(base, current_mode_id=mode_id)
                self._sessions[session_id] = updated

        self._touch()
        return updated

    def set_model(self, session_id: str, model_id: str) -> AcpSessionMeta:
        transport = self._require_transport()
        with self._state_lock:
            current = self._sessions.get(session_id) or AcpSessionMeta(
                session_id=session_id
            )

        if current.model_config_id:
            result = transport.send_request(
                "session/set_config_option",
                {
                    "sessionId": session_id,
                    "configId": current.model_config_id,
                    "value": model_id,
                },
                timeout=float(self._config.startup_timeout),
            )
            with self._state_lock:
                base = self._sessions.get(session_id) or current
                cfg_opts = result.get("configOptions")
                if isinstance(cfg_opts, list):
                    updated = _apply_config_options(base, cfg_opts)
                else:
                    updated = replace(base, current_model_id=model_id)
                self._sessions[session_id] = updated
        else:
            result = transport.send_request(
                "session/set_model",
                {
                    "sessionId": session_id,
                    "modelId": model_id,
                },
                timeout=float(self._config.startup_timeout),
            )
            with self._state_lock:
                base = self._sessions.get(session_id) or current
                if isinstance(result.get("models"), dict):
                    parsed = parse_session_meta(session_id, result)
                    updated = replace(
                        base,
                        models=parsed.models or base.models,
                        current_model_id=parsed.current_model_id or model_id,
                    )
                else:
                    updated = replace(base, current_model_id=model_id)
                self._sessions[session_id] = updated

        self._touch()
        return updated

    def cancel(self, session_id: str) -> None:
        t = self._transport
        if t is None or not t.is_alive:
            return
        try:
            t.send_notification("session/cancel", {"sessionId": session_id})
        except AcpError:
            pass
        self._touch()

    def prompt(
        self,
        session_id: str,
        text: str,
        on_event: Callable[[AcpUpdateEvent], None] | None,
        on_permission: Callable[[AcpPermissionRequest], str | None] | None,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        transport = self._require_transport()
        self._touch()

        turn_cond = threading.Condition()
        turn_done = threading.Event()
        cancel_lock = threading.Lock()
        cancel_sent = [False]

        def _send_cancel_once() -> None:
            with cancel_lock:
                if cancel_sent[0] or turn_done.is_set():
                    return
                cancel_sent[0] = True
            self.cancel(session_id)

        with self._turn_lock:
            self._active_session_id = session_id
            self._active_on_event = on_event
            self._active_on_permission = on_permission
            self._active_cancel_event = cancel_event
            self._active_send_cancel_once = _send_cancel_once
            self._turn_cond = turn_cond

        def _watch_cancel() -> None:
            while not turn_done.is_set():
                if cancel_event.wait(timeout=0.02):
                    if not turn_done.is_set():
                        _send_cancel_once()
                        with turn_cond:
                            turn_cond.notify_all()
                    break

        watcher = threading.Thread(
            target=_watch_cancel,
            name="acp-cancel-watcher",
            daemon=True,
        )
        watcher.start()

        try:
            return transport.send_request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=None,
            )
        finally:
            turn_done.set()
            with turn_cond:
                turn_cond.notify_all()
            watcher.join(timeout=1.0)
            with self._turn_lock:
                self._active_session_id = None
                self._active_on_event = None
                self._active_on_permission = None
                self._active_cancel_event = None
                self._active_send_cancel_once = None
                self._turn_cond = None
            self._touch()

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        self._touch()
        if method != "session/update":
            return

        with self._turn_lock:
            active_sid = self._active_session_id
            on_event = self._active_on_event

        sid = str(params.get("sessionId") or active_sid or "")
        with self._state_lock:
            current_meta = self._sessions.get(sid)

        events = parse_session_update(params, current_meta=current_meta)
        for ev in events:
            if ev.kind == "meta" and ev.session_meta is not None:
                target_sid = ev.session_meta.session_id or sid
                if target_sid:
                    with self._state_lock:
                        self._sessions[target_sid] = ev.session_meta
            if on_event is not None:
                try:
                    on_event(ev)
                except Exception:
                    log.debug("ACP on_event callback raised", exc_info=True)

    def _on_request(
        self, rpc_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        self._touch()
        transport = self._transport
        if transport is None:
            return

        if method != "session/request_permission":
            try:
                transport.send_error(
                    rpc_id, -32601, f"Unsupported client method: {method}"
                )
            except AcpError:
                pass
            return

        req = parse_permission_request(rpc_id, params)
        with self._turn_lock:
            on_permission = self._active_on_permission
            cancel_event = self._active_cancel_event
            send_cancel_once = getattr(self, "_active_send_cancel_once", None)
            turn_cond = self._turn_cond

        if on_permission is None or (
            cancel_event is not None and cancel_event.is_set()
        ):
            if cancel_event is not None and cancel_event.is_set() and callable(send_cancel_once):
                send_cancel_once()
            try:
                transport.send_response(rpc_id, format_permission_response(None))
            except AcpError:
                pass
            return

        cond = turn_cond if turn_cond is not None else threading.Condition()
        perm_done = [False]
        perm_choice: list[str | None] = [None]

        def _run_permission_cb() -> None:
            try:
                perm_choice[0] = on_permission(req)
            except Exception:
                log.debug("ACP on_permission callback raised", exc_info=True)
                perm_choice[0] = None
            finally:
                with cond:
                    perm_done[0] = True
                    cond.notify_all()

        cb_thread = threading.Thread(
            target=_run_permission_cb,
            name="acp-permission-cb",
            daemon=True,
        )
        cb_thread.start()

        with cond:
            cond.wait_for(
                lambda: perm_done[0]
                or (cancel_event is not None and cancel_event.is_set())
                or not self.is_alive
            )

        cancelled = cancel_event is not None and cancel_event.is_set()
        if cancelled and callable(send_cancel_once):
            send_cancel_once()
        option_id = None if (cancelled or not perm_done[0]) else perm_choice[0]
        try:
            transport.send_response(
                rpc_id, format_permission_response(option_id)
            )
        except AcpError:
            pass

    def _on_close(self, _returncode: int | None, _stderr_tail: str) -> None:
        with self._turn_lock:
            cond = self._turn_cond
        if cond is not None:
            with cond:
                cond.notify_all()

    def close(self) -> None:
        t = self._transport
        if t is not None:
            t.close()
        with self._turn_lock:
            cond = self._turn_cond
        if cond is not None:
            with cond:
                cond.notify_all()
