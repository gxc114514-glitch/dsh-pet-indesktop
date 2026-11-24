from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .client import AcpClient
from .protocol import AcpError

if TYPE_CHECKING:
    from pet.chat.models import AcpAgentConfig, ProviderConfig

log = logging.getLogger(__name__)


def _normalize_workspace(workspace_dir: str, config: AcpAgentConfig | None = None) -> str:
    raw = str(workspace_dir or "").strip()
    if not raw and config is not None:
        raw = str(config.default_cwd or "").strip()
    if not raw:
        raw = os.getcwd()
    return os.path.abspath(raw)


def _launch_fingerprint(config: AcpAgentConfig) -> tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]]:
    return (
        str(config.command or ""),
        tuple(str(a) for a in (config.args or ())),
        tuple(sorted((str(k), str(v)) for k, v in (config.env or {}).items())),
    )


@dataclass
class _ManagedClient:
    key: tuple[str, str]
    client: AcpClient
    config: AcpAgentConfig
    fingerprint: tuple[str, tuple[str, ...], tuple[tuple[str, str], ...]]
    busy: bool = False


class AcpManager:
    """Process-level registry of live local ACP Coding Agent clients.

    Keyed by ``(provider_id, normalized_workspace_dir)`` so closing and reopening
    the chat window reuses warm agent processes instead of respawning them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str], _ManagedClient] = {}

    def make_key(
        self,
        provider_id: str,
        workspace_dir: str,
        config: AcpAgentConfig | None = None,
    ) -> tuple[str, str]:
        return (str(provider_id), _normalize_workspace(workspace_dir, config))

    def acquire(
        self,
        provider_id: str,
        config: AcpAgentConfig,
        workspace_dir: str,
    ) -> AcpClient:
        ws = _normalize_workspace(workspace_dir, config)
        key = (str(provider_id), ws)
        fp = _launch_fingerprint(config)
        stale_client: AcpClient | None = None
        conflict_message = (
            "ACP client is busy; cannot acquire it with a different launch configuration."
        )

        with self._lock:
            entry = self._clients.get(key)
            if entry is not None:
                if entry.client.is_alive and entry.fingerprint == fp:
                    entry.config = config
                    return entry.client
                if entry.busy and entry.client.is_alive:
                    raise AcpError(conflict_message)
                if not entry.busy or not entry.client.is_alive:
                    self._clients.pop(key, None)
                    stale_client = entry.client

        if stale_client is not None:
            try:
                stale_client.close()
            except Exception:
                log.debug("Failed to close stale ACP client %s", key, exc_info=True)

        client = AcpClient(config, ws)
        client.start()

        redundant: AcpClient | None = None
        conflicting_busy_client = False
        with self._lock:
            existing = self._clients.get(key)
            if existing is not None and existing.client.is_alive and existing.fingerprint == fp:
                existing.config = config
                redundant = client
                result = existing.client
            elif existing is not None and existing.busy and existing.client.is_alive:
                redundant = client
                result = existing.client
                conflicting_busy_client = True
            else:
                if existing is not None:
                    redundant = existing.client
                self._clients[key] = _ManagedClient(
                    key=key,
                    client=client,
                    config=config,
                    fingerprint=fp,
                    busy=False,
                )
                result = client

        if redundant is not None:
            try:
                redundant.close()
            except Exception:
                log.debug("Failed to close redundant ACP client %s", key, exc_info=True)

        if conflicting_busy_client:
            raise AcpError(conflict_message)
        return result

    def evict(self, provider_id: str, workspace_dir: str) -> None:
        raw_key = (str(provider_id), str(workspace_dir or ""))
        norm_key = (str(provider_id), _normalize_workspace(workspace_dir))
        removed: list[AcpClient] = []

        with self._lock:
            for k in {raw_key, norm_key}:
                entry = self._clients.pop(k, None)
                if entry is not None:
                    removed.append(entry.client)

        for client in removed:
            try:
                client.close()
            except Exception:
                log.debug("Failed to close evicted ACP client", exc_info=True)

    def mark_busy(self, key: tuple[str, str], busy: bool) -> None:
        pid, ws = str(key[0]), str(key[1])
        norm_key = (pid, _normalize_workspace(ws))
        with self._lock:
            entry = self._clients.get((pid, ws)) or self._clients.get(norm_key)
            if entry is not None:
                entry.busy = bool(busy)

    def reap_idle(self, now: float | None = None) -> int:
        current = time.monotonic() if now is None else float(now)
        to_close: list[AcpClient] = []

        with self._lock:
            for key, entry in list(self._clients.items()):
                if entry.busy:
                    continue
                if not entry.client.is_alive:
                    self._clients.pop(key, None)
                    to_close.append(entry.client)
                    continue
                timeout_min = int(getattr(entry.config, "idle_timeout_minutes", 0) or 0)
                if timeout_min <= 0:
                    continue
                idle_seconds = current - entry.client.last_activity_at
                if idle_seconds >= timeout_min * 60.0:
                    self._clients.pop(key, None)
                    to_close.append(entry.client)

        for client in to_close:
            try:
                client.close()
            except Exception:
                log.debug("Failed to close idle ACP client", exc_info=True)

        return len(to_close)

    def shutdown_all(self) -> None:
        with self._lock:
            entries = list(self._clients.values())
            self._clients.clear()

        for entry in entries:
            try:
                entry.client.close()
            except Exception:
                log.debug("Failed to close ACP client %s on shutdown", entry.key, exc_info=True)

    def probe_connection(self, config: ProviderConfig) -> tuple[bool, str]:
        if not getattr(config, "is_acp", False) or config.acp is None:
            return (False, "当前 Provider 不是 ACP Agent 配置。")
        acp_cfg = config.acp
        if not str(acp_cfg.command or "").strip():
            return (False, "未配置 ACP Agent 启动命令。")

        ws = _normalize_workspace(acp_cfg.default_cwd, acp_cfg)
        client = AcpClient(acp_cfg, ws)
        try:
            client.start()
            name = client.agent_name or config.name or acp_cfg.command
            ver = client.agent_version
            label = f"{name} {ver}".strip()
            return (True, f"连接成功：{label}")
        except AcpError as exc:
            msg = str(exc) or "ACP 连接失败"
            if exc.stderr_tail and exc.stderr_tail.strip():
                msg = f"{msg}\n{exc.stderr_tail.strip()}"
            return (False, msg)
        except Exception as exc:
            return (False, str(exc) or type(exc).__name__)
        finally:
            try:
                client.close()
            except Exception:
                pass


_manager_lock = threading.Lock()
_manager_singleton: AcpManager | None = None
_quit_hook_connected = False


def get_acp_manager() -> AcpManager:
    """Return the process-wide ``AcpManager`` singleton.

    Lazily wires ``QApplication.aboutToQuit`` when a Qt application exists;
    safe to call in pure-Python contexts without importing PySide6 at module load.
    """
    global _manager_singleton, _quit_hook_connected
    with _manager_lock:
        if _manager_singleton is None:
            _manager_singleton = AcpManager()
        manager = _manager_singleton
        if not _quit_hook_connected:
            try:
                from PySide6.QtWidgets import QApplication

                app = QApplication.instance()
                if app is not None:
                    app.aboutToQuit.connect(manager.shutdown_all)
                    _quit_hook_connected = True
            except Exception:
                pass
        return manager
