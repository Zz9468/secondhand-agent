from decimal import Decimal, InvalidOperation


class ReplySafetyError(ValueError):
    """正式回复无法从可信业务结果生成时抛出的异常。"""


class FormalReplyRenderer:
    """只根据已校验并持久化的报价字段生成正式交易回复。"""

    def render_counter_offer(self, tool_result: dict[str, object]) -> str:
        offer = self._validated_offer(
            tool_result,
            expected_proposer="AGENT",
            expected_status="PROPOSED",
        )
        return (
            f"我可以提出的正式还价是 {self._money(offer['price'])} 元，"
            f"{self._shipping_text(offer)}{self._discount_text(offer)}"
            f"{self._delivery_text(offer)}。"
            "如果你能接受，请明确确认这个报价。"
        )

    def render_accepted_offer(self, tool_result: dict[str, object]) -> str:
        offer = self._validated_offer(
            tool_result,
            expected_proposer="BUYER",
            expected_status="ACCEPTED",
        )
        return (
            f"可以接受你提出的 {self._money(offer['price'])} 元，"
            f"{self._shipping_text(offer)}{self._discount_text(offer)}"
            f"{self._delivery_text(offer)}。"
            "请确认是否按这个条件继续；当前仅代表协商条件已接受，不代表已经成交。"
        )

    def render_approved_followup(self, tool_result: dict[str, object]) -> str:
        """审批通过后只表达卖家授权，仍要求买家明确确认。"""

        offer = self._validated_offer(
            tool_result,
            expected_proposer="BUYER",
            expected_status="PROPOSED",
        )
        return (
            f"卖家已同意你提出的 {self._money(offer['price'])} 元，"
            f"{self._shipping_text(offer)}{self._discount_text(offer)}"
            f"{self._delivery_text(offer)}。"
            "请明确确认是否按这个报价继续；当前仅代表卖家授权，不代表已经成交。"
        )

    def render_rejected_followup(self, tool_result: dict[str, object]) -> str:
        """审批拒绝通知只引用被拒绝的不可变报价快照。"""

        offer = self._validated_offer(
            tool_result,
            expected_proposer="BUYER",
            expected_status="REJECTED",
        )
        return (
            f"卖家未同意你提出的 {self._money(offer['price'])} 元，"
            f"{self._shipping_text(offer)}{self._discount_text(offer)}"
            f"{self._delivery_text(offer)}。"
            "如果商品仍可协商，你可以调整条件后重新报价。"
        )

    def render_invalidated_followup(self, tool_result: dict[str, object]) -> str:
        """真实状态变化时不再发送原审批承诺。"""

        offer = self._validated_offer(
            tool_result,
            expected_proposer="BUYER",
            expected_status={"PROPOSED", "ACCEPTED", "REJECTED", "WITHDRAWN"},
        )
        return (
            f"关于 {self._money(offer['price'])} 元的报价，"
            "审批完成后商品或交易条件已经变化，本次授权不再有效。"
            "本次审批等待已经结束，请刷新商品状态后再决定是否重新报价。"
        )

    @staticmethod
    def _validated_offer(
        tool_result: dict[str, object],
        *,
        expected_proposer: str,
        expected_status: str | set[str],
    ) -> dict[str, object]:
        if tool_result.get("ok") is not True:
            raise ReplySafetyError("工具没有返回成功的正式报价")
        offer = tool_result.get("offer")
        if not isinstance(offer, dict):
            raise ReplySafetyError("工具结果缺少正式报价快照")
        if offer.get("proposer") != expected_proposer:
            raise ReplySafetyError("正式报价提出方与回复动作不一致")
        actual_status = offer.get("status")
        if (
            actual_status != expected_status
            if isinstance(expected_status, str)
            else actual_status not in expected_status
        ):
            raise ReplySafetyError("正式报价状态与回复动作不一致")
        if type(offer.get("id")) is not int or offer["id"] <= 0:
            raise ReplySafetyError("正式报价缺少有效编号")
        return offer

    @staticmethod
    def _money(value: object) -> str:
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ReplySafetyError("正式报价金额无效") from exc
        if not amount.is_finite() or amount < 0:
            raise ReplySafetyError("正式报价金额无效")
        return format(amount, ".2f")

    @staticmethod
    def _shipping_text(offer: dict[str, object]) -> str:
        payer = offer.get("shipping_paid_by")
        if payer == "seller":
            return "包邮（运费由卖家承担）"
        if payer == "buyer":
            return "不包邮（运费由买家承担）"
        raise ReplySafetyError("正式报价缺少有效运费承担方")

    @classmethod
    def _discount_text(cls, offer: dict[str, object]) -> str:
        discount = cls._money(offer.get("seller_borne_discount", "0.00"))
        if Decimal(discount) == Decimal("0.00"):
            return ""
        return f"，另含卖家承担优惠 {discount} 元"

    @staticmethod
    def _delivery_text(offer: dict[str, object]) -> str:
        additional_terms = offer.get("additional_terms")
        if not isinstance(additional_terms, dict) or not additional_terms:
            return ""
        delivery_method = additional_terms.get("delivery_method")
        if delivery_method == "pickup":
            return "，交易方式为面交"
        if delivery_method == "shipping":
            return "，交易方式为快递"
        raise ReplySafetyError("正式报价包含无法安全表达的附加条件")
