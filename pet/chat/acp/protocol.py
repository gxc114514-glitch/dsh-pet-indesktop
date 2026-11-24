from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from typing import Any


class AcpError(RuntimeError):
    """Error raised by the ACP transport or protocol layer."""

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        stderr_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.code: int | None = code
        self.stderr_tail: str = stderr_tail


@dataclass
class AcpModeOption:
    mode_id: str
    name: str
    description: str = ""


@dataclass
class AcpModelOption:
    model_id: str
    name: str
    description: str = ""


@dataclass
class AcpSessionMeta:
    session_id: str
    modes: list[AcpModeOption] = field(default_factory=list)
    current_mode_id: str = ""
    models: list[AcpModelOption] = field(default_factory=list)
    current_model_id: str = ""
    mode_config_id: str = ""
    model_config_id: str = ""


@dataclass
class AcpPermissionOption:
    option_id: str
    name: str
    kind: str


@dataclass
class AcpPermissionRequest:
    request_id: str
    rpc_id: Any
    session_id: str
    tool_call_id: str
    title: str
    kind: str
    detail: str
    options: list[AcpPermissionOption]


@dataclass
class AcpUpdateEvent:
    kind: str  # 'text' | 'thought' | 'status' | 'meta'
    text: str = ""
    tool_call_id: str = ""
    session_meta: AcpSessionMeta | None = None


def _flatten_config_option_values(raw_options: Any) -> list[dict[str, Any]]:
    """Flatten ACP configOption ``options`` (handles flat items or grouped lists)."""
    if not isinstance(raw_options, list):
        return []
    flat: list[dict[str, Any]] = []
    for item in raw_options:
        if not isinstance(item, dict):
            continue
        nested = item.get("options")
        if isinstance(nested, list):
            for sub in nested:
                if isinstance(sub, dict):
                    flat.append(sub)
        else:
            flat.append(item)
    return flat


def _apply_config_options(
    meta: AcpSessionMeta, config_options: list[Any]
) -> AcpSessionMeta:
    modes = list(meta.modes)
    current_mode_id = meta.current_mode_id
    mode_config_id = meta.mode_config_id
    models = list(meta.models)
    current_model_id = meta.current_model_id
    model_config_id = meta.model_config_id

    for entry in config_options:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category") or "").strip().lower()
        opt_id = str(entry.get("id") or "")
        current_val = str(entry.get("currentValue") or "")
        flat_opts = _flatten_config_option_values(entry.get("options"))

        if category == "mode":
            if opt_id:
                mode_config_id = opt_id
            if current_val:
                current_mode_id = current_val
            if flat_opts:
                modes = [
                    AcpModeOption(
                        mode_id=str(o.get("value") or o.get("id") or ""),
                        name=str(
                            o.get("name") or o.get("value") or o.get("id") or ""
                        ),
                        description=str(o.get("description") or ""),
                    )
                    for o in flat_opts
                    if (o.get("value") or o.get("id"))
                ]
        elif category == "model":
            if opt_id:
                model_config_id = opt_id
            if current_val:
                current_model_id = current_val
            if flat_opts:
                models = [
                    AcpModelOption(
                        model_id=str(o.get("value") or o.get("id") or ""),
                        name=str(
                            o.get("name") or o.get("value") or o.get("id") or ""
                        ),
                        description=str(o.get("description") or ""),
                    )
                    for o in flat_opts
                    if (o.get("value") or o.get("id"))
                ]

    return replace(
        meta,
        modes=modes,
        current_mode_id=current_mode_id,
        mode_config_id=mode_config_id,
        models=models,
        current_model_id=current_model_id,
        model_config_id=model_config_id,
    )


def parse_session_meta(session_id: str, result: dict[str, Any]) -> AcpSessionMeta:
    """Parse ``session/new`` or ``session/load`` response into ``AcpSessionMeta``.

    Supports both legacy ``modes``/``models`` objects and newer ``configOptions``
    arrays (with flat or grouped option items).
    """
    raw = result if isinstance(result, dict) else {}
    sid = str(session_id or raw.get("sessionId") or "")

    modes: list[AcpModeOption] = []
    current_mode_id = ""
    modes_obj = raw.get("modes")
    if isinstance(modes_obj, dict):
        current_mode_id = str(modes_obj.get("currentModeId") or "")
        avail_modes = modes_obj.get("availableModes")
        if isinstance(avail_modes, list):
            for item in avail_modes:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or item.get("modeId") or "")
                if not mid:
                    continue
                modes.append(
                    AcpModeOption(
                        mode_id=mid,
                        name=str(item.get("name") or mid),
                        description=str(item.get("description") or ""),
                    )
                )

    models: list[AcpModelOption] = []
    current_model_id = ""
    models_obj = raw.get("models")
    if isinstance(models_obj, dict):
        current_model_id = str(models_obj.get("currentModelId") or "")
        avail_models = models_obj.get("availableModels")
        if isinstance(avail_models, list):
            for item in avail_models:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("modelId") or item.get("id") or "")
                if not mid:
                    continue
                models.append(
                    AcpModelOption(
                        model_id=mid,
                        name=str(item.get("name") or mid),
                        description=str(item.get("description") or ""),
                    )
                )

    meta = AcpSessionMeta(
        session_id=sid,
        modes=modes,
        current_mode_id=current_mode_id,
        models=models,
        current_model_id=current_model_id,
    )

    config_options = raw.get("configOptions")
    if isinstance(config_options, list):
        meta = _apply_config_options(meta, config_options)

    return meta


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if "text" in content and content["text"] is not None:
            return str(content["text"])
        if "content" in content:
            return _extract_content_text(content["content"])
        return ""
    if isinstance(content, list):
        parts = [_extract_content_text(item) for item in content]
        return "".join(p for p in parts if p)
    return ""


