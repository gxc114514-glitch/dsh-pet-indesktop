# DSH-Pet Unified Multi-Agent ACP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an embedded unified ACP (Agent Client Protocol) multi-agent client and interactive Modern Chat UI (workspace picker, dynamic mode/model dropdowns, in-chat permission approval cards, first-turn voice-friendly pet tone, AI settings, and macOS `.app` / Windows `.exe` packaging) to `dsh-pet-indesktop`.

**Architecture:** A pure-Python ACP JSON-RPC 2.0 stdio stack lives under `pet/chat/acp/` (`presets`, `protocol`, `transport`, `client`, `manager`) decoupled from the ephemeral close-destroys-window `ChatWindow` (`pet/chat/lifecycle.py`). An `AcpProvider` bridges the process-level `AcpManager` into `ChatService`'s single-active-turn `QThread` worker so existing `PetChatLink` animations and `ChatReplyReader` TTS voice playback work seamlessly, while Modern `ChatWindow` (`pet/chat/widgets.py`) and `_AiSettingsPage` (`pet/chat/ai_settings_page.py`) gain ACP-specific interactive controls without touching `pet/window.py` or `pet/modern_settings_dialog.py`.

**Tech Stack:** Python 3.15, PySide6 6.12, pure-Python `subprocess`/`threading`/`json` for ACP JSON-RPC 2.0 over stdio, pytest, ruff, PyInstaller 6.22.

**Spec:** `docs/superpowers/specs/2026-11-24-dsh-pet-acp-design.md`

## Global Constraints

- **Python & Qt floor**: Must run on Python 3.15 + PySide6 6.12 (macOS arm64 primary, Windows backup); pure-Python ACP core (`pet/chat/acp/*.py` except optional `QApplication.aboutToQuit` hook via lazy import) must NOT import `PySide6` at module top level so unit tests run headless without Qt.
- **Architecture guardrails (`tests/test_architecture.py`)**: Do NOT increase `pet/window.py` or `pet/modern_settings_dialog.py` beyond their line budgets; NEVER add top-level `pet.chat.*` imports to `pet/modern_settings_dialog.py` (`no-chat` variant guard).
- **Config nesting**: All ACP configuration lives inside `ChatSettings` / `ProviderConfig` (`pet/chat/models.py`) and `ChatSession`; do NOT add new top-level `config.json` keys.
- **Settings UI rule**: Every new settings control must be wrapped in `SettingRow` inside `pet/chat/ai_settings_page.py` (or a helper module imported by it).
- **Qt threading**: Every cross-thread worker → GUI signal connection must use `Qt.ConnectionType.QueuedConnection`.
- **Close-destroys-window (`pet/chat/lifecycle.py`)**: Closing `ChatWindow` cancels any in-flight turn/pending permission and destroys `ChatWindow`/`ChatService`, while keeping idle `AcpClient` processes warm in the process-level `AcpManager` until idle-reap or `aboutToQuit`.
- **Single active turn (v1)**: Switching session, provider, workspace, mode, or model while a turn is running or awaiting permission is blocked or prompts to stop first.
- **Safe defaults**: Never pass unattended auto-approve flags in built-in presets; never silently auto-approve on permission timeout.
- **No regressions**: Existing OpenAI-compatible chat, `ChatReplyReader` TTS voice readout, `no-chat` build exclusion, and `ruff check pet/ tests/` must remain green.

## Review Focus

