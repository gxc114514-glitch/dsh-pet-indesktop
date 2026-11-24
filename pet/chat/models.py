from __future__ import annotations

import importlib
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from pet.chat.acp.presets import DEFAULT_ACP_TONE_PROMPT

def utc_now(): return datetime.now(timezone.utc).isoformat()

def _safe_float(v, default, lo=None, hi=None):
    """容错数值解析：配置被手改/损坏时回退默认值而不是抛异常。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if lo is not None: x = max(lo, x)
    if hi is not None: x = min(hi, x)
    return x

def _safe_int(v, default, lo=None, hi=None):
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        return default
    if lo is not None: x = max(lo, x)
    if hi is not None: x = min(hi, x)
    return x

THINKING_MODES=('auto','enabled','disabled')
REASONING_EFFORTS=('low','high','max')
WEB_SEARCH_ENGINES=('baidu','duckduckgo','bocha','tavily')
# 需要 API Key 的搜索引擎（其余为免 Key 的网页抓取）
WEB_SEARCH_KEYED_ENGINES=('bocha','tavily')

def _choice(v, choices, default):
    v=str(v or '').strip().lower()
    return v if v in choices else default

@dataclass
class WebSearchSettings:
    """联网搜索（全局，所有 provider 共用）。DeepSeek 官方 API 无内置联网，
    由桌宠先调用搜索引擎、再把结果作为上下文交给模型。"""
    enabled: bool=False; engine: str='baidu'; api_key_ref: str='websearch/api'; api_key: str=''; max_results: int=5; fetch_pages: bool=True
    @classmethod
    def from_dict(cls,raw):
        raw=raw if isinstance(raw,dict) else {}
        return cls(bool(raw.get('enabled',False)),_choice(raw.get('engine','baidu'),WEB_SEARCH_ENGINES,'baidu'),str(raw.get('api_key_ref','websearch/api') or 'websearch/api'),str(raw.get('api_key','')),_safe_int(raw.get('max_results',5),5,lo=1,hi=10),bool(raw.get('fetch_pages',True)))
    def to_dict(self,include_secret=True):
        d=asdict(self)
        if not include_secret: d.pop('api_key',None)
        return d
    def resolve_api_key(self):
        return SecretStore().get(self.api_key_ref) or self.api_key

PROVIDER_KINDS=('openai','acp')

@dataclass
class AcpAgentConfig:
    """ACP (Agent Client Protocol) 本地 Coding Agent 启动与会话配置。"""
    preset: str='custom'
    command: str=''
    args: list[str]=field(default_factory=list)
    env: dict[str,str]=field(default_factory=dict)
    default_cwd: str=''
    default_mode_id: str=''
    default_model_id: str=''
    tone_enabled: bool=True
    tone_prompt: str=DEFAULT_ACP_TONE_PROMPT
    startup_timeout: float=20.0
    idle_timeout_minutes: int=30

    @classmethod
    def from_dict(cls, raw):
        raw=raw if isinstance(raw,dict) else {}
        args_raw=raw.get('args')
        args=[str(x) for x in args_raw] if isinstance(args_raw,list) else []
        env_raw=raw.get('env')
        env={str(k):str(v) for k,v in env_raw.items()} if isinstance(env_raw,dict) else {}
        tone_prompt_raw=raw.get('tone_prompt', DEFAULT_ACP_TONE_PROMPT)
        tone_prompt=DEFAULT_ACP_TONE_PROMPT if tone_prompt_raw is None else str(tone_prompt_raw)
        return cls(
            preset=str(raw.get('preset','custom') or 'custom'),
            command=str(raw.get('command','') or ''),
            args=args,
            env=env,
            default_cwd=str(raw.get('default_cwd','') or ''),
            default_mode_id=str(raw.get('default_mode_id','') or ''),
            default_model_id=str(raw.get('default_model_id','') or ''),
            tone_enabled=bool(raw.get('tone_enabled',True)),
            tone_prompt=tone_prompt,
            startup_timeout=_safe_float(raw.get('startup_timeout',20.0),20.0,lo=1.0),
            idle_timeout_minutes=_safe_int(raw.get('idle_timeout_minutes',30),30,lo=0),
        )

    def to_dict(self):
        return {
            'preset': self.preset,
            'command': self.command,
            'args': list(self.args),
            'env': dict(self.env),
            'default_cwd': self.default_cwd,
            'default_mode_id': self.default_mode_id,
            'default_model_id': self.default_model_id,
            'tone_enabled': self.tone_enabled,
            'tone_prompt': self.tone_prompt,
            'startup_timeout': self.startup_timeout,
            'idle_timeout_minutes': self.idle_timeout_minutes,
        }

@dataclass
class ProviderConfig:
    provider_id: str
    name: str='DeepSeek'; base_url: str='https://api.deepseek.com'; chat_path: str='/v1/chat/completions'; model: str='deepseek-v4-flash'; api_key_ref: str=''; api_key: str=''; timeout: float=60.0; temperature: float=0.7; max_tokens: int=2048; vision_model: str=''; vision_same_as_chat: bool=True; vision_base_url: str=''; vision_api_key_ref: str=''; vision_api_key: str=''; verify_ssl: bool=True
    # 深度思考：auto=不发送思考参数（沿用服务端默认；DeepSeek V4 默认开启思考）；enabled/disabled=显式开关
    thinking: str='auto'; reasoning_effort: str='high'
    # Provider 类型：'openai'（HTTP OpenAI 兼容）或 'acp'（本地 ACP Coding Agent）
    kind: str='openai'; acp: AcpAgentConfig | None=None
    # 运行时注入的联网搜索设置（来自 ChatSettings.web_search，不随 provider 落盘）
    web_search: Any=field(default=None, repr=False, compare=False)
    @property
    def is_acp(self) -> bool:
        return self.kind == 'acp'
    @classmethod
    def from_dict(cls,pid,raw):
        kind=_choice(raw.get('kind','openai'),PROVIDER_KINDS,'openai')
        acp=AcpAgentConfig.from_dict(raw.get('acp')) if kind=='acp' else None
        c = cls(str(pid), str(raw.get('name', pid)), str(raw.get('base_url', 'https://api.deepseek.com')), str(raw.get('chat_path', '/v1/chat/completions')), str(raw.get('model', 'deepseek-v4-flash')), str(raw.get('api_key_ref', f'provider/{pid}')), str(raw.get('api_key', '')), _safe_float(raw.get('timeout', 60), 60.0, lo=1.), _safe_float(raw.get('temperature', .7), 0.7, lo=0., hi=2.), _safe_int(raw.get('max_tokens', 2048), 2048, lo=1), verify_ssl=bool(raw.get('verify_ssl', True)), kind=kind, acp=acp)
        c.vision_model=str(raw.get('vision_model','')); c.vision_same_as_chat=bool(raw.get('vision_same_as_chat',True))
        c.vision_base_url=str(raw.get('vision_base_url','')); c.vision_api_key_ref=str(raw.get('vision_api_key_ref','')); c.vision_api_key=str(raw.get('vision_api_key',''))
        c.thinking=_choice(raw.get('thinking','auto'),THINKING_MODES,'auto'); c.reasoning_effort=_choice(raw.get('reasoning_effort','high'),REASONING_EFFORTS,'high')
        return c
    def to_dict(self,include_secret=True):
        d=asdict(self); d.pop('provider_id',None); d.pop('web_search',None)
        if self.is_acp:
            d['kind']='acp'
            d['acp']=(self.acp or AcpAgentConfig()).to_dict()
        else:
            d.pop('kind',None)
            d.pop('acp',None)
        if not include_secret: d.pop('api_key',None); d.pop('vision_api_key',None)
        return d

@dataclass
class ChatSettings:
    enabled: bool=True; active_provider: str='openai-main'; default_system_prompt: str='你是一只可爱的桌面宠物，请用自然、友善的中文和用户交流。'; history_message_limit: int=40; history_char_limit: int=24000; providers: dict[str,ProviderConfig]=field(default_factory=dict); web_search: WebSearchSettings=field(default_factory=WebSearchSettings); recent_workspaces: list[str]=field(default_factory=list)
    def __post_init__(self): self.attach_runtime()
    def attach_runtime(self):
        """把全局联网设置挂到每个 provider 上，调用点拿 active_config 即可带上。"""
        for p in self.providers.values(): p.web_search=self.web_search
    def record_workspace(self, path: str, limit: int=10) -> None:
        cleaned=str(path or '').strip()
        if not cleaned:
            return
        max_items=max(1,int(limit))
        self.recent_workspaces=[cleaned]+[w for w in self.recent_workspaces if w!=cleaned][:max_items-1]
    def ensure_acp_presets(self) -> bool:
        """若尚未配置任何 ACP provider，则按本机环境自动注入 4 个内置 ACP 预设。"""
        if any(p.is_acp for p in self.providers.values()):
            return False
        from pet.chat.acp.presets import detect_builtin_acp_providers
        for pid, cfg in detect_builtin_acp_providers().items():
            if pid not in self.providers:
                self.providers[pid]=cfg
        self.attach_runtime()
        return True
    @classmethod
    def defaults(cls):
        p=ProviderConfig('openai-main'); return cls(providers={p.provider_id:p})
    @classmethod
    def from_dict(cls,raw):
        raw=raw if isinstance(raw,dict) else {}; d=cls.defaults(); pr=raw.get('providers') if isinstance(raw.get('providers'),dict) else {}
        providers={str(k):ProviderConfig.from_dict(k,v) for k,v in pr.items() if isinstance(v,dict)} or d.providers
        active=str(raw.get('active_provider',next(iter(providers)))); active=active if active in providers else next(iter(providers))
        rw_raw=raw.get('recent_workspaces')
        recent_workspaces=[str(x).strip() for x in rw_raw if str(x).strip()] if isinstance(rw_raw,list) else []
        return cls(bool(raw.get('enabled',True)),active,str(raw.get('default_system_prompt',d.default_system_prompt)),_safe_int(raw.get('history_message_limit',40),40,lo=1),_safe_int(raw.get('history_char_limit',24000),24000,lo=100),providers,WebSearchSettings.from_dict(raw.get('web_search')),recent_workspaces)
    def to_dict(self,include_secrets=True):
        d={'enabled':self.enabled,'active_provider':self.active_provider,'default_system_prompt':self.default_system_prompt,'history_message_limit':self.history_message_limit,'history_char_limit':self.history_char_limit,'providers':{k:v.to_dict(include_secrets) for k,v in self.providers.items()},'web_search':self.web_search.to_dict(include_secrets)}
        if self.recent_workspaces:
            d['recent_workspaces']=list(self.recent_workspaces)
        return d
    @property
    def active_config(self):
        p=self.providers[self.active_provider]; p.web_search=self.web_search; return p

@dataclass
class ChatMessage:
    role: str; content: str; created_at: str=field(default_factory=utc_now); message_id: str=field(default_factory=lambda:uuid.uuid4().hex)
    # 仅供界面回显的思考过程 / 联网检索进度；不进模型上下文（PromptBuilder 只取 content）
    reasoning: str=''; activity: list[str]=field(default_factory=list)
    def to_dict(self):
        d=asdict(self)
        # 空值不落盘：旧会话文件格式不变，旧版本读到新文件也只会忽略这两个键
        if not d['reasoning']: d.pop('reasoning')
        if not d['activity']: d.pop('activity')
        return d
    @classmethod
    def from_dict(cls,raw):
        activity=raw.get('activity')
        activity=[str(x) for x in activity] if isinstance(activity,list) else []
        return cls(str(raw.get('role','user')),str(raw.get('content','')),str(raw.get('created_at',utc_now())),str(raw.get('message_id',uuid.uuid4().hex)),str(raw.get('reasoning') or ''),activity)

@dataclass
class ChatSession:
    session_id: str; character_id: str; provider_id: str; system_prompt: str; messages: list[ChatMessage]=field(default_factory=list); created_at: str=field(default_factory=utc_now); updated_at: str=field(default_factory=utc_now); custom_title: str=''; pinned: bool=False; workspace_dir: str=''; acp_session_id: str=''; acp_mode_id: str=''; acp_model_id: str=''
    @classmethod
    def create(cls,character_id,provider_id,system_prompt): return cls(uuid.uuid4().hex,character_id,provider_id,system_prompt)
    def to_dict(self):
        d={'session_id':self.session_id,'character_id':self.character_id,'provider_id':self.provider_id,'system_prompt':self.system_prompt,'created_at':self.created_at,'updated_at':self.updated_at,'title':self.custom_title,'pinned':self.pinned,'messages':[m.to_dict() for m in self.messages]}
        if self.workspace_dir: d['workspace_dir']=self.workspace_dir
        if self.acp_session_id: d['acp_session_id']=self.acp_session_id
        if self.acp_mode_id: d['acp_mode_id']=self.acp_mode_id
        if self.acp_model_id: d['acp_model_id']=self.acp_model_id
        return d
    @classmethod
    def from_dict(cls,raw):
        return cls(
            str(raw['session_id']),
            str(raw['character_id']),
            str(raw.get('provider_id','')),
            str(raw.get('system_prompt','')),
            [ChatMessage.from_dict(x) for x in raw.get('messages',[]) if isinstance(x,dict)],
            str(raw.get('created_at',utc_now())),
            str(raw.get('updated_at',utc_now())),
            str(raw.get('title',raw.get('custom_title',''))),
            bool(raw.get('pinned',False)),
            str(raw.get('workspace_dir','') or ''),
            str(raw.get('acp_session_id','') or ''),
            str(raw.get('acp_mode_id','') or ''),
            str(raw.get('acp_model_id','') or ''),
        )

    @property
    def title(self):
        """Upstream-compatible alias while modern UI keeps custom_title."""
        return self.custom_title

    @title.setter
    def title(self, value):
        self.custom_title = str(value or '')

# keyring 的"没有可用后端"占位实现：**全名精确匹配**，不扫名字子串。
# 扫子串（原来的 `"fail" not in low`）会误杀合法第三方后端——一个名为
# `company.failover.Keyring` 的可写后端会被判成不可用，等于把本来能存凭据的环境
# 判死（astra 第 13 轮）。这类判据只该认 keyring 自己的两个占位类。
_PLACEHOLDER_BACKENDS = frozenset({
    "keyring.backends.fail.Keyring",
    "keyring.backends.null.Keyring",
})


def _is_placeholder_backend(backend) -> bool:
    """这个后端只是"这台机器没有钥匙串"的占位实现（`fail` / `null`）吗？"""
    backend_type = type(backend)
    if f"{backend_type.__module__}.{backend_type.__name__}" in _PLACEHOLDER_BACKENDS:
        return True
    # 全名对不上时再看**类身份**：占位实现可能被继承/包装，或在别的模块路径下重导出。
    # 只比类、不猜名字，所以仍然不会误杀第三方后端。
    for module_name in ("keyring.backends.fail", "keyring.backends.null"):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 - 子系统不在就是"没有可比对的对象"
            continue
        placeholder = getattr(module, "Keyring", None)
        if isinstance(placeholder, type) and isinstance(backend, placeholder):
            return True
    return False


class SecretStore:
    def __init__(self,service_name='dsh-pet-standalone'):
        self.service_name=service_name
        try: import keyring
        except Exception: keyring=None
        self._keyring=keyring
        self._usable=None   # 惰性探测一次（后端在进程生命周期内不会换）

    def _backend_usable(self) -> bool:
        """**后端**真的能用吗（不只是 `import keyring` 成功）。

        模块在、后端不可用时写凭据会失败（`fail` 后端抛 ``NoKeyringError``，
        `null` 后端不抛但什么都不记）：界面说"已保存"、重启后主进程读到空串。
        **这是"读取为空"的成因，不是"服务端拒绝"的成因**——服务端回 401 并显示
        具体 key 尾号，只说明发出去的是非空凭据，具体为什么无效（key 失效 / 填错 /
        provider 不匹配 / 选错了旧的凭据条目）需要另查，别把两者混为一谈。
        """
        if self._keyring is None:
            return False
        try:
            backend = self._keyring.get_keyring()
        except Exception:
            return False
        return not _is_placeholder_backend(backend)

    @property
    def available(self):
        """可用 = 模块在 **且** 后端可用（两者缺一都是"存不了凭据"）。"""
        if self._usable is None:
            self._usable = self._backend_usable()
            if not self._usable:
                logging.getLogger("dsh-pet-standalone").warning(
                    "凭据存储不可用（系统钥匙串没有可用后端）：API Key 只能留在内存里，"
                    "重启后需要重新填写。打包版可能是 keyring 的 Windows 后端依赖"
                    "（pywin32-ctypes / win32ctypes）没随包进来，也可能只是这台机器的"
                    "系统钥匙串没启用/被策略禁用。")
        return self._usable

    def get(self,ref):
        if not self.available or not ref: return ''
        try: return str(self._keyring.get_password(self.service_name,ref) or '')
        except Exception as exc:
            logging.getLogger("dsh-pet-standalone").warning(
                "读取凭据失败（%s）：%s", ref, type(exc).__name__)
            return ''

    def set(self,ref,value):
        if not self.available or not ref: return False
        try: self._keyring.set_password(self.service_name,ref,value); return True
        except Exception as exc:
            # 失败不能静默：界面会显示"已保存"，用户下次启动就发现 Key 没了
            logging.getLogger("dsh-pet-standalone").warning(
                "保存凭据失败（%s）：%s", ref, type(exc).__name__)
            return False
