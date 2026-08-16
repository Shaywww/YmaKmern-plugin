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
sys.path.insert(0, "/opt/dududa20-prototype/packages/dududa-agent/src")

from astrbot.api import star
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.all import *

from dududa.router.openai_provider import OpenAIProvider
from dududa.router.router import ModelConfig, ModelRole, RouterConfig, ModelRouter
from dududa.core.persona.registry import PersonaRegistry
from dududa.core.persona.persona_renderer import PersonaRenderer
from dududa.core.memory import (MemoryRecord, MemoryType, MemoryScope,
                                  MemoryCandidate, SensitivityLevel,
                                  JSONMemoryRepository)
from dududa.core.state import (SocialAction, RuntimeState, RuntimePhase,
                                 RunOutcome, RuntimeBudget)
from dududa.core.renderer import FactAnchor, DraftResponse, FinalResponse, OCRenderer
from dududa.core.perception import PerceptionResult, SpeechAct, EntityRef
from dududa.core.context import ContextBuilder, ContextSnapshot
from dududa.core.capability import (CapabilityRegistry, Capability, CapabilityRisk,
                                      ProviderType, CapProvider, ToolObservation)
from dududa.safeguards.security import (PermissionEngine, AuthorizationDecision,
                                          AuthorizationResult, AuthReason,
                                          ConfirmationStore, Redactor)
from dududa.safeguards.limits import make_runtime_limits_from_env
from dududa.core.decision import SocialDecisionEngine, SocialDecision, DecisionReason
from dududa.core.delivery import DeliveryReceipt, DeliveryStatus
from dududa.runtime.orchestrator import RuntimeOrchestrator
from dududa.core.idempotency import MessageIdempotencyRegistry
from dududa.core.attachment_repo import AttachmentRepository
from dududa.core.profile import ProfileStore
from dududa.core.group_policy import GroupPolicyStore
from dududa.core.style_store import UserStyleStore
from dududa.evolution import ShadowEvolution
from dududa.core.structured_output import PERCEPTION_SYSTEM_PROMPT
from dududa.mcp.registry import register_all_mcp_services
from dududa.planner.integration import integrate_with_orchestrator
from dududa.adapters.astrbot.input_adapter import AstrBotInputAdapter, ActorMappingConfig

from dududa.application.dududa_utils import (
    _redact_text, _contains_restricted, _atomic_write_json,
    _group_safe_observations, _detect_media, _has_media_in_raw,
    _file_ext, _parse_document,
    _RESTRICTED_PATTERNS, _SENSITIVE_GROUP_KW, _IGNORE_PATTERNS, _IMAGE_EXTS,
)
from dududa.application.dududa_prod import (
    _ProdDecisionEngine, _ProdCapProvider, _ProdOrchestrator,
)
from dududa.application.dududa_core import DududaCore, persona_to_oc
from dududa.application import dududa_commands, dududa_handlers
from dududa.application.user_experience import (
    UserExperienceStore, ConversationTaskRegistry,
)
from dududa.application.dududa_log import get_logger as _get_logger
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
from _router import router as _model_router
router = _model_router


logger = _get_logger("dududa20")

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
MODEL   = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

VISION_KEY   = os.environ.get("OPENAI_API_KEY", API_KEY)
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-haiku-4-5-20251001")
VISION_BASE  = os.environ.get("OPENAI_BASE_URL", "https://www.mhcoding.xyz/v1")

FALLBACK_MODEL = os.environ.get("FALLBACK_MODEL", "gpt-5.5")
FALLBACK_KEY   = os.environ.get("FALLBACK_KEY", VISION_KEY)
FALLBACK_BASE  = os.environ.get("FALLBACK_BASE", VISION_BASE)

# ---- Model Router（文档 2.5.7：八类角色统一路由 + 降级）----
ROUTER_ENABLED = os.environ.get("DUDUDA_ROUTER", "1") == "1"

HYBRID_RENDER = os.environ.get("DUDUDA_HYBRID_RENDER", "1") == "1"
_RENDER_CONVERTER_SYSTEM = (
    "你是回复风格转换器。只能调整语序、句式、称呼、口语程度和适量表情；"
    "绝对不能修改数字、日期、来源、权限、拒绝结论、工具状态、目标用户或"
    "附件内容。只输出转换后的文本，不要任何解释。"
)