1. **Closing `ChatWindow` or clicking Stop while an inline `PermissionCard` is waiting**: Must unblock `AcpClient`'s waiting thread with ACP `{outcome: {outcome: "cancelled"}}` and never deadlock `release_window_when_idle` or leak a zombie worker `QThread`. (Pinned in Task 2 & Task 3 tests.)
2. **Non-UTF-8 bytes, partial/interleaved stdout noise, or high-volume stderr from an agent CLI**: Must never crash the stdio reader thread or fill OS pipe buffers; stderr is drained into a bounded 200-line ring buffer and non-JSON stdout lines are ignored/logged safely. (Pinned in Task 2 tests.)
3. **Agent crash / unexpected EOF mid-turn or mid-permission-wait**: Must reject pending RPC futures immediately, emit `ChatService.error` with exit code + stderr tail, dismiss active approval cards, and evict the dead `AcpClient` from `AcpManager` so the next message spawns cleanly. (Pinned in Task 2 & Task 3 tests.)
4. **Frozen macOS `.app` launched from Finder/Dock with minimal `PATH` (`/usr/bin:/bin`) and Windows `.cmd` shims (`npx.cmd`)**: Must augment child `PATH` cleanly without clobbering user `env`, resolve Windows shims with `CREATE_NO_WINDOW`, and terminate full child process trees on shutdown. (Pinned in Task 1 & Task 2 tests.)
5. **Mode/Model schema variation across agents (legacy `modes`/`models` vs ACP `configOptions` vs none advertised)**: Header dropdowns must populate from either schema, stay hidden when unsupported, and sync cleanly on `current_mode_update`/`config_option_update` notifications without crashing. (Pinned in Task 2 & Task 4 tests.)

---

### Task 1: Extended Chat Models & Built-in ACP Launch Presets

**Files:**
- Create: `pet/chat/acp/__init__.py`
- Create: `pet/chat/acp/presets.py`
- Modify: `pet/chat/models.py`
- Test: `tests/test_acp_models_presets.py`

**Interfaces:**
- Consumes: Existing `ProviderConfig`, `ChatSettings`, `ChatSession` in `pet/chat/models.py`.
- Produces:
  - `DEFAULT_ACP_TONE_PROMPT: str` in `pet/chat/acp/presets.py`
  - `AcpAgentConfig` dataclass in `pet/chat/models.py` with fields: `preset: str = 'custom'`, `command: str = ''`, `args: list[str]`, `env: dict[str, str]`, `default_cwd: str = ''`, `default_mode_id: str = ''`, `default_model_id: str = ''`, `tone_enabled: bool = True`, `tone_prompt: str = DEFAULT_ACP_TONE_PROMPT`, `startup_timeout: float = 20.0`, `idle_timeout_minutes: int = 30`, plus `from_dict(raw)` / `to_dict()`
  - `ProviderConfig.kind: str = 'openai'` (`'openai'` | `'acp'`), `ProviderConfig.acp: AcpAgentConfig | None = None`, `ProviderConfig.is_acp: bool` property
  - `ChatSettings.recent_workspaces: list[str]`, `ChatSettings.record_workspace(path: str, limit: int = 10) -> None`, `ChatSettings.ensure_acp_presets() -> bool`
  - `ChatSession.workspace_dir: str = ''`, `ChatSession.acp_session_id: str = ''`, `ChatSession.acp_mode_id: str = ''`, `ChatSession.acp_model_id: str = ''` (omitted from `to_dict()` when empty)
  - `pet/chat/acp/presets.py`: `ACP_PRESETS: dict[str, dict]`, `build_preset_provider(preset_id: str, platform_name: str | None = None) -> ProviderConfig`, `detect_builtin_acp_providers(platform_name: str | None = None) -> dict[str, ProviderConfig]`, `augmented_env(user_env: dict[str, str] | None = None, executable: str = '', platform_name: str | None = None) -> dict[str, str]`

- [ ] **Step 1: Write the failing test in `tests/test_acp_models_presets.py`**

