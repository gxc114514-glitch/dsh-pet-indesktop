from __future__ import annotations
import logging, threading, uuid
from typing import Any
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QApplication
from .acp_provider import CONTEXT_RESET_NOTICE, AcpProvider, format_acp_error
from .models import ChatSession, ProviderConfig
from .providers import OpenAICompatibleProvider, StreamEvent

log = logging.getLogger(__name__)
class _Worker(QThread):
    delta_received=Signal(str); completed=Signal(str); failed=Signal(str); stopped_by_user=Signal()
    # 非正文事件：(kind, text)，kind 为 'reasoning'（思考过程）或 'status'（联网进度）
    activity_received=Signal(str,str)
    def __init__(self,provider,messages,config,cancel):
        super().__init__()
        self.provider=provider; self.messages=messages; self.config=config; self.cancel=cancel; self.parts=[]
        self._responses = []
    def cancel_response(self):
        self.cancel.set()
        for resp in list(self._responses):
            try: resp.close()
            except Exception: pass
    def run(self):
        try:
            stream_kwargs = {}
            import inspect
            sig = inspect.signature(self.provider.stream)
            if 'response_holder' in sig.parameters:
                stream_kwargs['response_holder'] = self._responses
            for text in self.provider.stream(self.messages,self.config,self.cancel,**stream_kwargs):
                if self.cancel.is_set(): self.stopped_by_user.emit(); return
                if isinstance(text,StreamEvent):
                    if text.text: self.activity_received.emit(text.kind,text.text)
                    continue
                self.parts.append(text); self.delta_received.emit(text)
            if self.cancel.is_set(): self.stopped_by_user.emit()
            else:
                result = ''.join(self.parts)
                if result.strip():
                    self.completed.emit(result)
                else:
                    self.failed.emit('模型未返回任何内容，请稍后重试或检查模型配置。')
        except Exception as exc:
            self.stopped_by_user.emit() if self.cancel.is_set() else self.failed.emit(str(exc))


class _AcpWorker(QThread):
    delta_received = Signal(str)
    completed = Signal(str)
    failed = Signal(str)
    stopped_by_user = Signal()
    activity_received = Signal(str, str)
    permission_needed = Signal(object)
    meta_updated = Signal(str, object)
    context_reset_received = Signal(str)

    def __init__(
        self,
        acp_provider: AcpProvider,
        user_text: str,
        config: ProviderConfig,
        session: ChatSession,
        cancel: threading.Event,
        request_permission_fn,
        on_cancel_fn=None,
    ):
        super().__init__()
        self.acp_provider = acp_provider
        self.user_text = user_text
        self.config = config
        self.session = session
        self.cancel = cancel
        self._request_permission_fn = request_permission_fn
        self._on_cancel_fn = on_cancel_fn
        self.parts: list[str] = []

    def cancel_response(self):
        self.cancel.set()
        if callable(self._on_cancel_fn):
            try:
                self._on_cancel_fn()
            except Exception:
                pass

    def _on_permission(self, req):
        return self._request_permission_fn(self, req, self.cancel)

    def _on_meta(self, meta):
        self.meta_updated.emit(self.session.session_id, meta)

    def _on_context_reset(self, notice: str):
        self.context_reset_received.emit(notice)

    def run(self):
        try:
            for item in self.acp_provider.stream_turn(
                self.user_text,
                self.config,
                self.session,
                self.cancel,
                on_permission=self._on_permission,
                on_meta=self._on_meta,
                on_context_reset=self._on_context_reset,
            ):
                if self.cancel.is_set():
                    self.stopped_by_user.emit()
                    return
                if isinstance(item, StreamEvent):
                    if item.text:
                        self.activity_received.emit(item.kind, item.text)
                    continue
                self.parts.append(item)
                self.delta_received.emit(item)
            if self.cancel.is_set():
                self.stopped_by_user.emit()
            else:
                result = ''.join(self.parts)
                if result.strip():
                    self.completed.emit(result)
                else:
                    self.failed.emit('模型未返回任何内容，请稍后重试或检查模型配置。')
        except Exception as exc:
            if self.cancel.is_set():
                self.stopped_by_user.emit()
            else:
                self.failed.emit(format_acp_error(exc))