provider = OpenAIProvider(
    api_key=API_KEY,
    base_url="https://api.deepseek.com/v1",
    base_urls={FALLBACK_MODEL: FALLBACK_BASE, VISION_MODEL: VISION_BASE},
    api_keys={FALLBACK_MODEL: FALLBACK_KEY, VISION_MODEL: VISION_KEY},
)


def _role_cfg(role, effort, tokens, temp=0.7, timeout=30.0, model=None):
    """单角色生产配置：主模型 = MODEL（可覆盖），降级 = FALLBACK_MODEL（文档 2.5.7）。"""
    return ModelConfig(
        role=role, model_id=model or MODEL, reasoning_effort=effort,
        max_tokens=tokens, temperature=temp, timeout_seconds=timeout,
        retry_count=1, allow_sensitive=False, route_hint_allowed=False,
        fallback_model_id=FALLBACK_MODEL,
    )


router_config = RouterConfig(roles={
    ModelRole.PERCEPTION: _role_cfg(ModelRole.PERCEPTION, "low", 1024),
    ModelRole.SOCIAL_DECISION: _role_cfg(ModelRole.SOCIAL_DECISION, "medium", 512),
    ModelRole.TOOL_PLANNING: _role_cfg(ModelRole.TOOL_PLANNING, "high", 2048),
    ModelRole.DIRECT_CHAT: _role_cfg(ModelRole.DIRECT_CHAT, "medium", 2048),
    ModelRole.RESPONSE_COMPOSITION: _role_cfg(ModelRole.RESPONSE_COMPOSITION, "medium", 2048),
    ModelRole.MEMORY_SUMMARY: _role_cfg(ModelRole.MEMORY_SUMMARY, "low", 1024),
    ModelRole.IMAGE_UNDERSTANDING: _role_cfg(ModelRole.IMAGE_UNDERSTANDING,
                                            "medium", 1024, temp=0.3, timeout=90.0,
                                            model=VISION_MODEL),
    ModelRole.IMAGE_GENERATION: _role_cfg(ModelRole.IMAGE_GENERATION,
                                          "medium", 1024, temp=0.3, timeout=90.0,
                                          model=VISION_MODEL),
})

# ---- P5: 安全（Policy / 确认 / 脱敏）与 Memory v2 配置 ----
_PLUGIN_DATA_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.environ.get("DUDUDA_MEMORY_FILE",
                             os.path.join(_PLUGIN_DATA_DIR, "memory.json"))
CONFIRM_FILE = os.environ.get("DUDUDA_CONFIRM_FILE",
                              os.path.join(_PLUGIN_DATA_DIR, "confirmations.json"))
GROUP_POLICY_FILE = os.environ.get(
    "DUDUDA_GROUP_POLICY_FILE",
    os.path.join(_PLUGIN_DATA_DIR, "data", "group_policy.json"))
STYLE_FILE = os.environ.get("DUDUDA_STYLE_FILE",
                         os.path.join(_PLUGIN_DATA_DIR, "data", "styles.json"))
UX_FILE = os.environ.get("DUDUDA_UX_FILE",
                        os.path.join(_PLUGIN_DATA_DIR, "data", "user_experience.json"))
PERCEPTION_MODEL_ENABLED = os.environ.get("DUDUDA_PERCEPTION_MODEL", "1") == "1"
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
            "VISION_MODEL": VISION_MODEL, "VISION_KEY": VISION_KEY,
            "VISION_BASE": VISION_BASE,
            "MEMORY_FILE": MEMORY_FILE,
            "CONFIRM_FILE": CONFIRM_FILE,
            "OWNER_IDS": OWNER_IDS, "ADMIN_IDS": ADMIN_IDS,
            "TRUSTED_IDS": TRUSTED_IDS, "MUTED_IDS": MUTED_IDS,
        }[key]