Test assertions to pin:
- Legacy `ProviderConfig.from_dict("openai-main", {"name": "DeepSeek"})` loads with `kind == "openai"`, `acp is None`, `is_acp is False`, and `to_dict()` contains no `"acp"` key and preserves existing OpenAI fields identically.
- ACP `ProviderConfig` round-trips `kind == "acp"` and all `AcpAgentConfig` fields (`preset`, `command`, `args`, `env`, `default_cwd`, `default_mode_id`, `default_model_id`, `tone_enabled`, `tone_prompt`, `startup_timeout`, `idle_timeout_minutes`).
- `ChatSession` omits empty `workspace_dir`/`acp_session_id`/`acp_mode_id`/`acp_model_id` from `to_dict()` and round-trips non-empty values cleanly.
- `ChatSettings.record_workspace()` deduplicates, moves latest to front, caps at 10, and `ensure_acp_presets()` seeds `acp-factory`, `acp-devin`, `acp-dsh`, `acp-codex` only when no ACP providers exist (never overwriting user-edited ACP providers).
- `detect_builtin_acp_providers("darwin")` resolves all 4 presets with safe args (no `--yolo` or `--skip-permissions-unsafe`), uses DSH bundled Node 24 path when present (falling back to `dsh --profile acp`), and sets `startup_timeout == 45.0` for `acp-codex`.
- `detect_builtin_acp_providers("win32")` produces `droid`, `devin`, `dsh`, `npx` presets.
- `augmented_env({"CUSTOM": "1", "PATH": "/my/bin"}, executable="/opt/homebrew/bin/foo", platform_name="darwin")` preserves `CUSTOM` and `/my/bin` at front while appending missing standard macOS paths (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`) without duplicating entries.

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_models_presets.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'pet.chat.acp'`)

- [ ] **Step 3: Implement `pet/chat/acp/__init__.py`, `pet/chat/acp/presets.py`, and extend `pet/chat/models.py`**

