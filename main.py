# -*- coding: utf-8 -*-
"""Dududa 2.0 AstrBot 插件 —— 薄 Adapter（Phase 4 拆分）。

职责边界：
- 本文件：AstrBot 事件/命令适配、依赖装配、兼容 re-export；
- packages/application/dududa_utils.py：纯函数与常量；
- packages/application/dududa_core.py：应用用例层（DududaCore）；
- packages/application/dududa_handlers.py：消息流处理；
- packages/application/dududa_commands.py：管理命令；
- packages/application/dududa_prod.py：生产 Orchestrator / 决策 / CapProvider。
"""
import sys, os, re, time, logging, httpx, json as _json, base64 as _b64
from io import BytesIO
sys.path.insert(0, "/opt/dududa20-prototype")

from astrbot.api import star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.all import *

from packages.router.openai_provider import OpenAIProvider
from packages.router.router import ModelConfig, ModelRole
from packages.core.persona.registry import PersonaRegistry
from packages.core.persona.persona_renderer import PersonaRenderer
from packages.core.memory import (MemoryRecord, MemoryType, MemoryScope,
                                  MemoryCandidate, SensitivityLevel,
                                  JSONMemoryRepository)
from packages.core.state import (SocialAction, RuntimeState, RuntimePhase,
                                 RunOutcome, RuntimeBudget)
from packages.core.renderer import FactAnchor, DraftResponse, FinalResponse, OCRenderer
from packages.core.perception import PerceptionResult, SpeechAct, EntityRef
from packages.core.context import ContextBuilder, ContextSnapshot
from packages.core.capability import (CapabilityRegistry, Capability, CapabilityRisk,
                                      ProviderType, CapProvider, ToolObservation)
from packages.safeguards.security import (PermissionEngine, AuthorizationDecision,
                                          AuthorizationResult, AuthReason,
                                          ConfirmationStore, Redactor)
from packages.core.decision import SocialDecisionEngine, SocialDecision, DecisionReason
from packages.core.delivery import DeliveryReceipt, DeliveryStatus
from packages.runtime.orchestrator import RuntimeOrchestrator
from packages.mcp.registry import register_all_mcp_services
from packages.planner.integration import integrate_with_orchestrator
from packages.adapters.astrbot.input_adapter import AstrBotInputAdapter, ActorMappingConfig

# ---- 应用用例层（Phase 4：Core 薄化） ----
from packages.application.dududa_utils import (
    _redact_text, _contains_restricted, _atomic_write_json,
    _group_safe_observations, _detect_media, _has_media_in_raw,
    _file_ext, _parse_document,
    _RESTRICTED_PATTERNS, _SENSITIVE_GROUP_KW, _IGNORE_PATTERNS, _IMAGE_EXTS,
)
from packages.application.dududa_prod import (
    _ProdDecisionEngine, _ProdCapProvider, _ProdOrchestrator,
)
from packages.application.dududa_core import DududaCore, persona_to_oc
from packages.application import dududa_commands, dududa_handlers

# ---- Model Router（P1 遗留接入：消息类型 -> 模型路由，带回退） ----
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
from _router import router as _model_router
router = _model_router


logger = logging.getLogger("dududa20")

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL   = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
provider = OpenAIProvider(api_key=API_KEY, base_url="https://api.deepseek.com/v1")

VISION_KEY   = os.environ.get("OPENAI_API_KEY", API_KEY)
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")
VISION_BASE  = os.environ.get("OPENAI_BASE_URL", "https://www.mhcoding.xyz/v1")

# Fallback models for resilience
FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "gpt-5.5")
FALLBACK_KEY   = os.environ.get("FALLBACK_KEY", VISION_KEY)
FALLBACK_BASE  = os.environ.get("FALLBACK_BASE", VISION_BASE)

# ---- P5: 安全（Policy / 确认 / 脱敏）与 Memory v2 配置 ----
_PLUGIN_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.environ.get("DUDUDA_MEMORY_FILE",
                             os.path.join(_PLUGIN_DATA_DIR, "memory.json"))
CONFIRM_FILE = os.environ.get("DUDUDA_CONFIRM_FILE",
                              os.path.join(_PLUGIN_DATA_DIR, "confirmations.json"))
OWNER_IDS = {x.strip() for x in os.environ.get("DUDUDA_OWNER_IDS", "").split(",") if x.strip()}
ADMIN_IDS = {x.strip() for x in os.environ.get("DUDUDA_ADMIN_IDS", "").split(",") if x.strip()}
TRUSTED_IDS = {x.strip() for x in os.environ.get("DUDUDA_TRUSTED_IDS", "").split(",") if x.strip()}
MUTED_IDS = {x.strip() for x in os.environ.get("DUDUDA_MUTED_IDS", "").split(",") if x.strip()}

