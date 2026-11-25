from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .acp.protocol import AcpPermissionRequest


class PermissionCard(QFrame):
    """Inline approval request that retains a compact decision audit."""

    decided = Signal(str, str)

    def __init__(self, request: AcpPermissionRequest, parent=None):
        super().__init__(parent)
        self.request = request
        self.state = "pending"
        self.setObjectName("permission-card")
        self.setProperty("state", self.state)
        self.setAccessibleName(f"权限请求：{request.title}")
        self.setAccessibleDescription("选择允许或拒绝 Agent 请求的操作；可用 Tab 和回车操作。")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        heading.setSpacing(8)
        self.title_label = QLabel(request.title or "Agent 请求权限", self)
        self.title_label.setObjectName("permission-title")
        self.title_label.setWordWrap(True)
        heading.addWidget(self.title_label, 1)
        self.kind_label = QLabel(request.kind or "other", self)
        self.kind_label.setObjectName("permission-kind")
        self.kind_label.setToolTip(request.kind or "other")
        heading.addWidget(self.kind_label, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(heading)

        self.detail_label = QLabel(request.detail or "Agent 请求执行一项需要确认的操作。", self)
        self.detail_label.setObjectName("permission-detail")
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_label)

        self.options_widget = QWidget(self)
        self.options_widget.setObjectName("permission-options")
        options_layout = QHBoxLayout(self.options_widget)
        options_layout.setContentsMargins(0, 2, 0, 0)
        options_layout.setSpacing(8)
        options_layout.addStretch(1)
        self.option_buttons: list[QPushButton] = []
        for option in request.options:
            button = QPushButton(option.name or option.option_id, self.options_widget)
            button.setObjectName("permission-option")
            button.setProperty("optionId", option.option_id)
            button.setProperty("permissionKind", option.kind)
            button.setAccessibleName(f"{option.name}：{request.title}")
            button.setAccessibleDescription(option.kind or option.option_id)
            button.setMinimumHeight(28)
            button.clicked.connect(
                lambda _checked=False, option_id=option.option_id, name=option.name, kind=option.kind: self._decide(
                    option_id, name, kind
                )
            )
            options_layout.addWidget(button)
            self.option_buttons.append(button)
        layout.addWidget(self.options_widget)

        self.audit_label = QLabel(self)
        self.audit_label.setObjectName("permission-audit")
        self.audit_label.setWordWrap(True)
        self.audit_label.setAccessibleName("权限处理记录")
        self.audit_label.hide()
        layout.addWidget(self.audit_label)

    def _decide(self, option_id: str, name: str, kind: str) -> None:
        if self.state != "pending":
            return
        state = "denied" if str(kind).lower().startswith(("reject", "deny")) else "allowed"
        self.mark_resolved(name or option_id, state)
        self.decided.emit(self.request.request_id, option_id)

    def mark_resolved(self, label: str, state: str = "allowed") -> None:
        if state not in {"allowed", "denied", "cancelled"}:
            raise ValueError(f"Unsupported permission state: {state}")
        if self.state != "pending":
            return
        self.state = state
        self.setProperty("state", state)
        for button in self.option_buttons:
            button.setEnabled(False)
            button.hide()
        self.options_widget.hide()
        prefix = {"allowed": "已允许", "denied": "已拒绝", "cancelled": "已取消"}[state]
        self.audit_label.setText(f"{prefix} · {label}" if label else prefix)
        self.audit_label.setAccessibleDescription(self.audit_label.text())
        self.audit_label.show()
        self.style().unpolish(self)
        self.style().polish(self)


class ContextResetBanner(QFrame):
    """Centered divider explaining that following turns use a new Agent context."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = str(text)
        self.setObjectName("context-reset-banner")
        self.setAccessibleName("Agent 上下文重置通知")
        self.setAccessibleDescription(self.text)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(10)
        for index in range(2):
            line = QFrame(self)
            line.setObjectName("context-reset-divider")
            line.setFrameShape(QFrame.Shape.HLine)
            line.setFixedHeight(1)
            layout.addWidget(line, 1)
            if index == 0:
                label = QLabel(self.text, self)
                label.setObjectName("context-reset-label")
                label.setWordWrap(True)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setAccessibleDescription(self.text)
                layout.addWidget(label)
