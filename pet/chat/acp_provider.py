from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any

from .acp.client import AcpClient
from .acp.manager import AcpManager, get_acp_manager
from .acp.protocol import (
    AcpError,
    AcpPermissionRequest,
    AcpSessionMeta,
    AcpUpdateEvent,
)
from .models import AcpAgentConfig, ChatSession, ProviderConfig
from .providers import StreamEvent

log = logging.getLogger(__name__)

CONTEXT_RESET_NOTICE = "已开启新的 Agent 上下文（此前记录仅作本地展示）"
EMPTY_TOOL_COMPLETION_TEXT = "已完成操作。"


def format_acp_error(exc: Exception) -> str:
    """Format an ACP exception for user-visible error reporting, including stderr tail."""
    msg = str(exc) or type(exc).__name__
    tail = getattr(exc, "stderr_tail", "")
    if isinstance(tail, str) and tail.strip() and tail.strip() not in msg:
        return f"{msg}\n{tail.strip()}"
    return msg


def _resolve_workspace(config: ProviderConfig, session: ChatSession | None) -> str:
    ws = ""
    if session is not None and getattr(session, "workspace_dir", ""):
        ws = str(session.workspace_dir).strip()
    if not ws and config.acp is not None and config.acp.default_cwd:
        ws = str(config.acp.default_cwd).strip()
    if not ws:
        ws = os.getcwd()
    return os.path.abspath(ws)


def _sync_session_from_meta(session: ChatSession | None, meta: AcpSessionMeta) -> None:
    if session is None:
        return
    if meta.session_id:
        session.acp_session_id = meta.session_id
    if meta.current_mode_id:
        session.acp_mode_id = meta.current_mode_id
    if meta.current_model_id:
        session.acp_model_id = meta.current_model_id