class Main(star.Star):
    """薄 Adapter：装配依赖 + 事件/命令适配；业务逻辑在应用用例层。"""

    def __init__(self, context: star.Context):
        super().__init__(context)
        self.personas = PersonaRegistry()
        self.renderer = PersonaRenderer(self.personas.active)
        self.oc_renderer = OCRenderer(
            persona=persona_to_oc(self.personas.active),
            llm=self._render_llm if HYBRID_RENDER else None)
        self.permission_engine = PermissionEngine()
        self.confirmations = ConfirmationStore(ttl_seconds=600)
        self.limits = make_runtime_limits_from_env(
            budget_file_default=os.path.join(
                _PLUGIN_DATA_DIR, "data", "budget.json"))
        self.memory = JSONMemoryRepository(path=MEMORY_FILE)
        self.cap_registry = CapabilityRegistry()
        self.profile_store = ProfileStore(path=os.environ.get(
            "DUDUDA_PROFILE_FILE",
            os.path.join(_PLUGIN_DATA_DIR, "data", "profiles.json")))
        self.group_policy = GroupPolicyStore(path=GROUP_POLICY_FILE)
        self.style_store = UserStyleStore(path=STYLE_FILE)
        self.ux_store = UserExperienceStore(path=UX_FILE)
        self.evolution = ShadowEvolution()
        self.ux_tasks = ConversationTaskRegistry()
        self.progress_delay = float(os.environ.get("DUDUDA_PROGRESS_DELAY", "5"))
        self._pending_broadcasts = {}
        self._perception_model_enabled = PERCEPTION_MODEL_ENABLED
        self.context_builder = ContextBuilder(
            memory_repo=self.memory, capability_registry=self.cap_registry,
            profile_store=self.profile_store,
            style_store=self.style_store)
        self.input_adapter = AstrBotInputAdapter(ActorMappingConfig(hash_user_ids=True))
        self._pending_confirms = {}  # 兼容属性（实际状态在 DududaCore）
        self._model_router = None
        if ROUTER_ENABLED:
            self._model_router = ModelRouter(config=router_config, provider=provider)
        self._core = DududaCore(
            memory=self.memory, personas=self.personas, renderer=self.renderer,
            oc_renderer=self.oc_renderer, permission_engine=self.permission_engine,
            confirmations=self.confirmations, cap_registry=self.cap_registry,
            context_builder=self.context_builder, input_adapter=self.input_adapter,
            llm_provider=provider, config=_LiveConfig(),
            model_router=self._model_router,
            group_policy=self.group_policy,
        )
        # Core 判重注册表：独立于 handlers 外层判重（外层已登记的消息不会被 Core 误判）
        self._idem_core = MessageIdempotencyRegistry()
        # 受信 Attachment Repository（文档 2.4.2）：群图暂存/配对走仓库，Core 只拿 opaque ref
        self.media_repo = AttachmentRepository(
            ttl_seconds=float(os.environ.get("DUDUDA_MEDIA_TTL", "60")),
            max_entries=int(os.environ.get("DUDUDA_MEDIA_MAX_ENTRIES", "100")),
            max_bytes_per_entry=int(os.environ.get(
                "DUDUDA_MEDIA_MAX_BYTES", str(20 * 1024 * 1024))),
            max_total_bytes=int(os.environ.get(
                "DUDUDA_MEDIA_MAX_TOTAL_BYTES", str(200 * 1024 * 1024))),
        )
        self.runtime = _ProdOrchestrator(
            plugin=self,
            decision_engine=_ProdDecisionEngine(),
            capability_registry=self.cap_registry,
            memory_repo=self.memory,
            renderer=self.oc_renderer,
            planner_integration=integrate_with_orchestrator(None, self.cap_registry),
            profile_store=self.profile_store,
            style_store=self.style_store,
            idempotency_registry=self._idem_core,
            confirmation_store=self.confirmations,
        )
        self.enabled  = True
        self._bot_id  = None
        self._idem = MessageIdempotencyRegistry()
        self._pending_deliveries: dict = {}  # run_id -> (RuntimeResult, reply, ts)
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

    def _group_policy_view(self, event):
        """当前群 PolicyView 投影（供 ContextBuilder / Runtime 使用）。"""
        try:
            gid = str(getattr(event.message_obj, "group", None) or "")
            if not gid:
                return None
            return self.group_policy.to_policy_view(gid)
        except Exception:
            return None

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
                      sensitivity=None, run_id="", trace_id=""):
        self._core._store_memory(
            event, *contents, msg_type=msg_type, sensitivity=sensitivity,
            run_id=run_id, trace_id=trace_id)

    def _read_memory(self, event, limit=8, budget=2500, include_episodic=False):
        return self._core._read_memory(
            event, limit=limit, budget=budget, include_episodic=include_episodic)

    def _persona_to_oc(self, template):
        return self._core._persona_to_oc(template)

    def _render_response(self, raw_text: str, persona_tone: str = "", anchors=()) -> str:
        return self._core._render_response(raw_text, persona_tone=persona_tone, anchors=anchors)

    def _persona_tone(self):
        return self._core._persona_tone()

    async def _call_llm(self, system, user_msg, max_tokens=1024, temperature=0.5,
                        run_id="", trace_id="", skip_render=False):
        return await self._core._call_llm(
            system, user_msg, max_tokens=max_tokens, temperature=temperature,
            run_id=run_id, trace_id=trace_id, skip_render=skip_render)

    async def _render_llm(self, prompt: str, run_id: str = "",
                          trace_id: str = "") -> str:
        """2.5.8 hybrid renderer 模型回调：按 Persona 做风格转换。"""
        return await self._core._call_llm(
            _RENDER_CONVERTER_SYSTEM, prompt,
            max_tokens=1024, temperature=0.9,
            run_id=run_id, trace_id=trace_id, skip_render=True)

    async def _perception_signal(self, text: str, capabilities=()):
        """模型感知信号（DUDUDA_PERCEPTION_MODEL=1 启用；失败返回 None）。带能力清单输出 tool_plan。"""
        if not getattr(self, "_perception_model_enabled", False):
            return None
        try:
            user_msg = text
            if capabilities:
                cap_lines = "\n".join(f"- {c}" for c in list(capabilities)[:20])
                user_msg = f"可用工具:\n{cap_lines}\n\n用户消息: {text}"
            reply = await self._call_llm(
                PERCEPTION_SYSTEM_PROMPT, user_msg,
                max_tokens=512, temperature=0.0, skip_render=True)
            if not reply or not reply.strip():
                return None
            return _json.loads(reply)
        except Exception as e:
            logger.warning("Perception model signal failed: %s", e)
            return None

    async def _call_vision(self, system, user_text, image_b64, mime,
                           run_id="", trace_id=""):
        return await self._core._call_vision(
            system, user_text, image_b64, mime, run_id=run_id, trace_id=trace_id)

    @staticmethod
    def _deny_hint(res, conf) -> str:
        return dududa_commands._deny_hint(res, conf)


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
            from dududa.mcp.access import mcp_access
            mcp_access.ensure_seed(owner_ids=tuple(sorted(OWNER_IDS)))
            if os.environ.get("DUDUDA_MCP_CLIENT", "0") == "1":
                from dududa.mcp.client import create_unified_provider_factory
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

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        reply = await dududa_handlers.run_message_flow(self, event)
        if reply:
            yield event.plain_result(reply)

    @filter.after_message_sent()
    async def _after_message_sent(self, event: AstrMessageEvent):
        """两段式完成协议 Phase B：框架发送后确认投递（文档 2.3.15-2.3.16）。"""
        try:
            await dududa_handlers.complete_delivery_after_send(self, event)
        except Exception as e:
            logger.warning("after_message_sent delivery ack failed: %s", e)

    async def _handle_media(self, event, url, name, is_image):
        return await dududa_handlers.handle_media(self, event, url, name, is_image)

    async def _handle_image(self, event, data, name, ext):
        return await dududa_handlers.handle_image(self, event, data, name, ext)

    async def _handle_text(self, event):
        return await dududa_handlers.handle_text(self, event)

    async def _send_progress(self, event, text: str):
        """Send a non-terminal status update through the current adapter."""
        await event.send(event.plain_result(text))

    async def _send_subscription_message(self, origin: str, text: str):
        """Send only to a stored origin belonging to an explicit subscriber."""
        await self.context.send_message(origin, MessageChain().message(text))


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

    @filter.command("dududa_group")
    async def cmd_group(self, event: AstrMessageEvent, target: str = None):
        yield event.plain_result(await dududa_commands.cmd_group_impl(self, event, target))

    @filter.command("dududa_mode")
    async def cmd_group_mode(self, event: AstrMessageEvent,
                             group_id: str = None, mode: str = None):
        yield event.plain_result(await dududa_commands.cmd_group_mode_impl(
            self, event, group_id, mode))

    @filter.command("dududa_reply_rate")
    async def cmd_group_reply_rate(self, event: AstrMessageEvent,
                                   group_id: str = None, rate: str = None):
        yield event.plain_result(await dududa_commands.cmd_group_reply_rate_impl(
            self, event, group_id, rate))

    @filter.command("dududa_meme_rate")
    async def cmd_group_meme_rate(self, event: AstrMessageEvent,
                                  group_id: str = None, rate: str = None):
        yield event.plain_result(await dududa_commands.cmd_group_meme_rate_impl(
            self, event, group_id, rate))

    @filter.command("dududa_interrupt_cost")
    async def cmd_group_interrupt_cost(self, event: AstrMessageEvent,
                                       group_id: str = None, cost: str = None):
        yield event.plain_result(await dududa_commands.cmd_group_interrupt_cost_impl(
            self, event, group_id, cost))

    @filter.command("dududa_style")
    async def cmd_style(self, event: AstrMessageEvent):
        yield event.plain_result(await dududa_commands.cmd_style_impl(self, event))

    @filter.command("dududa_forget")
    async def cmd_forget(self, event: AstrMessageEvent):
        yield event.plain_result(await dududa_commands.cmd_forget_impl(self, event))

    @filter.command("dududa_help", alias={"嘟嘟哒帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看当前真正可用的能力和常用命令。"""
        yield event.plain_result(await dududa_commands.cmd_help_impl(self))

    @filter.command("dududa_feedback", alias={"问题反馈"})
    async def cmd_feedback(self, event: AstrMessageEvent, summary: str = None):
        """主动提交脱敏改进反馈，不触发自动修改或部署。"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=1)
        if len(parts) == 2:
            summary = parts[1]
        yield event.plain_result(await dududa_commands.cmd_feedback_impl(
            self, summary or ""))

    @filter.command("dududa_cancel", alias={"取消任务"})
    async def cmd_cancel(self, event: AstrMessageEvent):
        """取消当前会话正在执行的慢任务。"""
        yield event.plain_result(await dududa_commands.cmd_cancel_impl(self, event))

    @filter.command("dududa_memory", alias={"我的记忆"})
    async def cmd_memory(self, event: AstrMessageEvent,
                         action: str = "status", record_id: str = None):
        """查看、删除或暂停自己的记忆。"""
        yield event.plain_result(await dududa_commands.cmd_memory_impl(
            self, event, action, record_id))

    @filter.command("dududa_subscribe", alias={"订阅管理"})
    async def cmd_subscribe(self, event: AstrMessageEvent,
                            action: str = "list", topic: str = "更新"):
        """显式订阅、退订以及设置免打扰时间。"""
        yield event.plain_result(await dududa_commands.cmd_subscribe_impl(
            self, event, action, topic))

    @filter.command("dududa_broadcast")
    async def cmd_broadcast(self, event: AstrMessageEvent,
                            topic: str = None, message: str = None):
        """管理员生成订阅推送预览，不会立即发送。"""
        raw = str(getattr(event, "message_str", "") or "").strip()
        parts = raw.split(maxsplit=2)
        if len(parts) >= 3:
            topic, message = parts[1], parts[2]
        yield event.plain_result(await dududa_commands.cmd_broadcast_prepare_impl(
            self, event, topic, message))

    @filter.command("dududa_broadcast_confirm")
    async def cmd_broadcast_confirm(self, event: AstrMessageEvent,
                                    broadcast_id: str = None):
        """管理员确认向符合条件的显式订阅者发送预览。"""
        yield event.plain_result(await dududa_commands.cmd_broadcast_confirm_impl(
            self, event, broadcast_id))

    async def terminate(self):
        self.ux_tasks.cancel_all()
