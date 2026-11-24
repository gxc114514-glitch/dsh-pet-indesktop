# DSH-Pet Unified Multi-Agent ACP Desktop Pet — Design Document

Date: 2026-11-24  
Status: Ready for User Approval (builder-check completed and resolved)  
Target Repository (after approval): Fork/clone of `MerZlin/dsh-pet-indesktop` (will be moved to `docs/superpowers/specs/2026-11-24-dsh-pet-acp-design.md` once cloned)

---

## 1. Intent & Why

The user currently runs `CodexWhale` (a macOS-only Swift/AppKit desktop pet built around a single agent) alongside four separate coding agents installed locally: **Factory (Droid)**, **Devin**, **DeepSeek Harness (DSH)**, and **Codex**. Each agent lives in its own app or terminal, with no shared desktop presence, no unified chat surface, and no shared voice feedback.

We are replacing `CodexWhale` with a customized fork of **`MerZlin/dsh-pet-indesktop`** (Python + PySide6). The goal is a single cross-platform desktop pet (macOS Apple Silicon primary, Windows backup) that acts as a **unified interactive front-end for all four coding agents** while preserving `dsh-pet-indesktop`'s desktop pet animations, speech bubbles, TTS voice readout, and existing OpenAI-compatible chat mode.

**Why this matters:**
- One desktop companion stays visible across all coding sessions, reacts to thinking / tool execution / approval waiting / completion / errors, and reads completed replies aloud through the existing TTS pipeline.
- All four target agents natively speak (or ship an official adapter for) the **Agent Client Protocol (ACP)** over JSON-RPC stdio, letting a single client implementation drive all four with consistent streaming, tool progress, mode/model selection, cancellation, and interactive permission approvals.

---

## 2. Work Size

**Full project** (multi-module feature addition and standalone packaging on top of an existing PySide6 application: ACP protocol/process layer, provider/service integration, session/config schema extensions, Modern Chat UI additions including mode/model selectors and approval cards, AI settings UI, and macOS `.app` + Windows `.exe` standalone packaging).

---

## 3. Goals, Non-Goals, and Anti-Goals

