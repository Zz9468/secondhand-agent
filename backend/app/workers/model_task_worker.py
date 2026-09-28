import argparse
import logging
import socket
import time
from dataclasses import dataclass
from uuid import uuid4

from app.agent.approval_followup import LangChainApprovalFollowupProvider
from app.agent.decision_provider import LangChainDecisionProvider
from app.agent.model_factory import QwenChatModelFactory
from app.core.config import get_settings
from app.db.models import ApprovalFollowupStatus, ModelTaskStatus
from app.db.session import get_session_factory
from app.services.chat_service import ChatService, ChatTaskProcessingResult
from app.workers.approval_processor import ApprovalProcessingResult, ApprovalProcessor

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelTaskWorkerBatch:
    chat_results: tuple[ChatTaskProcessingResult, ...]
    approval_results: tuple[ApprovalProcessingResult, ...]

    @property
    def processed_count(self) -> int:
        return len(self.chat_results) + len(self.approval_results)

    @property
    def requires_manual_action(self) -> bool:
        return any(
            item.status is ModelTaskStatus.FAILED for item in self.chat_results
        ) or any(
            item.status is ApprovalFollowupStatus.MANUAL_REQUIRED
            for item in self.approval_results
        )


class ModelTaskWorker:
    """公平轮询聊天决策与审批回访，并接管到期的 Worker 租约。"""

    def __init__(
        self,
        *,
        chat_service: ChatService,
        approval_processor: ApprovalProcessor,
        worker_id: str,
    ) -> None:
        self._chat_service = chat_service
        self._approval_processor = approval_processor
        self._worker_id = worker_id

    def process_batch(self, *, limit: int = 20) -> ModelTaskWorkerBatch:
        if not 1 <= limit <= 200:
            raise ValueError("单轮模型任务数量必须在 1 到 200 之间")

        chat_results: list[ChatTaskProcessingResult] = []
        approval_results: list[ApprovalProcessingResult] = []
        excluded_approval_ids: set[int] = set()
        prefer_chat = True

        while len(chat_results) + len(approval_results) < limit:
            processed = False
            if prefer_chat:
                chat_result = self._chat_service.process_next_pending_task(
                    worker_id=self._worker_id
                )
                if chat_result is not None:
                    chat_results.append(chat_result)
                    processed = True
                else:
                    approval_result = self._approval_processor.process_next(
                        excluded_approval_ids=excluded_approval_ids
                    )
                    if approval_result is not None:
                        excluded_approval_ids.add(approval_result.approval_id)
                        approval_results.append(approval_result)
                        processed = True
            else:
                approval_result = self._approval_processor.process_next(
                    excluded_approval_ids=excluded_approval_ids
                )
                if approval_result is not None:
                    excluded_approval_ids.add(approval_result.approval_id)
                    approval_results.append(approval_result)
                    processed = True
                else:
                    chat_result = self._chat_service.process_next_pending_task(
                        worker_id=self._worker_id
                    )
                    if chat_result is not None:
                        chat_results.append(chat_result)
                        processed = True

            if not processed:
                break
            prefer_chat = not prefer_chat

        return ModelTaskWorkerBatch(
            chat_results=tuple(chat_results),
            approval_results=tuple(approval_results),
        )


def _build_worker(*, worker_id: str) -> ModelTaskWorker:
    model = QwenChatModelFactory().create(get_settings())
    session_factory = get_session_factory()
    return ModelTaskWorker(
        chat_service=ChatService(
            session_factory,
            LangChainDecisionProvider(model),
        ),
        approval_processor=ApprovalProcessor(
            session_factory,
            LangChainApprovalFollowupProvider(model),
        ),
        worker_id=worker_id,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="处理到期模型任务并接管过期 Worker 租约"
    )
    parser.add_argument("--once", action="store_true", help="只处理一批任务后退出")
    parser.add_argument("--batch-size", type=int, default=20, help="每轮最多处理任务数")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="轮询间隔秒数")
    parser.add_argument("--worker-id", help="可选的 Worker 实例标识")
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 200:
        parser.error("--batch-size 必须在 1 到 200 之间")
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds 必须大于 0")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker_id = args.worker_id or f"model-worker:{socket.gethostname()}:{uuid4().hex[:12]}"
    worker = _build_worker(worker_id=worker_id)
    while True:
        batch = worker.process_batch(limit=args.batch_size)
        logger.info(
            "model_task_worker worker_id=%s chat=%s approval=%s manual=%s",
            worker_id,
            len(batch.chat_results),
            len(batch.approval_results),
            batch.requires_manual_action,
        )
        if args.once:
            return 1 if batch.requires_manual_action else 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