def parse_session_update(
    params: dict[str, Any],
    current_meta: AcpSessionMeta | None = None,
) -> list[AcpUpdateEvent]:
    """Parse an ACP ``session/update`` notification into zero or more events."""
    if not isinstance(params, dict):
        return []
    raw_update = params.get("update")
    update = raw_update if isinstance(raw_update, dict) else params
    session_id = str(
        params.get("sessionId")
        or (current_meta.session_id if current_meta is not None else "")
    )
    tag = str(update.get("sessionUpdate") or "")

    if tag == "agent_message_chunk":
        text = _extract_content_text(update.get("content"))
        if text:
            return [AcpUpdateEvent(kind="text", text=text)]
        return []

    if tag == "agent_thought_chunk":
        text = _extract_content_text(update.get("content"))
        if text:
            return [AcpUpdateEvent(kind="thought", text=text)]
        return []

    if tag in ("tool_call", "tool_call_update"):
        tool_call_id = str(update.get("toolCallId") or "")
        title = str(update.get("title") or update.get("name") or "")
        status = str(update.get("status") or "")
        if title and status:
            text = f"{title} ({status})"
        elif title:
            text = title
        elif status:
            text = status
        else:
            text = str(update.get("kind") or "tool")
        return [AcpUpdateEvent(kind="status", text=text, tool_call_id=tool_call_id)]

    if tag == "plan":
        entries = update.get("entries")
        parts: list[str] = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content = str(entry.get("content") or "").strip()
                status = str(entry.get("status") or "").strip()
                if content and status:
                    parts.append(f"[{status}] {content}")
                elif content:
                    parts.append(content)
        summary = " | ".join(parts) if parts else "Plan updated"
        return [AcpUpdateEvent(kind="status", text=summary)]

    if tag == "current_mode_update":
        new_mode_id = str(
            update.get("currentModeId") or update.get("modeId") or ""
        )
        base = (
            current_meta
            if current_meta is not None
            else AcpSessionMeta(session_id=session_id)
        )
        updated_meta = replace(base, current_mode_id=new_mode_id)
        return [
            AcpUpdateEvent(kind="meta", text=new_mode_id, session_meta=updated_meta)
        ]

    if tag == "config_option_update":
        base = (
            current_meta
            if current_meta is not None
            else AcpSessionMeta(session_id=session_id)
        )
        config_options = update.get("configOptions")
        if isinstance(config_options, list):
            updated_meta = _apply_config_options(base, config_options)
            return [AcpUpdateEvent(kind="meta", session_meta=updated_meta)]
        cfg_id = str(update.get("configId") or "")
        val = str(update.get("value") or "")
        if cfg_id and val:
            if cfg_id == base.mode_config_id:
                updated_meta = replace(base, current_mode_id=val)
                return [AcpUpdateEvent(kind="meta", text=val, session_meta=updated_meta)]
            if cfg_id == base.model_config_id:
                updated_meta = replace(base, current_model_id=val)
                return [AcpUpdateEvent(kind="meta", text=val, session_meta=updated_meta)]
        return []

    return []


def _format_permission_detail(tool_call: dict[str, Any], params: dict[str, Any]) -> str:
    raw_input = tool_call.get("rawInput")
    if raw_input is not None:
        if isinstance(raw_input, str):
            return raw_input
        try:
            return json.dumps(raw_input, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(raw_input)
    content = tool_call.get("content")
    if content is not None:
        text = _extract_content_text(content)
        if text:
            return text
    locations = tool_call.get("locations")
    if isinstance(locations, list) and locations:
        paths = [
            str(loc.get("path"))
            for loc in locations
            if isinstance(loc, dict) and loc.get("path")
        ]
        if paths:
            return ", ".join(paths)
    if params.get("detail"):
        return str(params["detail"])
    return ""


def parse_permission_request(
    rpc_id: Any, params: dict[str, Any]
) -> AcpPermissionRequest:
    """Parse an incoming ``session/request_permission`` JSON-RPC request."""
    raw = params if isinstance(params, dict) else {}
    tool_call = raw.get("toolCall") if isinstance(raw.get("toolCall"), dict) else {}
    session_id = str(raw.get("sessionId") or "")
    tool_call_id = str(tool_call.get("toolCallId") or raw.get("toolCallId") or "")
    title = str(tool_call.get("title") or raw.get("title") or "Permission request")
    kind = str(tool_call.get("kind") or raw.get("kind") or "other")
    detail = _format_permission_detail(tool_call, raw)

    options: list[AcpPermissionOption] = []
    raw_options = raw.get("options")
    if isinstance(raw_options, list):
        for opt in raw_options:
            if not isinstance(opt, dict):
                continue
            oid = str(opt.get("optionId") or opt.get("id") or "")
            if not oid:
                continue
            options.append(
                AcpPermissionOption(
                    option_id=oid,
                    name=str(opt.get("name") or oid),
                    kind=str(opt.get("kind") or ""),
                )
            )

    request_id = str(raw.get("requestId") or uuid.uuid4().hex)
    return AcpPermissionRequest(
        request_id=request_id,
        rpc_id=rpc_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        title=title,
        kind=kind,
        detail=detail,
        options=options,
    )


def format_permission_response(option_id: str | None) -> dict[str, Any]:
    """Build the JSON-RPC result payload for ``session/request_permission``."""
    if not option_id:
        return {"outcome": {"outcome": "cancelled"}}
    return {"outcome": {"outcome": "selected", "optionId": str(option_id)}}