### Goals (v1)
1. **Unified multi-agent chat in the desktop pet**: Switch between Factory (Droid), Devin, DeepSeek Harness (DSH), Codex, and existing OpenAI-compatible HTTP chat providers inside `dsh-pet-indesktop`'s Modern chat window.
2. **Full interactive agent sessions (single active turn in v1)**:
   - Select a **working directory (workspace)** per chat session.
   - Expose **ACP Mode** (e.g., Plan / Ask / Auto-Edit / approval posture) and **Model** selector dropdowns directly in the Modern chat window header, populated dynamically from each agent's ACP capabilities.
   - Stream assistant text, reasoning/thought chunks, and tool execution status updates live into the chat bubble.
   - Render **in-chat permission approval cards** (Allow once / Allow always / Deny, mapped from the agent's ACP `PermissionOption` list) whenever an agent invokes `session/request_permission`.
   - Support interrupting an in-flight turn (`session/cancel`) via the existing Stop button.
   - Single active turn at a time in v1 (switching sessions or agents while a turn is running prompts the user to stop the current turn first or blocks switching until idle).
3. **Preserved pet animations, voice-friendly tone & TTS playback**:
   - Inject a **light, configurable pet-tone preamble on the first turn** of a newly created ACP session so replies stay concise, friendly, and pleasant when read aloud via TTS—without interfering with coding accuracy or injecting bulky companionship memory.
   - Drive pet states (`thinking`, `streaming`, `success`, `error`, plus visual/bubble feedback when awaiting permission approval) via `PetChatLink`.
   - Automatically read completed ACP replies aloud through `ChatReplyReader` (`ChatService.register_global_finished`), reusing Markdown cleanup and configured TTS providers.
4. **macOS `.app` & Windows `.exe` standalone delivery**:
   - Build and verify a standalone **macOS Apple Silicon `.app`** bundle locally (able to spawn local CLIs cleanly even when launched from Finder/Dock with a minimal `PATH`), alongside synchronized **Windows `.exe`** build configuration/scripts (plus `no-chat` packaging variant compatibility).
   - Pre-configure sensible local launch presets with auto-detection for macOS paths and fallback `PATH` / Windows commands, all editable in AI Settings.
5. **Upstream maintainability**:
   - Keep all new code modular under `pet/chat/` (specifically `pet/chat/acp/`), respecting `tests/test_architecture.py` guardrails so upstream `dsh-pet-indesktop` updates remain easy to merge.

### Non-Goals (explicitly deferred from v1)
- **Usage/quota cards and weekly reports** (explicitly deferred by user).
- **Merging with or migrating `CodexWhale`** (`CodexWhale` is deprecated and kept only as historical reference).
- **Concurrent multi-session background agent execution** (confirmed single active turn for v1; multi-turn concurrency deferred to v2).
- **Classic (`legacy_widgets.py`) chat window parity**: ACP interactive UI (workspace selector, mode/model dropdowns, approval cards) targets the Modern chat window (`pet/chat/widgets.py`); Classic window stays unchanged and continues to work for OpenAI-compatible providers. *(Author's call — see Section 9)*
- **Embedded terminal emulator / full diff viewer**: Tool activity shows concise status lines and command/path summaries; full terminal rendering and side-by-side git diff UI are out of scope for v1.
- **ACP client-side `fs/*` and `terminal/*` delegation**: In v1, the client advertises `fs: {readTextFile: false, writeTextFile: false}` and `terminal: false` during `initialize`, so each agent uses its own native filesystem/shell tooling (which all four CLIs support out of the box). *(Author's call — see Section 9)*
- **Built-in OAuth/login wizards inside the pet**: Agents rely on their existing local CLI/app login state; if an agent reports authentication required, the pet surfaces a clear actionable error telling the user how to authenticate that CLI.

### Anti-Goals (failure modes even if technically working)
- **Blocking the Qt GUI thread** on subprocess startup, JSON-RPC reads/writes, or waiting for user approval.
- **Orphaned agent subprocesses** left running after the user quits the pet or crashes out of a session.
- **Killing live ACP agent sessions just because the user closed the chat window** (`pet/chat/lifecycle.py` destroys `ChatWindow` and `ChatService` on close to reclaim RAM).
- **Breaking existing OpenAI-compatible chat, TTS, `no-chat` bundles, or `tests/test_architecture.py`**, including `WINDOW_PY_LINE_BUDGET`, `MODERN_SETTINGS_DIALOG_PY_LINE_BUDGET`, or the `no-chat` top-level import guard.
- **Silent auto-approval of destructive actions** without the user explicitly selecting an auto-approve mode or clicking Allow.

---

## 4. Constraints & Verified Environment

### Local Development & Primary Target (Verified)
- **OS / Arch**: macOS 15 (`darwin 24.6.0`), Apple Silicon (`arm64`), 8 CPUs, 16 GB RAM.
- **Python**: `/opt/homebrew/bin/python3.15` (Python 3.15.2). PySide6 `6.12.0` officially publishes Python 3.15 universal2 macOS and Windows wheels.
- **Node**: `/Users/gxc/.nvm/versions/node/v20.20.2/bin/{node,npm,npx}` (plus Node `24.21.0` bundled by DSH Desktop — see Section 6.3).
- **Target Agents Verified Locally**:
  1. **Factory (Droid)** v1.18.0: `/Applications/Factory.app/Contents/Resources/bin/droid`
  2. **Devin** v3000.12.43: `/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin`
  3. **DeepSeek Harness (DSH)** `0.2.1-rc.2`: `/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js` + bundled Node `24.21.0` at `/Users/gxc/.dsh/dsh-runtimes/dsh-plugin-desktop-primary-runtime/dependencies/node` (note: DSH Desktop `package.json` requires `node ^22.19.0 || >=24.0.0`, so using its bundled Node 24 avoids conflicting with the user's nvm Node 20!).
  4. **Codex** v0.171.0-alpha.13: `/Users/gxc/.local/bin/codex`, bridged via official `@agentclientprotocol/codex-acp` (`npx -y @agentclientprotocol/codex-acp` with `CODEX_PATH=/Users/gxc/.local/bin/codex`).

### Upstream `dsh-pet-indesktop` Architectural Constraints (Verified)
1. **Close-destroys-window (`pet/chat/lifecycle.py`)**: Closing `ChatWindow` detaches `ChatService` and calls `deleteLater()` once in-flight `QThread` workers exit. Therefore, long-lived ACP agent processes **must not** be owned by `ChatWindow` or `ChatService`.
2. **Architecture guardrails (`tests/test_architecture.py`)**:
   - `pet/window.py` and `pet/modern_settings_dialog.py` have strict line budgets (`WINDOW_PY_LINE_BUDGET`, `MODERN_SETTINGS_DIALOG_PY_LINE_BUDGET`). Do not bloat them; keep ACP code inside `pet/chat/`.
   - `pet/modern_settings_dialog.py` must have **zero top-level imports** of `pet.chat.*` (`no-chat` packaging build guard). All chat/ACP settings UI stays inside `pet/chat/ai_settings_page.py` (or a new submodule imported by it).
   - Settings UI controls must use `SettingRow`.
   - Config keys for ACP must stay nested inside the existing `chat` config dict (`ChatSettings`), avoiding unnecessary top-level config key registration churn.
3. **Qt threading**: All cross-thread worker → GUI signals must use `Qt.ConnectionType.QueuedConnection`. No `QWidget` access from background threads.
4. **Voice & Animation hooks**:
   - `ChatReplyReader` (`pet/chat_reply_voice.py`) subscribes to `ChatService.register_global_finished(cb)`. Replies completing via `ChatService._finished()` automatically trigger voice readout.
   - `PetChatLink` (`pet/chat/pet_link.py`) drives `pet_window.set_chat_status(state, text)` (`thinking`, `streaming`, `success`, `error`).

---

## 5. Approaches Considered & Decision

### Base Architecture Decision (Confirmed by User: Route B)
- **Route A (Rejected): Merge `CodexWhale` and `dsh-pet-indesktop` or port features into Swift `CodexWhale`.**
  - *Trade-off*: macOS-only (violates Windows backup goal), requires rewriting TTS, chat UI, and animation systems already mature in `dsh-pet-indesktop`.
- **Route B (Selected): Fork and customize `MerZlin/dsh-pet-indesktop` (Python + PySide6); deprecate `CodexWhale`.**
  - *Why*: Cross-platform macOS + Windows out of the box, rich animation/speech-bubble/TTS/chat infrastructure already built, clean `pet/chat/` extension seam.

### Agent Integration Architecture (Confirmed by User: Approach 1)
1. **Approach 1 (Selected): Embedded Unified ACP Client in `pet/chat/acp/`**
   - Pure Python JSON-RPC 2.0 over newline-delimited stdio inside the desktop pet process, managing long-lived agent subprocesses via an app-level `AcpManager`, exposed to `ChatService` through an `AcpProvider` alongside `OpenAICompatibleProvider`.
   - *Pros*: Single process tree (no separate daemon to install/monitor), zero extra port/auth surface, native bidirectional `session/request_permission` and `session/set_mode`/`session/set_model` support, full streaming and `session/cancel`, shares `ChatService` → `PetChatLink` & `ChatReplyReader`.
   - *Cons*: Must carefully isolate subprocess lifecycle from `ChatWindow` destruction and handle per-agent launch quirks cleanly via presets.
2. **Approach 2 (Rejected): Separate Local HTTP/WebSocket Bridge Daemon**
   - *Trade-off*: Adds a second runtime/service lifecycle, port management, and packaging overhead on both macOS and Windows with no user benefit for a single-user local pet.
3. **Approach 3 (Rejected): Stateless One-Shot CLI Invocation Per Turn**
   - *Trade-off*: Cannot support interactive mid-turn permission approval cards or live ACP mode/model switching cleanly, incurs heavy cold-start latency on every message, and requires four completely different CLI flag/JSON parsers instead of one ACP protocol.

---

## 6. Architecture & Design (Decided Parts)

### 6.1 High-Level Component Layout

All new ACP modules live under `pet/chat/acp/` (pure Python for protocol/transport/process management so unit tests run without Qt, plus thin Qt integration glue in `pet/chat/`):

```text
pet/chat/
├── acp/
│   ├── __init__.py
│   ├── protocol.py       # Pure-Python ACP JSON-RPC 2.0 framing, dataclasses, update/permission/mode/model/config parsers
│   ├── transport.py      # Subprocess stdio reader/writer threads, request/response correlation, stderr ring buffer
│   ├── client.py         # AcpClient: initialize, session/new, session/load, session/set_mode, session/set_model, session/set_config_option, session/prompt, session/cancel, permission callback
│   ├── manager.py        # App-level AcpManager: pools live AcpClient instances per (provider_id, workspace_dir), idle reaper, shutdown
│   └── presets.py        # Built-in 4-agent presets + macOS/Windows executable auto-detection & GUI PATH augmentation
├── acp_provider.py       # AcpProvider: bridges AcpManager <-> ChatService worker streaming, mode/model metadata, first-turn tone preamble, permission future
├── permission_card.py    # Modern UI inline approval widget (Allow / Allow Always / Deny + tool details)
├── models.py             # Extended ProviderConfig (kind='openai'|'acp', AcpAgentConfig) & ChatSession (workspace_dir, acp_session_id, acp_mode_id, acp_model_id)
├── service.py            # Extended ChatService: permission_requested / session_meta_updated / context_reset signals, resolve_permission(), set_session_mode/model, provider dispatch, skip memory_extract for ACP
├── widgets.py            # Modern ChatWindow: provider/agent switcher, workspace picker bar, dynamic mode/model dropdowns, inline approval card & context-reset notice
└── ai_settings_page.py   # ACP agent configuration rows (using SettingRow): preset picker, command/args/env, default workspace, tone preamble toggle/text, idle timeout, test handshake
```

### 6.2 Protocol & Transport Contract (ACP over JSON-RPC 2.0 Stdio)

- **Framing**: Newline-delimited JSON-RPC 2.0 over UTF-8 `stdin`/`stdout` (line-buffered). `stderr` is drained continuously by a dedicated daemon thread into a bounded ring buffer (e.g., last 200 lines) for diagnostics and never blocks the child process.
- **Handshake (`initialize`)**:
  - Client sends `protocolVersion: 1`, `clientInfo: {name: "dsh-pet", version: ...}`, and `clientCapabilities: {fs: {readTextFile: false, writeTextFile: false}, terminal: false}`.
  - Stores negotiated `agentCapabilities` (specifically `loadSession` support) and `agentInfo`.
- **Session Setup, Resumption & Mode/Model Discovery**:
  - Each `ChatSession` tied to an ACP provider stores `workspace_dir` (absolute path, defaulting to the provider's `default_cwd` or user home), `acp_session_id`, and persisted `acp_mode_id` / `acp_model_id`.
  - Session creation/resumption flow (triggered either eagerly when preparing a session to populate mode/model dropdowns, or lazily before the first `session/prompt`):
    1. Acquire/start the `AcpClient` for `(provider_id, workspace_dir)` from `AcpManager`.
    2. If `acp_session_id` is not yet live in this `AcpClient`:
       - If `acp_session_id` exists from a previous run and the agent advertises `loadSession`, attempt `session/load(sessionId, cwd, mcpServers=[])`.
       - If `session/load` is unsupported or fails, fall back to `session/new(cwd, mcpServers=[])`, save the new `acp_session_id` onto the `ChatSession`, and emit a `context_reset` signal so the UI shows an explicit inline notice (`"已开启新的 Agent 上下文（此前记录仅作本地展示）"`) without auto-replaying raw transcripts. *(Author's call — see Section 9)*
       - If no prior `acp_session_id` exists, call `session/new(cwd, mcpServers=[])` and persist `acp_session_id`.
    3. Parse session metadata returned by `session/new` / `session/load` (supporting both ACP `modes` / `models` structures and newer `configOptions` entries with category `mode` / `model`):
       - Emit `session_meta_updated(available_modes, current_mode_id, available_models, current_model_id)` to populate the Modern Chat Window header dropdowns.
       - If the session (or provider default) specifies a saved `acp_mode_id` or `acp_model_id` that differs from the agent's initial value and is present in the advertised list, apply it via `session/set_mode` / `session/set_model` (or `session/set_config_option` when exposed via `configOptions`).
  - **Live Mode / Model Switching**:
    - Selecting a mode or model in the header dropdown calls `session/set_mode` / `session/set_model` (or `session/set_config_option`) on the live `AcpClient` (guarded so switching is disabled while a turn is actively streaming), updates `ChatSession.acp_mode_id` / `acp_model_id`, and honors incoming `current_mode_update` / `config_option_update` notifications from the agent.
  - **Safe Default Approval Posture**:
    - Presets do **not** pass unattended auto-approve flags (e.g., no `--skip-permissions-unsafe` or `--yolo` by default). If an agent defaults to an interactive/ask mode, permission prompts appear in-chat; users who want looser autonomy can explicitly pick an auto-edit/bypass mode from the header Mode dropdown or customize CLI args in AI Settings.
- **First-Turn Light Pet Tone Preamble**:
  - Confirmed by user: on the **first turn** sent to a freshly created ACP session (`session/new`, tracked per live `acp_session_id`), if the ACP pet-tone option is enabled (default: **enabled**), prepend a concise system/style note to the prompt text (e.g., instructing the agent to keep final replies concise, friendly, natural for desktop-pet TTS voice playback, and free of redundant filler, while keeping code edits and technical accuracy 100% rigorous).
  - Do **not** inject upstream's heavy companionship memory (`memory_recall.py`) or full roleplay system prompt into ACP coding sessions, and do **not** repeat the preamble on subsequent turns or on `session/load` resumption.
  - Provide a toggle (`acp_tone_enabled`) and editable short prompt (`acp_tone_prompt`) in AI Settings so the user can tune or turn it off anytime.
- **Streaming `session/update` Mapping**:
  - `agent_message_chunk` (text content) → `ChatService.delta` (rendered live in the assistant Markdown bubble, accumulated for final `finished` text and TTS voice playback).
  - `agent_thought_chunk` (text content) → `StreamEvent(kind="reasoning", text=...)` → `ChatService.activity` (reuses existing collapsible thinking UI).
  - `tool_call` / `tool_call_update` / `plan` → `StreamEvent(kind="status", text=...)` → `ChatService.activity` (e.g., `"🔧 Running: pytest tests/"`, `"📄 Editing: pet/chat/service.py [completed]"`).
  - `current_mode_update` / `config_option_update` → `ChatService.session_meta_updated` (keeps header dropdowns synchronized if the agent changes mode itself, e.g., exiting plan mode).
- **Cancellation**:
  - Clicking Stop calls `ChatService.stop()` → sends ACP notification `session/cancel(sessionId)` to the live agent (and resolves any pending permission request as `cancelled`), without killing the underlying agent process unless it fails to settle within a bounded timeout.

### 6.3 Built-in Agent Launch Presets (Verified Local Paths + Cross-Platform Fallbacks)

Each ACP agent is represented as a `ProviderConfig` with `kind = "acp"` and an `AcpAgentConfig` block (`preset`, `command`, `args`, `env`, `default_cwd`, `default_mode_id`, `default_model_id`, `tone_enabled`, `tone_prompt`, `startup_timeout`, `idle_timeout_minutes`). All fields are editable in AI Settings, with an **"Auto-detect / Reset Preset"** button.

| Agent (`provider_id`) | macOS Auto-Detected Default | Windows / Generic Fallback | Notes |
|---|---|---|---|
| **Factory (Droid)** (`acp-factory`) | `/Applications/Factory.app/Contents/Resources/bin/droid` with args `["exec", "-o", "acp"]` | `droid` (from `PATH`) with args `["exec", "-o", "acp"]` | Verified v1.18.0 native ACP |
| **Devin** (`acp-devin`) | `/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin` with args `["acp"]` | `devin` (from `PATH`) with args `["acp"]` | Verified v3000.12.43 native ACP |
| **DeepSeek Harness** (`acp-dsh`) | `~/.dsh/dsh-runtimes/dsh-plugin-desktop-primary-runtime/dependencies/node` with args `["/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js", "--profile", "acp"]` (falls back to `dsh --profile acp` on `PATH`) | `dsh` (from `PATH`) with args `["--profile", "acp"]` | Uses DSH Desktop's bundled Node 24.21.0 so nvm Node 20 does not break DSH's `>=22.19` requirement |
| **Codex** (`acp-codex`) | `npx` (resolved via nvm/PATH) with args `["-y", "@agentclientprotocol/codex-acp"]`, env `{"CODEX_PATH": "~/.local/bin/codex"}` (if binary exists) | `npx.cmd` (or `npx`) with args `["-y", "@agentclientprotocol/codex-acp"]` | Official ACP adapter for Codex CLI |

**Environment Hygiene (`PATH` on macOS Finder/Dock `.app` Launch & Windows)**:
- Frozen macOS `.app` bundles launched from Finder/Dock inherit only `/usr/bin:/bin:/usr/sbin:/sbin`. `presets.py` / `transport.py` augments child process `PATH` with common user locations (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, active `~/.nvm/versions/node/*/bin`, `~/.cargo/bin`, and the parent directory of the resolved executable) when not already present, without overriding explicit user `env` entries.
- On Windows, resolve `.cmd` / `.exe` shims via `shutil.which` and spawn with `CREATE_NO_WINDOW` (so launching `npx.cmd` or Node CLIs never flashes an ugly black console window!).

### 6.4 Process, Turn & Window Lifecycle (Decoupled from Close-Destroys-Window)

Because `pet/chat/lifecycle.py` destroys `ChatWindow` and `ChatService` on close:
1. **`AcpManager` lives at process scope** (owned by `AppShell` or lazy singleton hooked into `QApplication.aboutToQuit`).
2. **Single-active-turn enforcement (v1)**:
   - `ChatService` remains strictly single-active-turn (matching its existing `_worker` architecture).
   - While a turn is running or waiting on a permission card, the sidebar session switch, provider switcher, workspace picker, and mode/model dropdowns either show a brief confirmation prompt (`"当前 Agent 正在运行，是否停止当前回合再切换？"`) or remain disabled until the user clicks Stop or the turn completes.
3. **Pool key**: `(provider_id, normalized_workspace_dir)`. Keeping one live subprocess per active `(agent, workspace)` gives instant multi-turn responsiveness and lets the user close and reopen the chat window without losing the live ACP session.
4. **Window close while a turn is running**:
   - Closing `ChatWindow` triggers `ChatService.stop()` (which sends `session/cancel` and unblocks any pending permission wait as `cancelled`), waits for the worker thread via `release_window_when_idle`, and leaves the idle `AcpClient` warm in `AcpManager` for quick reopen.
5. **Idle reaping & App exit**:
   - An idle client with no active turn for `idle_timeout_minutes` (default: **30 minutes**, configurable; `0` = never reap) is gracefully terminated (`stdin` closed → SIGTERM → SIGKILL after 3s). *(Author's call — see Section 9)*
   - On `QApplication.aboutToQuit`, `AcpManager.shutdown_all()` terminates all live subprocesses (and process groups on POSIX / `taskkill /T` or job tree kill on Windows) within a bounded timeout so zero orphan processes remain.
6. **Switching workspace mid-session**:
   - A session is bound to one `workspace_dir` once the first message has been sent. Changing workspace starts a **new session** (or, before the first message is sent, updates the draft session's `workspace_dir` in place). *(Author's call — see Section 9)*

### 6.5 Interactive Permission Approval Flow

When an ACP agent sends a JSON-RPC request `session/request_permission` (containing `sessionId`, `toolCall` metadata, and `options: [{optionId, name, kind}]`):

```text
Agent stdout ──▶ AcpClient (reader thread)
                    │  creates PendingPermission(request_id, tool_summary, options, event)
                    ▼
                 AcpProvider ──▶ _Worker emits permission_requested(payload) [QueuedConnection]
                    │
                    ▼
                 ChatService.permission_requested ──▶ Modern ChatWindow
                    │                                   renders inline PermissionCard
                    │                                   PetChatLink → bubble prompt
                    ▼
                 User clicks [Allow once] / [Allow always] / [Deny]
                    │
                    ▼
                 ChatService.resolve_permission(request_id, outcome)
                    │  sets result & signals threading.Event
                    ▼
                 AcpClient writes JSON-RPC response {outcome: {outcome: "selected", optionId}} ──▶ Agent stdin
```

**Edge Cases Handled**:
- **Stop button / window close while approval card is open**: Pending permission is automatically resolved with `{outcome: {outcome: "cancelled"}}` and the card updates to a muted "Cancelled" state so the reader/worker thread never deadlocks.
- **Multiple sequential approvals in one turn**: Each permission request has a unique `request_id`; once resolved, the card collapses into a compact one-line audit badge (e.g., `✓ Allowed: Run pytest tests/`) and remains visible in the message activity log.
- **No silent timeout approval**: Never auto-approve on timeout. If the agent itself cancels or disconnects, the card automatically dismisses and shows the disconnected/cancelled state.

### 6.6 Modern Chat Window UI & Settings Changes

#### Modern Chat Window (`pet/chat/widgets.py` + `pet/chat/permission_card.py`)
Sketch of the updated chat header (with Mode & Model dropdowns) and inline approval card:

```text
┌────────────────────────────────────────────────────────────────┐
│ [Agent: Factory ▾] [Mode: Ask ▾] [Model: Default ▾]     [New]  │
│ [📁 ~/Projects/my-app ▾]                                       │
├────────────────────────────────────────────────────────────────┤
│ ── 已开启新的 Agent 上下文（历史记录仅作本地展示） ──           │
│                                                                │
│ User: Run the test suite and fix the failing auth test.        │
│                                                                │
│ ▸ Thinking... (collapsible, existing reasoning widget)         │
│ ▸ Activity: Reading tests/test_auth.py                         │
│                                                                │
│ ┌─ Permission Required ──────────────────────────────────────┐ │
│ │ Tool: Execute shell command                                │ │
│ │ Detail: pytest tests/test_auth.py                          │ │
│ │   [ Allow Once ]  [ Allow Always ]  [ Deny ]               │ │
│ └────────────────────────────────────────────────────────────┘ │
│                                                                │
│ Assistant: 修好了 auth.py 里的过期校验，测试全绿啦。            │
├────────────────────────────────────────────────────────────────┤
│ [ Type a message...                               ] [Send/Stop]│
└────────────────────────────────────────────────────────────────┘
```

- **Agent / Provider Switcher** in the chat header: Lists all configured providers (`OpenAI-compatible` and `ACP` agents with a subtle `ACP` / `API` badge). Switching provider creates a new draft session (or switches an empty draft session) bound to that provider.
- **Mode & Model Dropdowns** (shown only when `kind == "acp"` and the connected agent advertises modes/models or config options):
  - Populated asynchronously once the ACP session initializes (showing a subtle loading/default state before handshake completes).
  - Gracefully hidden if a specific agent does not advertise modes or models via ACP.
  - Disabled while a turn is actively in flight to prevent mid-turn protocol races.
- **Workspace Picker Bar** (shown only when `kind == "acp"`): Displays the shortened folder path with a folder-picker button (`QFileDialog.getExistingDirectory`) and a dropdown of recently used workspaces (persisted in `ChatSettings.recent_workspaces`, capped at 10).
- **Session Sidebar**: Displays the agent badge and workspace folder name on ACP sessions so users can distinguish sessions across projects and agents.
- **AI Settings Page (`pet/chat/ai_settings_page.py`)**:
  - Extends provider management with a Provider Type (`OpenAI-Compatible API` vs `ACP Coding Agent`).
  - For ACP providers, renders `SettingRow`-wrapped controls: Preset template selector, Command, Arguments, Environment variables (`KEY=VALUE`), Default Working Directory, First-Turn Pet Tone toggle + editable tone prompt, Idle Timeout (minutes), plus an **"Auto-detect Local Agents"** action and **"Test Handshake"** button (runs `initialize` + `session/new` probe and displays agent name/version, supported modes/models, or error + stderr excerpt).

### 6.7 Pet Animation & Voice Readout Integration

- **Pet Animations (`PetChatLink`)**:
  - `ChatService.started` → `thinking`
  - `ChatService.delta` (first text token) → `streaming`
  - `ChatService.permission_requested` → keep pet alert with a short speech bubble notice (e.g., `"需要你确认权限～"`) without breaking existing `set_chat_status` API.
  - `ChatService.finished` → `success`
  - `ChatService.error` → `error`
  - `ChatService.stopped` → idle/reset (existing behavior).
- **Voice Readout (`ChatReplyReader`)**:
  - Because `AcpProvider` streams assistant text through `ChatService` and emits `finished(request_id, final_text)` on turn completion, `ChatService._global_finished_listeners` fires automatically — **zero changes needed in `pet/chat_reply_voice.py`** for basic TTS playback!
  - Tool activity and reasoning go through `activity` (not final text), so the pet speaks only the assistant's actual reply, not raw tool logs.
  - **Memory Extraction Guard**: `ChatService._maybe_extract_memory()` skips running `memory_extract_hook` when `config.kind == "acp"`, because ACP providers do not implement stateless OpenAI completion requests and coding sessions should not pollute pet companionship memory. *(Author's call — see Section 9)*

### 6.8 Error Handling & Diagnostics

1. **Executable not found / permission denied**: Detected before spawn; emits a friendly actionable error message pointing to AI Settings (and noting how to install or re-detect the agent).
2. **Startup / handshake timeout**: If `initialize` does not respond within `startup_timeout` (default 20s, 45s for `npx`-based Codex cold cache), terminate the process and include the last 10 lines of `stderr` in the error message.
3. **Agent crash mid-turn**: Reader thread detects EOF on `stdout`, rejects pending JSON-RPC requests, emits `ChatService.error` with exit code + `stderr` tail, and evicts the dead `AcpClient` from `AcpManager` so the next user message cleanly spawns a fresh process.
4. **Authentication error**: Surface the JSON-RPC error message verbatim (plus a hint to log into the agent's CLI/desktop app) rather than retrying in a loop.
5. **Empty text turn (tool-only turn)**: Unlike stateless LLM chat (where empty text is treated as an error in `ChatService._Worker`), an ACP agent may occasionally complete a turn with tool actions and no prose. If tool activity occurred and `session/prompt` returned `stopReason: "end_turn"` with empty assistant text, synthesize a brief completion note (e.g., `"已完成操作。"`) instead of showing a false error. *(Author's call — see Section 9)*

### 6.9 Standalone Packaging Delivery (macOS `.app` & Windows `.exe`)

Confirmed by user: v1 includes standalone app packaging alongside source/venv development:
1. **macOS `.app` (primary — built & smoke-tested directly on this Apple Silicon Mac)**:
   - Update/add macOS PyInstaller spec & build script (adapting upstream `dsh-pet-indesktop` build scripts if present) to produce a runnable `DSH-Pet.app` (`arm64`, Python 3.15 + PySide6 6.12).
   - Keep `pet.chat.acp.*` bundled in the full variant and cleanly excluded in the `no-chat` variant (zero top-level `pet.chat` import in `modern_settings_dialog.py`).
   - Do **not** enable macOS App Sandbox entitlements so the `.app` can spawn user-installed CLIs (`droid`, `devin`, DSH bundled Node 24, `npx`/`codex`) and access user-selected workspace directories cleanly; use ad-hoc signing (`codesign --deep --force -s -`) for local launch.
   - Verify the built `.app` launches from Finder / `open DSH-Pet.app` (minimal login `PATH`) and still auto-detects and connects to all 4 local ACP agents.
2. **Windows `.exe` (backup — spec & build script synchronized)**:
   - Keep upstream Windows PyInstaller spec / build script updated with `pet.chat.acp.*` hidden imports (if needed) and `CREATE_NO_WINDOW` subprocess handling, plus `no-chat` exclusion parity.
   - Verify Windows-specific path/shim resolution and process-tree termination logic via unit tests on macOS; note that final Windows `.exe` binary compilation and smoke testing must be run on a Windows machine or Windows CI runner (since our local build host is macOS arm64).

---

## 7. Data Model Compatibility

- **`ProviderConfig`** (`pet/chat/models.py`):
  - Add `kind: str = "openai"` (values: `"openai"` | `"acp"`; defaults to `"openai"` when loading legacy configs).
  - Add `acp: AcpAgentConfig | None = None` (serialized under `"acp"` only when `kind == "acp"`).
  - Existing OpenAI configs load with zero migration and serialize identically.
- **`ChatSettings`** (`pet/chat/models.py`):
  - Add `recent_workspaces: list[str] = field(default_factory=list)` (optional, omitted if empty).
  - Seed default ACP presets (`acp-factory`, `acp-devin`, `acp-dsh`, `acp-codex`) lazily when no ACP providers exist yet, **only** enabling/auto-detecting paths without overwriting user-edited providers.
- **`ChatSession`** (`pet/chat/models.py`):
  - Add `workspace_dir: str = ""`, `acp_session_id: str = ""`, `acp_mode_id: str = ""`, and `acp_model_id: str = ""` (all omitted from `to_dict()` when empty, matching `ChatMessage.reasoning`/`activity` convention).

---

## 8. Testing & Verification Strategy

1. **Pure-Python ACP Protocol & Lifecycle Tests** (no Qt required):
   - Mock stdio ACP agent subprocess (Python script speaking real JSON-RPC 2.0 over stdio) verifying: `initialize`, `session/new`, `session/load` & fallback context-reset behavior, `session/set_mode`, `session/set_model`, `session/set_config_option`, `session/prompt` streaming (`agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, `current_mode_update`), first-turn tone preamble (injected once on `session/new`, skipped on turn 2+ and `session/load`), `session/request_permission` (allow / deny / cancel), `session/cancel`, stderr capture, crash recovery, and `AcpManager.shutdown_all()` leaving zero zombie processes.
2. **Qt / Service Integration Tests**:
   - `ChatService` + `AcpProvider` signal ordering (`started` → `session_meta_updated` / `activity` / `permission_requested` / `delta` → `finished`), single-active-turn guard on session/mode/model switching, `_global_finished_listeners` invocation for voice readout, `memory_extract_hook` skipped for ACP, and `release_window_when_idle` safety when closing mid-turn or mid-permission-prompt.
3. **Architecture Guardrail Suite**:
   - Full upstream `pytest tests/test_architecture.py` must pass green (line budgets, zero top-level `pet.chat` import in `modern_settings_dialog.py`, pure logic checks).
4. **Live Smoke Test & Frozen `.app` Verification on Local macOS Machine**:
   - Verify real ACP handshake (`initialize` + `session/new` + advertised modes/models inspection) against each of the 4 locally installed agents (Factory, Devin, DSH, Codex).
   - Verify end-to-end interactive turn (including permission approval card, mode/model switcher, and TTS voice playback) both from source virtualenv and from the built standalone `DSH-Pet.app` launched via `open`.

---

## 9. Author's Calls (Defaults Proposed for User Confirmation)

The following decisions were settled with sensible defaults rather than left open; please flag any you want changed:

1. **Modern Chat Window only for v1**: ACP workspace picker, mode/model dropdowns, and interactive permission cards are added to Modern Chat (`pet/chat/widgets.py`), not Classic (`legacy_widgets.py`).
2. **Agent-native file/shell tools in v1**: Client advertises `fs: false, terminal: false` in ACP `initialize` so each agent uses its own proven tools and sandboxing.
3. **Workspace bound per session**: Changing workspace after messages exist starts a new session; before the first message, it updates the draft session in place.
4. **Explicit notice on `session/load` fallback**: If an agent doesn't support `session/load` (or resumption fails after app restart / idle reap), start a clean `session/new` and show an inline notice (`"已开启新的 Agent 上下文（此前记录仅作本地展示）"`) rather than auto-injecting raw historical transcripts.
5. **Idle subprocess reaper = 30 minutes** (configurable in AI Settings; `0` disables reaping), plus immediate cleanup on app exit.
6. **Skip companionship memory read/write** (`memory_recall` / `memory_extract_hook`) for ACP coding sessions so code chats don't pollute pet persona memory (while still supporting the light, configurable first-turn pet-tone preamble).
7. **Tool-only turns succeed cleanly**: If an ACP turn runs tools and ends cleanly with no text chunk, display a short completion note rather than an empty-response error.

---

## 10. Left to the Builder

The implementation planner / builder may decide:
- Exact Qt widget layout, QSS styling tokens, icon choices, and animation polish for the workspace bar, mode/model dropdowns, and `PermissionCard` (matching `pet/chat/modern_styles.qss` light/dark themes).
- Exact wording of the default light first-turn pet-tone preamble (editable by user in AI Settings).
- Exact internal threading primitives (`queue.Queue` vs `threading.Event` / `concurrent.futures.Future`) inside `pet/chat/acp/transport.py` and `client.py`.
- Exact formatting of tool call activity summaries (truncation length, emoji/icon prefix, collapsing repeated updates for the same `toolCallId`).
- How `AcpManager` is initialized and wired into `AppShell` (e.g., passed down vs accessed via a clean getter registered with `QApplication.aboutToQuit`).
- Exact helper decomposition inside `pet/chat/ai_settings_page.py` (splitting an `acp_settings_section.py` helper if `ai_settings_page.py` gets crowded).
- Exact PyInstaller spec layout for macOS `.app` and Windows `.exe` builds (reusing upstream packaging scripts wherever present).