class _AcpMetaWorker(QThread):
    meta_updated = Signal(str, object)
    context_reset_received = Signal(str)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self.cancel = threading.Event()

    def run(self):
        if self.cancel.is_set():
            return
        try:
            result = self._fn()
            if self.cancel.is_set():
                return
            if isinstance(result, tuple) and len(result) == 3:
                session_id, meta, reset_notice = result
                if meta is not None:
                    self.meta_updated.emit(session_id, meta)
                if reset_notice:
                    self.context_reset_received.emit(reset_notice)
        except Exception as exc:
            if not self.cancel.is_set():
                self.failed.emit(format_acp_error(exc))


class ChatService(QObject):
    started=Signal(str); delta=Signal(str,str); finished=Signal(str,str); error=Signal(str,str); stopped=Signal(str)
    # (request_id, kind, text)：kind='reasoning' 思考过程增量 / 'status' 联网检索进度或工具状态
    activity=Signal(str,str,str)
    # ACP 扩展信号：(request_id, AcpPermissionRequest) / (session_id, AcpSessionMeta) / (request_id, notice_text)
    permission_requested=Signal(str,object)
    session_meta_updated=Signal(str,object)
    context_reset=Signal(str,str)
    # 进程级「回复完成」订阅（灵动岛事件动效等）：AppShell 注册一次，
    # 回调总在 GUI 线程执行（worker→本对象的链路全是 QueuedConnection）。
    _global_finished_listeners: list = []

    @classmethod
    def register_global_finished(cls, callback):
        if callback not in cls._global_finished_listeners:
            cls._global_finished_listeners.append(callback)

    @classmethod
    def unregister_global_finished(cls, callback):
        if callback in cls._global_finished_listeners:
            cls._global_finished_listeners.remove(callback)

    def __init__(self,provider=None,parent=None,*,acp_provider=None):
        super().__init__(parent); self.provider=provider or OpenAICompatibleProvider(); self.acp_provider=acp_provider or AcpProvider(); self._request_id=None; self._cancel=None; self._worker=None; self._workers=set()
        self._detached=False
        self._perm_lock=threading.Lock()
        self._pending_permissions: dict[str, tuple[threading.Event, list[str | None]]]={}
        # 档 B 记忆提炼（pet/chat/memory_extract）：前端在构造后挂 hook
        # （(user_text, reply_text, provider, provider_config) -> None），
        # finished 路径在发完信号后同步调一次——hook 内部只做护栏检查并起后台线程。
        self.memory_extract_hook=None; self._extract_user_text=None; self._extract_config=None
        # 退出时先取消并短等在飞 worker，避免 QThread 运行中被销毁导致崩溃
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def shutdown(self) -> bool:
        # QThread 并非 daemon 线程，若在运行中被 Python GC 丢弃会触发 'QThread: Destroyed while running' 崩溃。
        # 因此取消并有界等待后，超时未退出的 worker 仍保留在 self._workers 集合中（不强行 clear/置空），
        # 等其 run() 结束后由 finished 信号自行 discard 回收引用。
        self.stop()
        for worker in list(self._workers):
            if hasattr(worker, "cancel_response") and callable(worker.cancel_response):
                try: worker.cancel_response()
                except Exception: pass
            elif hasattr(worker, "cancel") and worker.cancel is not None:
                worker.cancel.set()
            if worker.isRunning():
                worker.wait(1500)
        # 返回 True 表示全部退出，False 表示仍有 worker 在运行（供调用方参考）
        return not any(worker.isRunning() for worker in self._workers)
    @property
    def busy(self): return self._worker is not None and self._worker.isRunning()
    def has_running_workers(self) -> bool:
        """还有没有没退出的 worker（含已 stop、线程仍在校尾的）。

        关窗即销毁的路径靠它决定「现在删还是等 worker 退了再删」：QThread 运行中
        被销毁会直接崩进程。``busy`` 只看当前那一只，会被上一轮的收尾线程漏掉。
        """
        return any(worker.isRunning() for worker in list(self._workers))
    def detach(self) -> None:
        """销毁前摘线：worker 已发出但尚未投递的排队信号不许再进本对象。

        关窗即销毁（``pet/chat/lifecycle.py``）之后，worker 的排队回调可能落在
        本对象已删之后——那时 ``self.stopped.emit`` 会抛
        "Signal source has been deleted"。置位后 ``_current`` 恒 False，全部
        排队回调第一行就返回（含 ``_cleanup``，不碰任何 Qt 对象）。
        """
        self._detached = True
        self.stop()
    def resolve_permission(self, permission_request_id: str, option_id: str | None) -> bool:
        with self._perm_lock:
            entry = self._pending_permissions.pop(str(permission_request_id), None)
        if entry is None:
            return False
        ev, holder = entry
        holder[0] = option_id
        ev.set()
        return True

    def _cancel_all_pending_permissions(self) -> None:
        with self._perm_lock:
            entries = list(self._pending_permissions.values())
            self._pending_permissions.clear()
        for ev, holder in entries:
            holder[0] = None
            ev.set()

    def _await_permission_from_worker(
        self,
        worker: _AcpWorker,
        req,
        cancel: threading.Event,
    ) -> str | None:
        if cancel.is_set() or self._detached:
            return None
        ev = threading.Event()
        holder: list[str | None] = [None]
        perm_id = str(req.request_id)
        with self._perm_lock:
            if cancel.is_set() or self._detached:
                return None
            self._pending_permissions[perm_id] = (ev, holder)
        worker.permission_needed.emit(req)
        try:
            while not ev.is_set():
                if cancel.is_set() or self._detached:
                    return None
                ev.wait(timeout=0.05)
            if cancel.is_set() or self._detached:
                return None
            return holder[0]
        finally:
            with self._perm_lock:
                self._pending_permissions.pop(perm_id, None)

    def _start_meta_worker(self, fn) -> None:
        if self._detached:
            return
        worker = _AcpMetaWorker(fn)
        self._workers.add(worker)
        worker.meta_updated.connect(
            self._session_meta_updated, Qt.ConnectionType.QueuedConnection
        )
        worker.context_reset_received.connect(
            self._context_reset_bg, Qt.ConnectionType.QueuedConnection
        )
        worker.failed.connect(
            self._meta_error, Qt.ConnectionType.QueuedConnection
        )
        worker.finished.connect(
            lambda w=worker: self._workers.discard(w),
            Qt.ConnectionType.QueuedConnection,
        )
        worker.start()

    def prepare_acp_session(self, config: ProviderConfig, session: ChatSession) -> None:
        if not getattr(config, 'is_acp', False) or session is None:
            return

        def _work():
            meta, was_reset = self.acp_provider.prepare_session(config, session)
            notice = CONTEXT_RESET_NOTICE if was_reset else ''
            return (session.session_id, meta, notice)

        self._start_meta_worker(_work)

    def set_acp_mode(self, config: ProviderConfig, session: ChatSession, mode_id: str) -> None:
        if not getattr(config, 'is_acp', False) or session is None:
            return

        def _work():
            meta = self.acp_provider.set_mode(config, session, mode_id)
            return (session.session_id, meta, '')

        self._start_meta_worker(_work)

    def set_acp_model(self, config: ProviderConfig, session: ChatSession, model_id: str) -> None:
        if not getattr(config, 'is_acp', False) or session is None:
            return

        def _work():
            meta = self.acp_provider.set_model(config, session, model_id)
            return (session.session_id, meta, '')

        self._start_meta_worker(_work)

    def send(self,messages:list[dict[str,Any]],config:ProviderConfig,request_id=None,*,session:ChatSession | None=None):
        self.stop(); rid=request_id or uuid.uuid4().hex; cancel=threading.Event()
        # 档 B 提炼上下文：本轮最后一条纯文本用户消息（图片等多模态形态不提炼）
        self._extract_user_text=None; self._extract_config=config
        for m in reversed(messages):
            if isinstance(m,dict) and m.get('role')=='user' and isinstance(m.get('content'),str):
                self._extract_user_text=m['content']; break

        if getattr(config, 'is_acp', False):
            acp_session = session if session is not None else ChatSession.create('', config.provider_id, '')
            user_text = self._extract_user_text or ''
            worker = _AcpWorker(
                self.acp_provider,
                user_text,
                config,
                acp_session,
                cancel,
                self._await_permission_from_worker,
                on_cancel_fn=self._cancel_all_pending_permissions,
            )
            worker.permission_needed.connect(
                lambda req, rid=rid: self._permission_requested(rid, req),
                Qt.ConnectionType.QueuedConnection,
            )
            worker.meta_updated.connect(
                self._session_meta_updated,
                Qt.ConnectionType.QueuedConnection,
            )
            worker.context_reset_received.connect(
                lambda notice, rid=rid: self._context_reset(rid, notice),
                Qt.ConnectionType.QueuedConnection,
            )
        else:
            worker=_Worker(self.provider,messages,config,cancel)

        self._request_id=rid; self._cancel=cancel; self._worker=worker
        self._workers.add(worker)
        # 全部显式 QueuedConnection：worker 线程 emit 的信号若直连 lambda
        # 会在 worker 线程执行 _delta/_cleanup 等，与 GUI 线程的 busy()/
        # _request_id 读写构成数据竞态。队列投递保证回调只在 GUI 线程跑。
        worker.delta_received.connect(lambda text,rid=rid:self._delta(rid,text), Qt.ConnectionType.QueuedConnection)
        worker.activity_received.connect(lambda kind,text,rid=rid:self._activity(rid,kind,text), Qt.ConnectionType.QueuedConnection)
        worker.completed.connect(lambda text,rid=rid:self._finished(rid,text), Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(lambda text,rid=rid:self._error(rid,text), Qt.ConnectionType.QueuedConnection)
        worker.stopped_by_user.connect(lambda rid=rid:self._stopped(rid), Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(lambda rid=rid:self._cleanup(rid), Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(lambda w=worker: self._workers.discard(w), Qt.ConnectionType.QueuedConnection)
        self.started.emit(rid); worker.start(); return rid
    def stop(self):
        self._cancel_all_pending_permissions()
        if self._worker is not None:
            if hasattr(self._worker, "cancel_response") and callable(self._worker.cancel_response):
                try: self._worker.cancel_response()
                except Exception: pass
            elif hasattr(self._worker, "cancel") and self._worker.cancel is not None:
                self._worker.cancel.set()
        elif self._cancel is not None:
            self._cancel.set()
    def _current(self,rid): return (not self._detached) and rid==self._request_id
    def _delta(self,rid,text):
        if self._current(rid): self.delta.emit(rid,text)
    def _activity(self,rid,kind,text):
        if self._current(rid): self.activity.emit(rid,kind,text)
    def _permission_requested(self,rid,req):
        if self._current(rid): self.permission_requested.emit(rid,req)
    def _session_meta_updated(self,session_id,meta):
        if not self._detached: self.session_meta_updated.emit(session_id,meta)
    def _context_reset(self,rid,notice):
        if self._current(rid): self.context_reset.emit(rid,notice)
    def _context_reset_bg(self,notice):
        if not self._detached: self.context_reset.emit('',notice)
    def _meta_error(self,text):
        if not self._detached:
            log.debug("ACP metadata helper failed: %s", text)
    def _finished(self,rid,text):
        if self._current(rid):
            self.finished.emit(rid,text)
            for cb in list(type(self)._global_finished_listeners):
                try: cb(text)
                except Exception: pass
            self._maybe_extract_memory(text)
    def _maybe_extract_memory(self,reply_text):
        # 档 B：回复完成后安排静默记忆提炼；ACP Agent 跳过；hook 异常绝不抛到对话路径
        hook=self.memory_extract_hook; user_text=self._extract_user_text; config=self._extract_config
        if hook is None or not user_text or config is None or getattr(config, 'is_acp', False): return
        try: hook(user_text,reply_text,self.provider,config)
        except Exception: log.exception("记忆提炼 hook 异常（已静默）")
    def _error(self,rid,text):
        if self._current(rid): self.error.emit(rid,text)
    def _stopped(self,rid):
        if self._current(rid): self.stopped.emit(rid)
    def _cleanup(self,rid):
        if self._current(rid): self._worker=None; self._cancel=None
