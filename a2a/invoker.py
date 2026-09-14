"""AgentInvoker 抽象（docs/05 §8、FR-099）。

``single`` 与 ``a2a`` 必须复用同一套接口、Artifact 模型和安全门禁：
- ``InProcessInvoker``：进程内直接调用 Agent handler；
- ``A2AInvoker``：HTTP/SSE 传输。

两种实现都必须创建 Task、校验 Agent Card、记录 Message 与 Artifact，
并且都需要一个数据库会话来完成状态落库。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from a2a.protocol import A2ATask, ArtifactEnvelope, TaskHandle
from domain.enums import ChildTaskStatus


@dataclass(slots=True)
class ChildTaskOutcome:
    """一次子任务执行的结果（两种 Invoker 共用）。"""

    task_id: str
    status: ChildTaskStatus
    artifacts: list[ArtifactEnvelope] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    replayed: bool = False
    transport: str = "inprocess"
    degraded_reason: str | None = None

    @property
    def completed(self) -> bool:
        return self.status is ChildTaskStatus.COMPLETED


class AgentInvoker(ABC):
    """Coordinator 调用子 Agent 的唯一入口。"""

    mode: str = "abstract"

    @abstractmethod
    async def submit(self, task: A2ATask, *, session: Session) -> TaskHandle:
        """提交子任务；相同幂等键必须返回同一个 TaskHandle。"""

    @abstractmethod
    async def wait(self, handle: TaskHandle, *, session: Session) -> list[ArtifactEnvelope]:
        """等待子任务进入终态并返回通过校验的 Artifact。"""

    @abstractmethod
    async def execute(self, task_id: str, *, session: Session) -> ChildTaskOutcome:
        """执行（或幂等返回）子任务，负责终态收敛与 Artifact 入库。"""

    @abstractmethod
    async def cancel(self, task_id: str, *, session: Session) -> None:
        """请求取消未完成子任务。"""

    @abstractmethod
    async def status(self, task_id: str, *, session: Session) -> str:
        """查询子任务状态。

        超时/断线/重启后必须先调用本方法对账，禁止未知状态下盲目重放（宪法第七条）。
        """


__all__ = ["AgentInvoker", "ChildTaskOutcome"]
