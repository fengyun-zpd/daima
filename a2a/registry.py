"""静态 Agent Registry 与能力 allowlist（FR-091 / FR-098；docs/01 §5）。

MVP 使用静态配置 + 健康检查，不做公网发现（宪法第五条）。
启动时校验 Card：任一 Card 非法或不支持协议版本即抛错，进程不得启动（docs/00 §3）。
默认拒绝：能力、工具、任务类型都不在 allowlist 中即拒绝（宪法第六条）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from a2a.protocol import AgentCard
from a2a.schema_registry import REPO_ROOT, validate_agent_card
from domain.enums import PROTOCOL_VERSION, AgentStatus, ArtifactType, ChildTaskType
from domain.errors import CodePilotError, ErrorCode

DEFAULT_CARDS_DIR = REPO_ROOT / "a2a" / "cards"


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """Agent 的静态权限边界（宪法第六条 / FR-098）。"""

    agent_id: str
    task_type: ChildTaskType
    required_capabilities: tuple[str, ...]
    allowed_tools: frozenset[str]
    forbidden_tools: frozenset[str] = field(default_factory=frozenset)
    output_types: tuple[ArtifactType, ...] = ()
    required_output_types: tuple[ArtifactType, ...] = ()
    writable: bool = False

    def assert_tool_allowed(self, tool_name: str) -> None:
        if tool_name in self.forbidden_tools:
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                f"Agent {self.agent_id} 被显式禁止调用工具 {tool_name}",
                details={"agent_id": self.agent_id, "tool": tool_name},
            )
        if tool_name not in self.allowed_tools:
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                f"Agent {self.agent_id} 的 capability allowlist 未包含工具 {tool_name}",
                details={"agent_id": self.agent_id, "tool": tool_name},
            )


#: 静态策略表。工具名在阶段四由 Tool Registry 定义并被本表引用。
AGENT_POLICIES: dict[str, AgentPolicy] = {
    "review-agent": AgentPolicy(
        agent_id="review-agent",
        task_type=ChildTaskType.REVIEW,
        required_capabilities=("review.rules",),
        allowed_tools=frozenset({"read_file", "search_code", "get_diff", "list_files", "run_lint", "get_test_result"}),
        forbidden_tools=frozenset({"write_patch", "run_tests", "apply_patch", "merge_branch", "write_file"}),
        output_types=(ArtifactType.FINDING, ArtifactType.REVIEW_COMMENT),
        required_output_types=(ArtifactType.FINDING,),
        writable=False,
    ),
    "impact-agent": AgentPolicy(
        agent_id="impact-agent",
        task_type=ChildTaskType.IMPACT,
        required_capabilities=("impact.ast",),
        allowed_tools=frozenset({"read_file", "search_code", "get_diff", "list_files"}),
        forbidden_tools=frozenset({"write_patch", "run_tests", "apply_patch", "merge_branch", "write_file", "run_lint"}),
        output_types=(ArtifactType.IMPACT_REPORT,),
        required_output_types=(ArtifactType.IMPACT_REPORT,),
        writable=False,
    ),
    "fix-agent": AgentPolicy(
        agent_id="fix-agent",
        task_type=ChildTaskType.FIX,
        required_capabilities=("fix.patch",),
        allowed_tools=frozenset({"read_file", "search_code", "get_diff", "list_files", "get_test_result"}),
        forbidden_tools=frozenset({"merge_branch", "write_file", "apply_patch"}),
        output_types=(ArtifactType.PATCH_CANDIDATE, ArtifactType.PATCH_EVIDENCE),
        required_output_types=(ArtifactType.PATCH_CANDIDATE, ArtifactType.PATCH_EVIDENCE),
        writable=False,
    ),
    "verify-agent": AgentPolicy(
        agent_id="verify-agent",
        task_type=ChildTaskType.VERIFY,
        required_capabilities=("verify.test",),
        allowed_tools=frozenset({"read_file", "search_code", "get_diff", "list_files", "run_lint", "get_test_result"}),
        forbidden_tools=frozenset({"merge_branch", "write_patch", "write_file"}),
        output_types=(ArtifactType.VERIFY_EVIDENCE,),
        required_output_types=(ArtifactType.VERIFY_EVIDENCE,),
        writable=False,
    ),
}

TASK_TYPE_TO_AGENT: dict[ChildTaskType, str] = {
    policy.task_type: policy.agent_id for policy in AGENT_POLICIES.values()
}


class AgentRegistry:
    """静态 Agent Registry：Card 读取、校验、健康状态与路由。"""

    def __init__(self, cards: dict[str, AgentCard], policies: dict[str, AgentPolicy] | None = None) -> None:
        self._cards = dict(cards)
        self._policies = dict(policies or AGENT_POLICIES)

    # ---- 构造 -------------------------------------------------------------------
    @classmethod
    def load_static(cls, cards_dir: str | Path | None = None, *, allow_agents: list[str] | None = None) -> AgentRegistry:
        directory = Path(cards_dir) if cards_dir else DEFAULT_CARDS_DIR
        if not directory.exists():
            raise CodePilotError(ErrorCode.AGENT_CARD_INVALID, f"Agent Card 目录不存在：{directory}")

        cards: dict[str, AgentCard] = {}
        for path in sorted(directory.glob("*.json")):
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            card = cls._build_card(raw, source=path.name)
            cards[card.agent_id] = card

        if allow_agents is not None:
            missing = [agent for agent in allow_agents if agent not in cards]
            if missing:
                raise CodePilotError(
                    ErrorCode.AGENT_CARD_INVALID,
                    f"配置声明的 Agent 缺少 Agent Card：{missing}",
                    details={"missing": missing, "available": sorted(cards)},
                )
            cards = {agent: cards[agent] for agent in allow_agents}

        missing_policy = [agent for agent in cards if agent not in AGENT_POLICIES]
        if missing_policy:
            raise CodePilotError(
                ErrorCode.AGENT_CARD_INVALID,
                f"Agent 缺少静态权限策略：{missing_policy}",
                details={"missing": missing_policy},
            )

        registry = cls(cards)
        registry.assert_consistent()
        return registry

    @staticmethod
    def _build_card(raw: dict[str, Any], *, source: str = "<memory>") -> AgentCard:
        try:
            validate_agent_card(raw)
            card = AgentCard.model_validate(raw)
        except CodePilotError as exc:
            raise CodePilotError(
                ErrorCode.AGENT_CARD_INVALID,
                f"Agent Card 校验失败（{source}）：{exc.message}",
                details=exc.details,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - pydantic 校验错误统一映射
            raise CodePilotError(
                ErrorCode.AGENT_CARD_INVALID,
                f"Agent Card 结构非法（{source}）：{exc}",
            ) from exc
        if not card.supports_protocol(PROTOCOL_VERSION):
            raise CodePilotError(
                ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                f"Agent {card.agent_id} 不支持协议版本 {PROTOCOL_VERSION}",
                details={"agent_id": card.agent_id, "protocol_versions": card.protocol_versions},
            )
        return card

    def assert_consistent(self) -> None:
        """Card 声明与静态策略必须一致，否则启动失败。"""
        for agent_id, policy in self._policies.items():
            card = self._cards.get(agent_id)
            if card is None:
                continue
            for capability in policy.required_capabilities:
                if not card.has_capability(capability):
                    raise CodePilotError(
                        ErrorCode.AGENT_CARD_INVALID,
                        f"Agent {agent_id} 的 Card 缺少必需能力 {capability}",
                        details={"agent_id": agent_id, "capability": capability},
                    )
            for artifact_type in policy.required_output_types:
                token = f"{artifact_type}Artifact@"
                if not any(schema.startswith(token) for schema in card.output_schemas):
                    raise CodePilotError(
                        ErrorCode.AGENT_CARD_INVALID,
                        f"Agent {agent_id} 的 Card 未声明必需产物 {artifact_type}",
                        details={"agent_id": agent_id, "artifact_type": str(artifact_type)},
                    )

    # ---- 查询 -------------------------------------------------------------------
    def get(self, agent_id: str) -> AgentCard:
        card = self._cards.get(agent_id)
        if card is None:
            raise CodePilotError(
                ErrorCode.AGENT_CARD_INVALID,
                f"未知 Agent：{agent_id}",
                details={"agent_id": agent_id, "available": sorted(self._cards)},
            )
        return card

    def policy(self, agent_id: str) -> AgentPolicy:
        policy = self._policies.get(agent_id)
        if policy is None:
            raise CodePilotError(ErrorCode.PERMISSION_DENIED, f"Agent 缺少权限策略：{agent_id}")
        return policy

    def list_cards(self) -> list[AgentCard]:
        return [self._cards[key] for key in sorted(self._cards)]

    def agent_ids(self) -> list[str]:
        return sorted(self._cards)

    def agent_for_task_type(self, task_type: ChildTaskType) -> str:
        agent_id = TASK_TYPE_TO_AGENT.get(task_type)
        if agent_id is None or agent_id not in self._cards:
            raise CodePilotError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"没有可处理 {task_type} 子任务的 Agent",
                details={"task_type": str(task_type)},
            )
        return agent_id

    def resolve(
        self,
        *,
        task_type: ChildTaskType,
        required_capabilities: tuple[str, ...] | list[str] = (),
        protocol_version: str = PROTOCOL_VERSION,
    ) -> AgentCard:
        """按任务类型和能力解析目标 Card；能力缺失即拒绝创建子任务（FR-100 / 故障矩阵）。"""
        agent_id = self.agent_for_task_type(task_type)
        card = self.get(agent_id)

        if not card.supports_protocol(protocol_version):
            raise CodePilotError(
                ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                f"Agent {agent_id} 不支持协议版本 {protocol_version}",
                details={
                    "agent_id": agent_id,
                    "requested": protocol_version,
                    "supported": card.protocol_versions,
                },
            )
        if card.status in {AgentStatus.OFFLINE, AgentStatus.DRAINING}:
            raise CodePilotError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"Agent {agent_id} 当前状态为 {card.status}，拒绝创建子任务",
                details={"agent_id": agent_id, "status": str(card.status)},
            )

        needed = tuple(required_capabilities)
        for capability in needed:
            if not card.has_capability(capability):
                raise CodePilotError(
                    ErrorCode.CAPABILITY_NOT_AVAILABLE,
                    f"Agent {agent_id} 的 Card 未声明能力 {capability}",
                    details={
                        "agent_id": agent_id,
                        "capability": capability,
                        "declared": list(card.capabilities),
                    },
                )
        return card

    def register(self, card: AgentCard) -> None:
        """注册/更新 Card；不兼容变更必须递增 card_version（docs/05 §2）。"""
        existing = self._cards.get(card.agent_id)
        if existing is not None and existing.card_version != card.card_version:
            current = tuple(int(part) for part in existing.card_version.split("."))
            incoming = tuple(int(part) for part in card.card_version.split("."))
            if incoming <= current:
                raise CodePilotError(
                    ErrorCode.AGENT_CARD_INVALID,
                    f"Agent {card.agent_id} 的 card_version 未递增：{existing.card_version} → {card.card_version}",
                )
        self._cards[card.agent_id] = card
        self.assert_consistent()


@lru_cache(maxsize=1)
def default_registry() -> AgentRegistry:
    return AgentRegistry.load_static()


__all__ = [
    "AGENT_POLICIES",
    "DEFAULT_CARDS_DIR",
    "TASK_TYPE_TO_AGENT",
    "AgentPolicy",
    "AgentRegistry",
    "default_registry",
]