class AcpProvider:
    """Chat provider bridging ``ChatService`` to a local ACP Coding Agent via ``AcpManager``."""

    def __init__(self, manager: AcpManager | None = None) -> None:
        self._manager: AcpManager | None = manager
        self._tone_lock = threading.Lock()
        # Tracks newly created (id(client), acp_session_id) pairs that have not yet
        # received their first user prompt (e.g. after eager prepare_session()).
        self._pending_tone_sessions: set[tuple[int, str]] = set()

    @property
    def manager(self) -> AcpManager:
        if self._manager is None:
            self._manager = get_acp_manager()
        return self._manager

    def _require_acp_config(self, config: ProviderConfig) -> AcpAgentConfig:
        if not getattr(config, "is_acp", False) or config.acp is None:
            raise AcpError("当前 Provider 未配置 ACP Agent。")
        return config.acp

    def _apply_initial_preferences(
        self,
        client: AcpClient,
        meta: AcpSessionMeta,
        acp_cfg: AcpAgentConfig,
        session: ChatSession | None,
    ) -> AcpSessionMeta:
        desired_mode = (
            (session.acp_mode_id if session is not None else "") or acp_cfg.default_mode_id
        ).strip()
        if desired_mode and desired_mode != meta.current_mode_id:
            valid_modes = {m.mode_id for m in meta.modes}
            if not valid_modes or desired_mode in valid_modes:
                try:
                    meta = client.set_mode(meta.session_id, desired_mode)
                except AcpError as exc:
                    if not client.is_alive:
                        raise
                    log.debug("Failed to apply initial ACP mode %s: %s", desired_mode, exc)

        desired_model = (
            (session.acp_model_id if session is not None else "") or acp_cfg.default_model_id
        ).strip()
        if desired_model and desired_model != meta.current_model_id:
            valid_models = {m.model_id for m in meta.models}
            if not valid_models or desired_model in valid_models:
                try:
                    meta = client.set_model(meta.session_id, desired_model)
                except AcpError as exc:
                    if not client.is_alive:
                        raise
                    log.debug("Failed to apply initial ACP model %s: %s", desired_model, exc)

        return meta

    def _ensure_session(
        self,
        client: AcpClient,
        acp_cfg: AcpAgentConfig,
        session: ChatSession | None,
        *,
        mark_pending_tone_if_fresh: bool,
    ) -> tuple[AcpSessionMeta, bool, bool]:
        existing_sid = session.acp_session_id if session is not None else ""
        meta, is_fresh_new, context_was_reset = client.ensure_session(existing_sid)
        if is_fresh_new:
            meta = self._apply_initial_preferences(client, meta, acp_cfg, session)
            if mark_pending_tone_if_fresh and not context_was_reset and meta.session_id:
                with self._tone_lock:
                    self._pending_tone_sessions.add((id(client), meta.session_id))
        _sync_session_from_meta(session, meta)
        return (meta, is_fresh_new, context_was_reset)

    def prepare_session(
        self,
        config: ProviderConfig,
        session: ChatSession,
    ) -> tuple[AcpSessionMeta, bool]:
        acp_cfg = self._require_acp_config(config)
        ws = _resolve_workspace(config, session)
        client = self.manager.acquire(config.provider_id, acp_cfg, ws)
        try:
            meta, _is_fresh, context_was_reset = self._ensure_session(
                client, acp_cfg, session, mark_pending_tone_if_fresh=True
            )
            return (meta, context_was_reset)
        except Exception:
            if not client.is_alive:
                self.manager.evict(config.provider_id, ws)
            raise

    def set_mode(
        self,
        config: ProviderConfig,
        session: ChatSession,
        mode_id: str,
    ) -> AcpSessionMeta:
        acp_cfg = self._require_acp_config(config)
        ws = _resolve_workspace(config, session)
        client = self.manager.acquire(config.provider_id, acp_cfg, ws)
        try:
            meta, _is_fresh, _reset = self._ensure_session(
                client, acp_cfg, session, mark_pending_tone_if_fresh=True
            )
            updated = client.set_mode(meta.session_id, mode_id)
            _sync_session_from_meta(session, updated)
            return updated
        except Exception:
            if not client.is_alive:
                self.manager.evict(config.provider_id, ws)
            raise

    def set_model(
        self,
        config: ProviderConfig,
        session: ChatSession,
        model_id: str,
    ) -> AcpSessionMeta:
        acp_cfg = self._require_acp_config(config)
        ws = _resolve_workspace(config, session)
        client = self.manager.acquire(config.provider_id, acp_cfg, ws)
        try:
            meta, _is_fresh, _reset = self._ensure_session(
                client, acp_cfg, session, mark_pending_tone_if_fresh=True
            )
            updated = client.set_model(meta.session_id, model_id)
            _sync_session_from_meta(session, updated)
            return updated
        except Exception:
            if not client.is_alive:
                self.manager.evict(config.provider_id, ws)
            raise

    def stream_turn(
        self,
        user_text: str,
        config: ProviderConfig,
        session: ChatSession,
        cancel_event: threading.Event,
        on_permission: Callable[[AcpPermissionRequest], str | None] | None = None,
        on_meta: Callable[[AcpSessionMeta], None] | None = None,
        on_context_reset: Callable[[str], None] | None = None,
    ) -> Iterator[str | StreamEvent]:
        acp_cfg = self._require_acp_config(config)
        ws = _resolve_workspace(config, session)
        mgr = self.manager
        key = mgr.make_key(config.provider_id, ws, acp_cfg)
        client = mgr.acquire(config.provider_id, acp_cfg, ws)
        mgr.mark_busy(key, True)

        try:
            meta, is_fresh_new, context_was_reset = self._ensure_session(
                client, acp_cfg, session, mark_pending_tone_if_fresh=False
            )
            if context_was_reset and on_context_reset is not None:
                on_context_reset(CONTEXT_RESET_NOTICE)
            if on_meta is not None:
                on_meta(meta)

            if cancel_event.is_set():
                return

            session_key = (id(client), meta.session_id)
            with self._tone_lock:
                was_pending = session_key in self._pending_tone_sessions
                self._pending_tone_sessions.discard(session_key)
            need_tone = not context_was_reset and (is_fresh_new or was_pending)

            tone = (acp_cfg.tone_prompt or "").strip()
            if need_tone and acp_cfg.tone_enabled and tone:
                prompt_text = f"{tone}\n\n{user_text}"
            else:
                prompt_text = user_text

            ev_queue: queue.Queue[Any] = queue.Queue()
            sentinel = object()
            prompt_result: list[dict[str, Any] | None] = [None]
            prompt_error: list[BaseException | None] = [None]

            def _on_event(ev: AcpUpdateEvent) -> None:
                ev_queue.put(ev)

            def _run_prompt() -> None:
                try:
                    prompt_result[0] = client.prompt(
                        meta.session_id,
                        prompt_text,
                        on_event=_on_event,
                        on_permission=on_permission,
                        cancel_event=cancel_event,
                    )
                except BaseException as exc:
                    prompt_error[0] = exc
                finally:
                    ev_queue.put(sentinel)

            prompt_thread = threading.Thread(
                target=_run_prompt,
                name="acp-prompt-runner",
                daemon=True,
            )
            prompt_thread.start()

            has_text = False
            has_tool_activity = False

            while True:
                try:
                    item = ev_queue.get(timeout=0.05)
                except queue.Empty:
                    if not prompt_thread.is_alive():
                        break
                    continue

                if item is sentinel:
                    break

                if not isinstance(item, AcpUpdateEvent):
                    continue

                if item.kind == "text":
                    if item.text:
                        has_text = True
                        yield item.text
                elif item.kind == "thought":
                    if item.text:
                        yield StreamEvent("reasoning", item.text)
                elif item.kind == "status":
                    has_tool_activity = True
                    if item.text:
                        yield StreamEvent("status", item.text)
                elif item.kind == "meta" and item.session_meta is not None:
                    _sync_session_from_meta(session, item.session_meta)
                    if on_meta is not None:
                        on_meta(item.session_meta)

            prompt_thread.join(timeout=2.0)

            if prompt_error[0] is not None:
                exc = prompt_error[0]
                if not client.is_alive:
                    mgr.evict(config.provider_id, ws)
                raise exc

            if not cancel_event.is_set() and isinstance(prompt_result[0], dict):
                stop_reason = str(prompt_result[0].get("stopReason") or "")
                if stop_reason == "end_turn" and has_tool_activity and not has_text:
                    yield EMPTY_TOOL_COMPLETION_TEXT
        except Exception:
            if not client.is_alive:
                mgr.evict(config.provider_id, ws)
            raise
        finally:
            mgr.mark_busy(key, False)