# 应用用例层配置：动态代理（每次读取当前模块常量，monkeypatch 兼容）
class _LiveConfig:
    """读取 main 模块当前常量；测试 monkeypatch main.XXX 后立即生效。"""

    def __getitem__(self, key: str):
        return {
            "MODEL": MODEL,
            "FALLBACK_MODEL": FALLBACK_MODEL,
            "FALLBACK_KEY": FALLBACK_KEY,
            "FALLBACK_BASE": FALLBACK_BASE,
            "VISION_MODEL": VISION_MODEL,
            "VISION_KEY": VISION_KEY,
            "VISION_BASE": VISION_BASE,
            "MEMORY_FILE": MEMORY_FILE,
            "CONFIRM_FILE": CONFIRM_FILE,
            "OWNER_IDS": OWNER_IDS,
            "ADMIN_IDS": ADMIN_IDS,
            "TRUSTED_IDS": TRUSTED_IDS,
            "MUTED_IDS": MUTED_IDS,
        }[key]


class Main(star.Star):
    """薄 Adapter：装配依赖 + 事件/命令适配；业务逻辑在应用用例层。"""

    def __init__(self, context: star.Context):
        super().__init__(context)
        self.personas = PersonaRegistry()
        self.renderer = PersonaRenderer(self.personas.active)
        self.oc_renderer = OCRenderer(persona=persona_to_oc(self.personas.active))
        self.permission_engine = PermissionEngine()
        self.confirmations = ConfirmationStore(ttl_seconds=600)
        self.memory = JSONMemoryRepository(path=MEMORY_FILE)
        self.cap_registry = CapabilityRegistry()
        self.context_builder = ContextBuilder(memory_repo=self.memory, capability_registry=self.cap_registry)
        self.input_adapter = AstrBotInputAdapter(ActorMappingConfig(hash_user_ids=True))
        self._pending_confirms = {}  # 兼容属性（实际状态在 DududaCore）
        self._core = DududaCore(
            memory=self.memory, personas=self.personas, renderer=self.renderer,
            oc_renderer=self.oc_renderer, permission_engine=self.permission_engine,
            confirmations=self.confirmations, cap_registry=self.cap_registry,
            context_builder=self.context_builder, input_adapter=self.input_adapter,
            llm_provider=provider, config=_LiveConfig(),
        )
        self.runtime = _ProdOrchestrator(
            plugin=self,
            decision_engine=_ProdDecisionEngine(),
            capability_registry=self.cap_registry,
            memory_repo=self.memory,
            renderer=self.oc_renderer,
            planner_integration=integrate_with_orchestrator(None, self.cap_registry),
        )
        self.enabled  = True
        self._bot_id  = None
        self._processed: set[str] = set()
        self._last_file_ts: float = 0.0
        self.mcp_client = None
        self._register_builtin_caps()
        self._register_mcp_caps()
        logger.info("Dududa 2.0 | renderer=OK | memory=JSON | vision=%s | security=ON", VISION_MODEL)

    # ---- 薄壳：委托应用用例层（保持测试与 _Prod* 兼容） ----

    def _is_self_message(self, event) -> bool:
        return self._core._is_self_message(event)

    def _get_bot_id(self, event) -> str:
        return self._core._get_bot_id(event)

    def _actor_for(self, event):
        return self._core._actor_for(event)

    def _scope_key(self, event, resource="") -> str:
        return self._core._scope_key(event, resource)

    @staticmethod
    def _same_scope_prefix(a: str, b: str) -> bool:
        return DududaCore._same_scope_prefix(a, b)

    def _authorize(self, event, action, resource="", payload=None,
                   capability_risk=None, requires_confirmation=False):
        return self._core._authorize(
            event, action, resource=resource, payload=payload,
            capability_risk=capability_risk,
            requires_confirmation=requires_confirmation)

    def _confirm_key(self, event, resource, payload) -> str:
        return self._core._confirm_key(event, resource, payload)

    def _authorize_manage(self, event, resource, payload):
        return self._core._authorize_manage(event, resource, payload)

    def _create_confirmation(self, event, resource, payload):
        return self._core._create_confirmation(event, resource, payload)

    def _consume_confirm(self, event, conf, resource, payload) -> bool:
        return self._core._consume_confirm(event, conf, resource, payload)

    def _load_confirmations(self):
        self._core._load_confirmations()

    def _save_confirmations(self):
        self._core._save_confirmations()

    def _should_ignore(self, event) -> bool:
        return self._core._should_ignore(event)

    def _social_decision(self, event) -> tuple:
        try:
            return self._social_decision_impl(event)
        except Exception:
            # 生产兜底：任何异常都回落到普通回答，不吞消息也不崩
            return SocialAction.ANSWER, "normal"

    def _social_decision_impl(self, event) -> tuple:
        return self._core._social_decision_impl(event)

    def _perceive(self, event) -> PerceptionResult:
        return self._core._perceive(event)

    def _make_scope(self, event, msg_type="text") -> MemoryScope:
        return self._core._make_scope(event, msg_type=msg_type)

    def _store_memory(self, event, *contents: str, msg_type="text",
                      sensitivity=None):
        self._core._store_memory(
            event, *contents, msg_type=msg_type, sensitivity=sensitivity)

    def _read_memory(self, event, limit=8, budget=2500, include_episodic=False):
        return self._core._read_memory(
            event, limit=limit, budget=budget, include_episodic=include_episodic)

    def _persona_to_oc(self, template):
        return self._core._persona_to_oc(template)

    def _render_response(self, raw_text: str, persona_tone: str = "", anchors=()) -> str:
        return self._core._render_response(raw_text, persona_tone=persona_tone, anchors=anchors)

    def _persona_tone(self):
        return self._core._persona_tone()

    async def _call_llm(self, system, user_msg, max_tokens=1024, temperature=0.5):
        return await self._core._call_llm(
            system, user_msg, max_tokens=max_tokens, temperature=temperature)

    async def _call_vision(self, system, user_text, image_b64, mime):
        return await self._core._call_vision(system, user_text, image_b64, mime)

    @staticmethod
    def _deny_hint(res, conf) -> str:
        return dududa_commands._deny_hint(res, conf)

    # ---- 装配（Capability 注册，保留在适配层） ----

    def _register_builtin_caps(self):
        # Register built-in capabilities for discovery & health tracking
        self.cap_registry.register(
            Capability(capability_id="chat", name="智能对话",
                       description="基于 LLM 的文本对话能力",
                       provider=ProviderType.BUILTIN, risk=CapabilityRisk.READ_ONLY),
            _ProdCapProvider(self, "chat")
        )
        self.cap_registry.register(
            Capability(capability_id="vision", name="图片识别",
                       description="识别图片内容并提取文字 (GPT-5.5 Vision)",
                       provider=ProviderType.BUILTIN, risk=CapabilityRisk.READ_ONLY),
            _ProdCapProvider(self, "vision")
        )
        self.cap_registry.register(
            Capability(capability_id="file_reader", name="文件阅读",
                       description="解析 docx/pdf/txt 文件内容",
                       provider=ProviderType.BUILTIN, risk=CapabilityRisk.READ_ONLY),
            _ProdCapProvider(self, "file_reader")
        )

    def _register_mcp_caps(self):
        try:
            if os.environ.get("DUDUDA_MCP_CLIENT", "0") == "1":
                # P6: 统一 MCP Client（iCourse stdio server，懒启动）
                from packages.mcp.client import create_unified_provider_factory
                factory = create_unified_provider_factory()
                n = register_all_mcp_services(
                    self.cap_registry, provider_factory=factory)
                self.mcp_client = factory
            else:
                n = register_all_mcp_services(self.cap_registry)
                self.mcp_client = None
            logger.info("MCP capabilities registered: %d", n)
        except Exception as e:
            logger.warning("MCP registration failed: %s", e)

    # ---- 事件入口（薄壳） ----

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        reply = await dududa_handlers.run_message_flow(self, event)
        if reply:
            yield event.plain_result(reply)

    async def _handle_media(self, event, url, name, is_image):
        return await dududa_handlers.handle_media(self, event, url, name, is_image)

    async def _handle_image(self, event, data, name, ext):
        return await dududa_handlers.handle_image(self, event, data, name, ext)

    async def _handle_text(self, event):
        return await dududa_handlers.handle_text(self, event)

    # ---- 管理命令（薄壳） ----

    @filter.command("dududa")
    async def cmd_status(self, event: AstrMessageEvent):
        yield event.plain_result(await dududa_commands.cmd_status_impl(self))

    @filter.command("dududa_mcp")
    async def cmd_mcp(self, event: AstrMessageEvent):
        """统一 MCP Client 状态（启用 DUDUDA_MCP_CLIENT=1 后生效）。"""
        yield event.plain_result(await dududa_commands.cmd_mcp_impl(self))

    @filter.command("dududa_health")
    async def cmd_health(self, event: AstrMessageEvent):
        yield event.plain_result(await dududa_commands.cmd_health_impl(self))

    @filter.command("dududa_persona")
    async def cmd_persona(self, event: AstrMessageEvent, target: str = None):
        yield event.plain_result(await dududa_commands.cmd_persona_impl(self, event, target))

    @filter.command("dududa_confirm")
    async def cmd_confirm(self, event: AstrMessageEvent, confirmation_id: str = None):
        """管理员批准高风险操作确认（绑定发起者/会话/操作内容，单次使用）。"""
        yield event.plain_result(await dududa_commands.cmd_confirm_impl(self, event, confirmation_id))

    @filter.command("dududa_off")
    async def cmd_off(self, event: AstrMessageEvent):
        yield event.plain_result(await dududa_commands.cmd_off_impl(self, event))

    @filter.command("dududa_on")
    async def cmd_on(self, event: AstrMessageEvent):
        yield event.plain_result(await dududa_commands.cmd_on_impl(self, event))

    @filter.command("dududa_forget")
    async def cmd_forget(self, event: AstrMessageEvent):
        yield event.plain_result(await dududa_commands.cmd_forget_impl(self, event))
