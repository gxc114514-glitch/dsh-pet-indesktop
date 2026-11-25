# -*- coding: utf-8 -*-
"""ACP provider controls for the modern AI settings page."""
from __future__ import annotations

import shlex

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from ..settings_widgets import (
    BrowserSpinBox,
    ModernSelect,
    SettingRow,
    SettingsSection,
    ToggleSwitch,
    _line_edit,
)
from .acp.presets import ACP_PRESETS, build_preset_provider
from .models import AcpAgentConfig


def _acp_config(value) -> AcpAgentConfig:
    if isinstance(value, AcpAgentConfig):
        return AcpAgentConfig.from_dict(value.to_dict())
    return AcpAgentConfig.from_dict(value if isinstance(value, dict) else {})


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = value.strip()
    return values


class AcpSettingsSection(QWidget):
    """Staged ACP launch/tone controls; no configuration is written here."""

    auto_detect_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loading = False

        self.acp_preset = ModernSelect(self, width=230)
        self.acp_preset.addItem("自定义命令", "custom")
        for preset_id, metadata in ACP_PRESETS.items():
            self.acp_preset.addItem(str(metadata["name"]), preset_id)
        self.acp_command = _line_edit()
        self.acp_command.setPlaceholderText("例如：droid 或 /path/to/agent")
        self.acp_args = _line_edit()
        self.acp_args.setPlaceholderText("例如：exec -o acp")
        self.acp_env = QPlainTextEdit(self)
        self.acp_env.setPlaceholderText("每行一个 KEY=VALUE")
        self.acp_env.setMinimumSize(240, 72)
        self.acp_default_cwd = _line_edit()
        self.acp_default_cwd.setPlaceholderText("留空则使用当前工作目录")
        self.acp_tone_enabled = ToggleSwitch(self)
        self.acp_tone_prompt = QPlainTextEdit(self)
        self.acp_tone_prompt.setMinimumSize(240, 88)
        self.acp_idle_timeout = BrowserSpinBox(self)
        self.acp_idle_timeout.setRange(0, 1440)
        self.acp_idle_timeout.setSuffix(" 分钟")
        self.acp_idle_timeout.setSpecialValueText("不自动关闭")
        self.auto_detect = QPushButton("自动检测内置 Agent", self)
        self.auto_detect.setProperty("variant", "ghost")

        self.rows = [
            SettingRow(
                "acp_preset",
                "Agent 预设",
                "选择内置 Agent 会填入安全启动参数；也可使用自定义命令。",
                self.acp_preset,
                self,
            ),
            SettingRow(
                "acp_command",
                "启动命令",
                "本机 ACP Agent 的可执行文件或命令。",
                self.acp_command,
                self,
            ),
            SettingRow(
                "acp_args",
                "启动参数",
                "按命令行规则拆分；包含空格的单个参数可用引号包裹。",
                self.acp_args,
                self,
            ),
            SettingRow(
                "acp_env",
                "环境变量",
                "每行一个 KEY=VALUE；随 Agent 进程传递，不会写入聊天内容。",
                self.acp_env,
                self,
                stacked=True,
            ),
            SettingRow(
                "acp_default_cwd",
                "默认工作目录",
                "新建 Agent 会话使用的项目目录。",
                self.acp_default_cwd,
                self,
            ),
            SettingRow(
                "acp_tone_enabled",
                "桌宠语气提示",
                "仅在新建会话的第一轮注入一次，不会在后续轮次重复发送。",
                self.acp_tone_enabled,
                self,
            ),
            SettingRow(
                "acp_tone_prompt",
                "语气提示内容",
                "要求 Agent 保持技术结论严谨，同时用适合桌宠的简洁语气回复。",
                self.acp_tone_prompt,
                self,
                stacked=True,
            ),
            SettingRow(
                "acp_idle_timeout",
                "空闲自动关闭",
                "Agent 空闲超过该时长后结束进程；0 表示不自动关闭。",
                self.acp_idle_timeout,
                self,
            ),
            SettingRow(
                "acp_auto_detect",
                "检测内置 Agent",
                "添加缺少的内置 Agent 配置，不覆盖已有项；随设置保存生效。",
                self.auto_detect,
                self,
            ),
        ]
        self.section = SettingsSection("ACP Coding Agent", self.rows, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.section)

        self.acp_preset.currentIndexChanged.connect(self._on_preset_changed)
        self.auto_detect.clicked.connect(self.auto_detect_requested)
        self.load_from_draft({})

    def load_from_draft(self, draft: dict) -> None:
        config = _acp_config(draft.get("acp"))
        self._loading = True
        try:
            preset_id = config.preset
            if self.acp_preset.findData(preset_id) < 0:
                preset_id = f"acp-{config.preset}"
            self.acp_preset.setCurrentData(preset_id)
            if self.acp_preset.currentData() != preset_id:
                self.acp_preset.setCurrentData("custom")
            self.acp_command.setText(config.command)
            self.acp_args.setText(shlex.join(config.args))
            self.acp_env.setPlainText(
                chr(10).join(f"{key}={value}" for key, value in config.env.items())
            )
            self.acp_default_cwd.setText(config.default_cwd)
            self.acp_tone_enabled.setChecked(config.tone_enabled)
            self.acp_tone_prompt.setPlainText(config.tone_prompt)
            self.acp_idle_timeout.setValue(config.idle_timeout_minutes)
            self._startup_timeout = config.startup_timeout
            self._default_mode_id = config.default_mode_id
            self._default_model_id = config.default_model_id
        finally:
            self._loading = False

    def capture_to_draft(self, draft: dict) -> None:
        try:
            args = shlex.split(self.acp_args.text(), posix=True)
        except ValueError:
            args = self.acp_args.text().split()
        preset_id = str(self.acp_preset.currentData() or "custom")
        preset = preset_id.removeprefix("acp-") if preset_id in ACP_PRESETS else "custom"
        draft["acp"] = {
            "preset": preset,
            "command": self.acp_command.text().strip(),
            "args": args,
            "env": _parse_env(self.acp_env.toPlainText()),
            "default_cwd": self.acp_default_cwd.text().strip(),
            "default_mode_id": self._default_mode_id,
            "default_model_id": self._default_model_id,
            "tone_enabled": self.acp_tone_enabled.isChecked(),
            "tone_prompt": self.acp_tone_prompt.toPlainText().strip(),
            "startup_timeout": self._startup_timeout,
            "idle_timeout_minutes": int(self.acp_idle_timeout.value()),
        }

    def set_visible(self, visible: bool) -> None:
        self.setVisible(visible)
        self.section.setVisible(visible)
        for row in self.rows:
            row.setVisible(visible)
        if self.rows:
            self.section.card.refresh_separators()

    def _on_preset_changed(self, _index: int = -1) -> None:
        if self._loading:
            return
        preset_id = str(self.acp_preset.currentData() or "custom")
        if preset_id == "custom":
            return
        provider = build_preset_provider(preset_id)
        self.load_from_draft({"acp": provider.acp.to_dict()})
