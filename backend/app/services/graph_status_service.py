"""仅暴露调用方会话的业务/通知状态，不返回内部输入或模型内容。"""

from sqlalchemy import select

from app.db.models import Message, ModelExecutionTask
from app.db.models.graph import BusinessActionReceipt, NegotiationInput, NotificationDelivery
from app.runtime.contracts import Conflict
from app.runtime.input_queue import owned_session


class GraphStatusService:
    def __init__(self, factory):
        self.factory = factory

    def get(self, sid, buyer_id, run_id):
        with self.factory() as db:
            session = owned_session(db, sid, buyer_id)
            run = db.get(NegotiationInput, run_id)
            if not run or run.session_id != sid:
                raise Conflict("运行不存在或无权访问")
            return self._public_status(db, session, run)

    def get_request(self, sid, buyer_id, request_id):
        with self.factory() as db:
            session = owned_session(db, sid, buyer_id)
            run = db.scalar(
                select(NegotiationInput).where(
                    NegotiationInput.session_id == sid,
                    NegotiationInput.request_id == request_id,
                )
            )
            if not run:
                raise Conflict("请求不存在或无权访问")
            return self._public_status(db, session, run)

    @staticmethod
    def _public_status(db, session, run):
        run_id = run.id
        task = db.get(ModelExecutionTask, run.task_id)
        receipt = db.get(BusinessActionReceipt, f"turn:{run_id}:action")
        delivery = db.get(NotificationDelivery, f"turn:{run_id}:reply")
        message = (
            db.get(Message, delivery.public_message_id)
            if delivery and delivery.public_message_id
            else None
        )
        return {
            "run_id": run.id,
            "input_seq": run.input_seq,
            "task_status": task.status.value,
            "blocked_by_input_seq": session.last_resolved_input_seq + 1
            if run.input_seq > session.last_resolved_input_seq + 1
            else None,
            "business_status": "COMMITTED" if receipt else "NOT_COMMITTED",
            "business_outcome": receipt.result["outcome"] if receipt else None,
            "notification_status": delivery.status if delivery else "NOT_REQUIRED",
            "public_message_id": message.id if message else None,
            "recovery_required": run.recovery_required,
            "retryable": not run.recovery_required
            and task.status.value in {"PENDING", "RETRY_WAIT"},
            "public_error_category": "RECOVERY_REQUIRED"
            if run.recovery_required
            else "RESULT_DELIVERY_DELAYED"
            if delivery and delivery.status != "PUBLISHED"
            else None,
        }
