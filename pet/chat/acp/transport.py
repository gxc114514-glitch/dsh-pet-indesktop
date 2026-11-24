from __future__ import annotations

import collections
import itertools
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any, Callable

from .protocol import AcpError

log = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class StdioJsonRpcTransport:
    """Newline-delimited JSON-RPC 2.0 transport over a child process's stdio."""

    def __init__(
        self,
        command: str,
        args: list[str],
        cwd: str,
        env: dict[str, str],
        on_notification: Callable[[str, dict[str, Any]], None] | None = None,
        on_request: Callable[[Any, str, dict[str, Any]], None] | None = None,
        on_close: Callable[[int | None, str], None] | None = None,
    ) -> None:
        self._command = str(command)
        self._args = [str(a) for a in args]
        self._cwd = str(cwd) if cwd else ""
        self._env = dict(env)
        self._on_notification = on_notification
        self._on_request = on_request
        self._on_close = on_close

        self._proc: subprocess.Popen[bytes] | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, Future[Any]] = {}
        self._next_id = itertools.count(1)

        self._stderr_lock = threading.Lock()
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=200)

        self._closed = threading.Event()
        self._close_callback_lock = threading.Lock()
        self._close_callback_fired = False

        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    @property
    def is_alive(self) -> bool:
        proc = self._proc
        return (
            proc is not None
            and proc.poll() is None
            and not self._closed.is_set()
        )

    def start(self) -> None:
        if self._proc is not None:
            raise AcpError("ACP transport already started")

        search_path = self._env.get("PATH")
        if sys.platform == "win32":
            resolved = shutil.which(self._command, path=search_path) or self._command
        else:
            resolved = (
                self._command
                if os.path.sep in self._command
                else (shutil.which(self._command, path=search_path) or self._command)
            )

        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": self._cwd or None,
            "env": self._env,
            "bufsize": 0,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = _CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True

        try:
            self._proc = subprocess.Popen([resolved, *self._args], **popen_kwargs)
        except OSError as exc:
            raise AcpError(
                f"Failed to start ACP process {self._command!r}: {exc}"
            ) from exc

        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name="acp-stderr-reader",
            daemon=True,
        )
        self._stdout_thread = threading.Thread(
            target=self._stdout_loop,
            name="acp-stdout-reader",
            daemon=True,
        )
        self._stderr_thread.start()
        self._stdout_thread.start()

    def stderr_tail(self, max_lines: int = 10) -> str:
        if max_lines <= 0:
            return ""
        with self._stderr_lock:
            if not self._stderr_lines:
                return ""
            lines = list(self._stderr_lines)[-max_lines:]
        return "\n".join(lines)

    def _write_message(self, payload: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed.is_set():
            raise AcpError(
                "ACP transport is closed", stderr_tail=self.stderr_tail()
            )
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if proc.stdin is None or self._closed.is_set():
                raise AcpError(
                    "ACP transport is closed", stderr_tail=self.stderr_tail()
                )
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AcpError(
                    f"Failed to write to ACP stdin: {exc}",
                    stderr_tail=self.stderr_tail(),
                ) from exc

    def send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = 30.0,
    ) -> dict[str, Any]:
        if self._closed.is_set() or self._proc is None:
            raise AcpError(
                "ACP transport is closed", stderr_tail=self.stderr_tail()
            )

        rpc_id = next(self._next_id)
        fut: Future[Any] = Future()
        with self._pending_lock:
            if self._closed.is_set():
                raise AcpError(
                    "ACP transport is closed", stderr_tail=self.stderr_tail()
                )
            self._pending[rpc_id] = fut

        try:
            self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "method": method,
                    "params": params,
                }
            )
        except Exception:
            with self._pending_lock:
                self._pending.pop(rpc_id, None)
            raise

        try:
            result = fut.result(timeout=timeout)
        except FutureTimeoutError as exc:
            with self._pending_lock:
                self._pending.pop(rpc_id, None)
            raise AcpError(
                f"ACP request {method!r} timed out after {timeout}s",
                stderr_tail=self.stderr_tail(),
            ) from exc

        return result if isinstance(result, dict) else {}

    def send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    def send_response(self, rpc_id: Any, result: dict[str, Any]) -> None:
        self._write_message(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": result,
            }
        )

    def send_error(self, rpc_id: Any, code: int, message: str) -> None:
        self._write_message(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": int(code), "message": str(message)},
            }
        )

    def _fail_all_pending(self, exc: AcpError) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(exc)

    def _fire_on_close(self, returncode: int | None, stderr_tail: str) -> None:
        with self._close_callback_lock:
            if self._close_callback_fired:
                return
            self._close_callback_fired = True
        if self._on_close is not None:
            try:
                self._on_close(returncode, stderr_tail)
            except Exception:
                log.debug("ACP on_close callback raised", exc_info=True)

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stream = proc.stderr
        try:
            while True:
                raw = stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                with self._stderr_lock:
                    self._stderr_lines.append(line)
        except (OSError, ValueError):
            pass

    def _dispatch_incoming_request(
        self, rpc_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        if self._on_request is None:
            try:
                self.send_error(rpc_id, -32601, f"Method not found: {method}")
            except AcpError:
                pass
            return
        try:
            self._on_request(rpc_id, method, params)
        except Exception as exc:
            log.debug(
                "ACP on_request handler failed for %s", method, exc_info=True
            )
            try:
                self.send_error(rpc_id, -32603, str(exc))
            except AcpError:
                pass

    def _stdout_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while True:
                raw = stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("Ignoring non-JSON ACP stdout line: %s", line[:200])
                    continue
                if not isinstance(msg, dict):
                    continue

                if "id" in msg and "method" not in msg:
                    rpc_id = msg["id"]
                    with self._pending_lock:
                        fut = self._pending.pop(rpc_id, None)
                    if fut is None or fut.done():
                        continue
                    err = msg.get("error")
                    if err is not None:
                        if isinstance(err, dict):
                            code = err.get("code")
                            message = str(err.get("message") or "ACP RPC error")
                        else:
                            code = None
                            message = str(err)
                        fut.set_exception(
                            AcpError(
                                message,
                                code=code if isinstance(code, int) else None,
                                stderr_tail=self.stderr_tail(),
                            )
                        )
                    else:
                        fut.set_result(msg.get("result"))
                elif "method" in msg and "id" in msg:
                    # CRITICAL: dispatch agent->client requests on a helper daemon
                    # thread so stdout reading never blocks while waiting for user input.
                    rpc_id = msg["id"]
                    method = str(msg["method"])
                    params = msg.get("params")
                    params_dict = params if isinstance(params, dict) else {}
                    t = threading.Thread(
                        target=self._dispatch_incoming_request,
                        args=(rpc_id, method, params_dict),
                        name=f"acp-req-{method}",
                        daemon=True,
                    )
                    t.start()
                elif "method" in msg:
                    method = str(msg["method"])
                    params = msg.get("params")
                    params_dict = params if isinstance(params, dict) else {}
                    if self._on_notification is not None:
                        try:
                            self._on_notification(method, params_dict)
                        except Exception:
                            log.debug(
                                "ACP on_notification handler failed for %s",
                                method,
                                exc_info=True,
                            )
        except (OSError, ValueError):
            pass
        finally:
            rc: int | None = proc.poll()
            if rc is None:
                try:
                    rc = proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    rc = proc.poll()
            stderr_t = self._stderr_thread
            if stderr_t is not None and stderr_t is not threading.current_thread():
                stderr_t.join(timeout=0.3)

            tail = self.stderr_tail()
            self._closed.set()
            err = AcpError(
                f"ACP process exited unexpectedly (code={rc})",
                code=rc,
                stderr_tail=tail,
            )
            self._fail_all_pending(err)
            self._fire_on_close(rc, tail)

    def close(self, graceful_timeout: float = 3.0) -> None:
        self._closed.set()
        proc = self._proc
        if proc is None:
            return

        with self._write_lock:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        if proc.poll() is None:
            initial_wait = min(0.25, max(0.05, graceful_timeout / 4.0))
            try:
                proc.wait(timeout=initial_wait)
            except subprocess.TimeoutExpired:
                pass

        if proc.poll() is None:
            if sys.platform == "win32":
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=graceful_timeout)
                except subprocess.TimeoutExpired:
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=_CREATE_NO_WINDOW,
                            timeout=5.0,
                            check=False,
                        )
                    except (OSError, subprocess.SubprocessError):
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
            else:
                pgid: int | None = None
                try:
                    pgid = os.getpgid(proc.pid)
                except OSError:
                    pgid = None

                try:
                    if pgid is not None:
                        os.killpg(pgid, signal.SIGTERM)
                    else:
                        proc.terminate()
                except OSError:
                    try:
                        proc.terminate()
                    except OSError:
                        pass

                try:
                    proc.wait(timeout=graceful_timeout)
                except subprocess.TimeoutExpired:
                    try:
                        if pgid is not None:
                            os.killpg(pgid, signal.SIGKILL)
                        else:
                            proc.kill()
                    except OSError:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass

        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

        tail = self.stderr_tail()
        self._fail_all_pending(
            AcpError("ACP transport closed", code=proc.poll(), stderr_tail=tail)
        )

        cur = threading.current_thread()
        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None and t is not cur and t.is_alive():
                t.join(timeout=1.0)

        self._fire_on_close(proc.poll(), tail)