Implement the exact dataclasses, presets (exact verified macOS paths from Spec §6.3 + Windows/PATH fallbacks), and backward-compatible `from_dict`/`to_dict` serialization. Keep `pet/chat/acp/presets.py` pure Python (zero Qt imports).

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_models_presets.py tests/test_chat_subsystem.py tests/test_architecture.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pet/chat/acp/__init__.py pet/chat/acp/presets.py pet/chat/models.py tests/test_acp_models_presets.py
git commit -m "feat(chat): add ACP config/session models and 4-agent launch presets"
```

---

### Task 2: Pure-Python ACP Protocol, Stdio Transport & `AcpClient`

**Files:**
- Create: `pet/chat/acp/protocol.py`
- Create: `pet/chat/acp/transport.py`
- Create: `pet/chat/acp/client.py`
- Test: `tests/test_acp_client.py`

**Interfaces:**
- Consumes: `AcpAgentConfig`, `augmented_env` from Task 1.
- Produces:
  - `pet/chat/acp/protocol.py`:
    - `AcpError(RuntimeError)` (with `code: int | None`, `stderr_tail: str`)
    - `AcpModeOption(mode_id: str, name: str, description: str = '')`
    - `AcpModelOption(model_id: str, name: str, description: str = '')`
    - `AcpSessionMeta(session_id: str, modes: list[AcpModeOption], current_mode_id: str, models: list[AcpModelOption], current_model_id: str, mode_config_id: str = '', model_config_id: str = '')`
    - `AcpPermissionOption(option_id: str, name: str, kind: str)`
    - `AcpPermissionRequest(request_id: str, rpc_id: Any, session_id: str, tool_call_id: str, title: str, kind: str, detail: str, options: list[AcpPermissionOption])`
    - `AcpUpdateEvent(kind: str, text: str = '', tool_call_id: str = '', session_meta: AcpSessionMeta | None = None)` where `kind` in `('text', 'thought', 'status', 'meta')`
    - Pure parsers: `parse_session_meta(session_id: str, result: dict) -> AcpSessionMeta`, `parse_session_update(params: dict, current_meta: AcpSessionMeta | None = None) -> list[AcpUpdateEvent]`, `parse_permission_request(rpc_id: Any, params: dict) -> AcpPermissionRequest`, `format_permission_response(option_id: str | None) -> dict` (`option_id is None` → `{"outcome": {"outcome": "cancelled"}}`; else `{"outcome": {"outcome": "selected", "optionId": option_id}}`)
  - `pet/chat/acp/transport.py`:
    - `StdioJsonRpcTransport(command: str, args: list[str], cwd: str, env: dict[str, str], on_notification, on_request, on_close)`
    - Methods: `start()`, `send_request(method: str, params: dict, timeout: float = 30.0) -> dict`, `send_notification(method: str, params: dict) -> None`, `send_response(rpc_id: Any, result: dict) -> None`, `send_error(rpc_id: Any, code: int, message: str) -> None`, `close(graceful_timeout: float = 3.0) -> None`, `stderr_tail(max_lines: int = 10) -> str`, `is_alive: bool`
    - Platform spawning: `start_new_session=True` + `os.killpg` on POSIX; `subprocess.CREATE_NO_WINDOW` + `taskkill /F /T /PID` fallback on Windows; binary `stdout`/`stderr` decoded with `errors="replace"`; stderr drained into `collections.deque(maxlen=200)`; non-JSON stdout lines ignored safely.
  - `pet/chat/acp/client.py`:
    - `AcpClient(config: AcpAgentConfig, workspace_dir: str)`
    - Properties: `is_alive: bool`, `agent_name: str`, `agent_version: str`, `supports_load_session: bool`, `last_activity_at: float`
    - Methods:
      - `start() -> dict` (spawns transport + runs `initialize` with `protocolVersion=1`, `clientCapabilities={"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}`)
      - `ensure_session(acp_session_id: str = '') -> tuple[AcpSessionMeta, bool, bool]` returning `(meta, is_fresh_new, context_was_reset)`
      - `set_mode(session_id: str, mode_id: str) -> AcpSessionMeta` (uses `session/set_config_option` if `mode_config_id` set, else `session/set_mode`)
      - `set_model(session_id: str, model_id: str) -> AcpSessionMeta` (uses `session/set_config_option` if `model_config_id` set, else `session/set_model`)
      - `prompt(session_id: str, text: str, on_event, on_permission, cancel_event: threading.Event) -> dict`
      - `cancel(session_id: str) -> None`
      - `close() -> None`

- [ ] **Step 1: Write the failing test in `tests/test_acp_client.py`**

Include a self-contained Python mock ACP server script (invoked via `sys.executable`) that speaks real newline-delimited JSON-RPC 2.0 over stdin/stdout and supports test modes via env/args. Pin tests for:
1. `initialize` handshake verifying `fs: false, terminal: false` and `agentInfo` parsing.
2. `ensure_session` creating new session, parsing both legacy `modes`/`models` and `configOptions` schemas, and `session/load` fallback setting `context_was_reset=True`.
3. `set_mode` and `set_model` dispatching `session/set_mode`/`session/set_model` or `session/set_config_option` and handling `current_mode_update` notifications.
4. `prompt` streaming `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, and `plan`.
5. Bidirectional `session/request_permission`: allow (`optionId`), deny, and `cancel_event` unblocking a pending permission with `{outcome: {outcome: "cancelled"}}` plus sending `session/cancel`.
6. Review Focus edge cases: non-UTF-8 bytes + non-JSON stdout banner lines + 300 lines of stderr (bounded to 200 without blocking), agent crash mid-turn (immediate `AcpError` with exit code + stderr tail), startup timeout killing hung process, and `close()` leaving zero live child PIDs.

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_client.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `protocol.py`, `transport.py`, and `client.py`**

Keep all three modules pure Python (zero `PySide6` imports). Ensure reader thread dispatches `session/request_permission` on a dedicated helper thread (never blocking the stdout reader loop, so `session/update` or cancellation notifications keep flowing while waiting for user input!).

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_client.py -v`
Expected: PASS (all cases, zero hanging threads/processes)

- [ ] **Step 5: Commit**

```bash
git add pet/chat/acp/protocol.py pet/chat/acp/transport.py pet/chat/acp/client.py tests/test_acp_client.py
git commit -m "feat(chat): implement pure-Python ACP protocol, stdio transport, and client"
```

---

### Task 3: Process-Level `AcpManager`, `AcpProvider` & `ChatService` / `PetChatLink` Integration

**Files:**
- Create: `pet/chat/acp/manager.py`
- Create: `pet/chat/acp_provider.py`
- Modify: `pet/chat/service.py`
- Modify: `pet/chat/pet_link.py`
- Test: `tests/test_acp_service.py`

**Interfaces:**
- Consumes: `AcpClient`, `AcpSessionMeta`, `AcpPermissionRequest`, `AcpUpdateEvent`, `AcpError` (Task 2); `ProviderConfig`, `ChatSession` (Task 1); `ChatService`, `_Worker`, `PetChatLink`.
- Produces:
  - `pet/chat/acp/manager.py`:
    - `AcpManager`: `acquire(provider_id: str, config: AcpAgentConfig, workspace_dir: str) -> AcpClient`, `evict(provider_id: str, workspace_dir: str) -> None`, `mark_busy(key, busy: bool)`, `reap_idle() -> int`, `shutdown_all() -> None`, `probe_connection(config: ProviderConfig) -> tuple[bool, str]`
    - Module singleton `get_acp_manager() -> AcpManager` (registers `shutdown_all` on `QApplication.aboutToQuit` lazily when Qt app exists; pure-Python usable when no Qt app exists).
  - `pet/chat/acp_provider.py`:
    - `AcpProvider(manager: AcpManager | None = None)`:
      - `stream_turn(user_text: str, config: ProviderConfig, session: ChatSession, cancel_event: threading.Event, on_permission, on_meta, on_context_reset) -> Iterator[str | StreamEvent]`
      - Injects `config.acp.tone_prompt` once on first turn of a newly created `session/new` (`is_fresh_new` and `tone_enabled` and non-empty `tone_prompt`); never repeats on turn 2+ or `session/load`.
      - If tool activity occurred and `stopReason == "end_turn"` with empty text, yields `"已完成操作。"`.
      - `prepare_session(config: ProviderConfig, session: ChatSession) -> tuple[AcpSessionMeta, bool]`
      - `set_mode(config: ProviderConfig, session: ChatSession, mode_id: str) -> AcpSessionMeta`
      - `set_model(config: ProviderConfig, session: ChatSession, model_id: str) -> AcpSessionMeta`
  - `pet/chat/service.py` (`ChatService` additions):
    - Signals: `permission_requested = Signal(str, object)` (`request_id`, `AcpPermissionRequest`), `session_meta_updated = Signal(str, object)` (`session_id`, `AcpSessionMeta`), `context_reset = Signal(str, str)` (`request_id`, `notice_text`)
    - `send(messages, config, request_id=None, *, session=None)`: dispatches to `AcpProvider` when `config.is_acp`, else existing OpenAI `self.provider`.
    - `resolve_permission(permission_request_id: str, option_id: str | None) -> bool` (unblocks waiting permission event; `None` = cancelled).
    - `stop()` & `detach()` cancel all pending permissions as `None` (cancelled) in addition to signalling `cancel_event`.
    - `_maybe_extract_memory()` returns early if `getattr(config, "is_acp", False)`.
    - Async helpers (run in background `QThread` / worker with queued signals so GUI never blocks): `prepare_acp_session(config: ProviderConfig, session: ChatSession)`, `set_acp_mode(config: ProviderConfig, session: ChatSession, mode_id: str)`, `set_acp_model(config: ProviderConfig, session: ChatSession, model_id: str)`.
  - `pet/chat/pet_link.py`:
    - `PetChatLink.waiting_permission(self, text: str = '需要你确认权限～') -> None`

- [ ] **Step 1: Write the failing test in `tests/test_acp_service.py`**

Using real Qt event loop (`QT_QPA_PLATFORM=offscreen`) and the mock stdio ACP server script from Task 2, test:
1. Full `ChatService.send(..., session=session)` turn with ACP provider: `started` → `session_meta_updated` → `activity` (reasoning + tool status) → `permission_requested` → `resolve_permission(allow)` → `delta` → `finished`.
2. `_global_finished_listeners` fires with final assistant text (confirming `ChatReplyReader` TTS integration) AND `memory_extract_hook` is NOT called for ACP providers (while still called for OpenAI providers).
3. First-turn tone preamble is prepended on turn 1 of a fresh session, NOT on turn 2 of the same session, and NOT when `tone_enabled=False`.
4. Tool-only turn with `end_turn` completes with `"已完成操作。"` instead of error.
5. Closing/detaching `ChatService` (simulating `release_window_when_idle`) while a permission card is pending unblocks cleanly without deadlock, emits no post-detach signal crashes, and keeps the idle `AcpClient` reusable in `AcpManager`.
6. Agent crash mid-turn emits `error` with stderr tail and evicts the dead client from `AcpManager` so the next turn recovers automatically.
7. `AcpManager.reap_idle()` terminates idle clients past timeout (and skips `idle_timeout_minutes == 0`) and `shutdown_all()` terminates all clients cleanly.

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_service.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `manager.py`, `acp_provider.py`, and extend `service.py` & `pet_link.py`**

Preserve 100% compatibility with existing `ChatService` tests (`tests/test_chat_service.py`, `tests/test_chat_window_lifecycle.py`, `tests/test_chat_memory_extract.py`, `tests/test_chat_reply_voice.py`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_service.py tests/test_chat_service.py tests/test_chat_window_lifecycle.py tests/test_chat_memory_extract.py tests/test_chat_reply_voice.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pet/chat/acp/manager.py pet/chat/acp_provider.py pet/chat/service.py pet/chat/pet_link.py tests/test_acp_service.py
git commit -m "feat(chat): add process-level AcpManager, AcpProvider, and ChatService ACP wiring"
```

---

### Task 4: Inline `PermissionCard` & Modern `ChatWindow` Interactive ACP UI

**Files:**
- Create: `pet/chat/permission_card.py`
- Modify: `pet/chat/widgets.py`
- Modify: `pet/chat/modern_styles.qss`
- Test: `tests/test_acp_chat_ui.py`

**Interfaces:**
- Consumes: `ChatService` ACP signals/methods (Task 3), `AcpPermissionRequest`, `AcpSessionMeta` (Task 2), `ProviderConfig.is_acp`, `ChatSession` ACP fields, `ChatSettings.recent_workspaces` (Task 1).
- Produces:
  - `pet/chat/permission_card.py`:
    - `PermissionCard(request: AcpPermissionRequest, parent=None)`: signal `decided = Signal(str, str)` (`request_id`, `option_id`), method `mark_resolved(label: str, state: str = 'allowed')` (`'allowed'` | `'denied'` | `'cancelled'`), collapses buttons into a single-line audit badge once decided.
    - `ContextResetBanner(text: str, parent=None)`: compact centered divider widget inserted into `message_host_layout`.
  - `pet/chat/widgets.py` (`ChatWindow` additions):
    - Header controls: `self.provider_combo` (lists all providers with `[ACP]` / `[API]` prefix/badge), `self.acp_mode_combo`, `self.acp_model_combo` (dynamically shown/hidden based on `AcpSessionMeta`, disabled while `service.busy`).
    - ACP Workspace bar (`self.workspace_bar`, visible only when active provider `is_acp`): `self.workspace_combo` (recent workspaces + current folder) + `self.workspace_pick_btn` (`QFileDialog.getExistingDirectory`). Changing workspace updates empty draft session in-place, or starts a new session if current session already has messages.
    - Single-active-turn guard: `_guard_busy_switch() -> bool` checks `self.service.busy`; if busy, asks user whether to stop current turn (in tests or non-interactive, respects confirmation) and only switches if stopped.
    - Session sidebar (`SessionListRow`): shows secondary subtitle line (`AgentName · folder_name`) when session belongs to an ACP provider.
    - `_on_permission_requested(request_id, req)`: inserts `PermissionCard` at `message_host_layout.count() - 1`, calls `self.pet_link.waiting_permission()`, wires `card.decided` → `self.service.resolve_permission(...)`. On `_stopped` / `_error` / close, marks any open `PermissionCard` as `'cancelled'`.
    - `_on_context_reset(request_id, notice)`: inserts `ContextResetBanner` into `message_host_layout`.
    - `_begin_generation`: when active provider `is_acp`, bypasses `memory_recall` and full roleplay/system prompt builder, passes `session=self.session` to `self.service.send(...)`, and persists updated `acp_session_id`/`acp_mode_id`/`acp_model_id` via `self.store.save(self.session)`.

- [ ] **Step 1: Write the failing test in `tests/test_acp_chat_ui.py`**

Using `QT_QPA_PLATFORM=offscreen` + a temp config dir + mock stdio ACP server:
1. Opening `ChatWindow` with an OpenAI provider hides `workspace_bar`, `acp_mode_combo`, `acp_model_combo`.
2. Switching `provider_combo` to an ACP provider shows `workspace_bar`, triggers `prepare_acp_session`, populates `acp_mode_combo` and `acp_model_combo` from `session_meta_updated`, and hides model combo when agent advertises no models.
3. Changing workspace on an empty session updates `session.workspace_dir` in place and records to `settings.recent_workspaces`; changing workspace after a message exists creates a new session with the new `workspace_dir`.
4. Sending a message renders streaming text, tool activity, and an inline `PermissionCard`; clicking an option button resolves permission, collapses the card to an audit badge, completes the turn, and invokes `ChatReplyReader`/global finished listener.
5. Clicking Stop while a `PermissionCard` is open cancels the permission card and marks it cancelled without hanging; attempting to switch session while busy triggers the single-active-turn guard.
6. `context_reset` signal inserts a `ContextResetBanner` into the timeline.

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_chat_ui.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `permission_card.py`, update `widgets.py`, and add QSS rules to `modern_styles.qss`**

Keep styling consistent with both light and dark themes in `pet/chat/modern_styles.qss` (and `themes.py`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_chat_ui.py tests/test_chat_timeline_scroll.py tests/test_chat_window_lifecycle.py tests/test_chat_shared_behavior.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pet/chat/permission_card.py pet/chat/widgets.py pet/chat/modern_styles.qss tests/test_acp_chat_ui.py
git commit -m "feat(chat): add inline PermissionCard and interactive ACP Modern Chat UI"
```

---

### Task 5: AI Settings Page ACP Configuration & Handshake Probe

**Files:**
- Create: `pet/chat/acp_settings_section.py`
- Modify: `pet/chat/ai_settings_page.py`
- Modify: `pet/chat/providers.py` (`test_connection` dispatch for ACP)
- Test: `tests/test_acp_settings_ui.py`

**Interfaces:**
- Consumes: `AcpAgentConfig`, `ProviderConfig.kind`, `detect_builtin_acp_providers`, `build_preset_provider` (Task 1); `get_acp_manager().probe_connection` (Task 3).
- Produces:
  - `pet/chat/acp_settings_section.py`: builds all ACP `SettingRow` widgets (`acp_preset`, `acp_command`, `acp_args`, `acp_env`, `acp_default_cwd`, `acp_tone_enabled`, `acp_tone_prompt`, `acp_idle_timeout`, plus "自动检测本地 Agent" button) and provides `load_from_draft(draft)`, `capture_to_draft(draft)`, `set_visible(visible: bool)`.
  - `pet/chat/ai_settings_page.py`: adds `provider_kind` `SettingRow` (`OpenAI 兼容 API` / `ACP 编程 Agent`), toggles OpenAI vs ACP row groups cleanly when switching type or provider, persists `kind` and `acp` dict through `_provider_drafts` and `save()`, and supports "自动检测本地 Agent" to seed/refresh the 4 presets.
  - `pet/chat/providers.py`: `test_connection(config, timeout=10.0)` delegates to `get_acp_manager().probe_connection(config)` when `config.is_acp` (so the existing "测试连接" button works for both OpenAI and ACP providers!).

- [ ] **Step 1: Write the failing test in `tests/test_acp_settings_ui.py`**

Pin assertions:
1. `_AiSettingsPage` renders all new ACP controls wrapped in `SettingRow` and shows OpenAI rows for `kind == "openai"`, ACP rows for `kind == "acp"`.
2. Switching a provider to `kind == "acp"`, filling command/args/env/default_cwd/tone_enabled/tone_prompt/idle_timeout, and calling `save()` persists cleanly into `config.chat_settings()` and reloads identically.
3. Clicking "自动检测本地 Agent" populates missing `acp-factory`/`acp-devin`/`acp-dsh`/`acp-codex` presets without overwriting an already customized provider.
4. `providers.test_connection(acp_config)` against the mock stdio ACP server returns `(True, message)` including agent name/version and advertised modes/models count.
5. `tests/test_architecture.py` remains 100% green (`MODERN_SETTINGS_DIALOG_PY_LINE_BUDGET` untouched, zero top-level `pet.chat` import in `modern_settings_dialog.py`).

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_settings_ui.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `acp_settings_section.py`, update `ai_settings_page.py` and `providers.py`**

Do NOT modify `pet/modern_settings_dialog.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_settings_ui.py tests/test_architecture.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pet/chat/acp_settings_section.py pet/chat/ai_settings_page.py pet/chat/providers.py tests/test_acp_settings_ui.py
git commit -m "feat(chat): add ACP agent configuration UI and handshake probe to AI settings"
```

---

### Task 6: Cross-Platform Packaging Scripts (macOS `.app` & Windows `.exe`)

**Files:**
- Modify: `scripts/build_macos.sh`
- Modify: `scripts/build_onedir.ps1`
- Modify: `scripts/build_linux.sh`
- Create: `tests/test_acp_packaging.py`

**Interfaces:**
- Consumes: All modules under `pet/chat/acp/*`, `pet/chat/acp_provider.py`, `pet/chat/permission_card.py`, `pet/chat/acp_settings_section.py`.
- Produces:
  - Explicit `ACP_HIDDEN` hidden-import list in `scripts/build_macos.sh`, `scripts/build_onedir.ps1`, and `scripts/build_linux.sh` included ONLY on chat-enabled variants (`webm-chat`, `gif-chat`), omitted on `no-chat` variants (`webm`, `gif` which pass `--exclude-module pet.chat`).
  - macOS `.app` build supports local ad-hoc signing (`codesign --deep --force -s -`) without App Sandbox entitlements blocking child CLI execution.

- [ ] **Step 1: Write the failing test in `tests/test_acp_packaging.py`**

Verify statically that `scripts/build_macos.sh`, `scripts/build_onedir.ps1`, and `scripts/build_linux.sh` each declare all ACP modules for chat-enabled variants and preserve `--exclude-module pet.chat` for `no-chat` variants.

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_packaging.py -v`
Expected: FAIL

- [ ] **Step 3: Update `scripts/build_macos.sh`, `scripts/build_onedir.ps1`, and `scripts/build_linux.sh`**

Read each script carefully before editing so existing flags, slimming, and `no-chat` paths remain intact.

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_acp_packaging.py tests/test_architecture.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/build_macos.sh scripts/build_onedir.ps1 scripts/build_linux.sh tests/test_acp_packaging.py
git commit -m "build: wire ACP chat modules into macOS, Windows, and Linux packaging scripts"
```

---

### Task 7: End-to-End Verification, Live 4-Agent Smoke Test & Standalone macOS `.app` Build

**Files:**
- Create: `scripts/smoke_test_acp_agents.py` (standalone manual/CI verification helper, zero changes to runtime code)

- [ ] **Step 1: Run `ruff` and full chat + architecture + ACP pytest suite**

Run:
```bash
.venv/bin/ruff check pet/ tests/
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_architecture.py tests/test_acp_*.py tests/test_chat_*.py -q
```
Expected: 0 ruff errors, all tests pass.

- [ ] **Step 2: Run live ACP smoke test against all 4 locally installed agents**

Create `scripts/smoke_test_acp_agents.py` that uses `detect_builtin_acp_providers()` + `AcpManager.probe_connection()` (and inspects real `initialize` + `session/new` + advertised modes/models) against Factory, Devin, DeepSeek Harness, and Codex under minimal Finder-like `PATH=/usr/bin:/bin:/usr/sbin:/sbin` to verify our PATH augmentation and real protocol compatibility on this Mac. Fix any real-world protocol quirks discovered in the 4 live CLIs (with regression tests!).

- [ ] **Step 3: Build standalone macOS Apple Silicon `.app` and verify launch**

Build the `webm-chat` (or `gif-chat` depending on repo assets) macOS `.app` bundle via `scripts/build_macos.sh`, verify the `.app` bundle contains `pet.chat.acp` and starts cleanly without import errors, and verify Finder-style minimal `PATH` still resolves local ACP agents.

- [ ] **Step 4: Final full verification & commit**

Run:
```bash
.venv/bin/ruff check pet/ tests/
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_architecture.py tests/test_acp_*.py tests/test_chat_*.py -q
```
Commit `scripts/smoke_test_acp_agents.py` and any live-verified protocol fixes.
