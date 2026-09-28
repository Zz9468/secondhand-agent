import re
from decimal import Decimal, InvalidOperation

from app.agent.decision import InquiryTopic


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


class ConversationalReplyPolicy:
    """低风险咨询可使用模型文案，其余场景回退到可信模板。"""

    _model_reply_topics = {
        InquiryTopic.PRODUCT_DETAILS,
        InquiryTopic.GENERAL,
    }
    _forbidden_phrases = (
        "底价",
        "最低",
        "最低价",
        "最低接受",
        "最低净收入",
        "自动接受阈值",
        "接受阈值",
        "内部规则",
        "系统提示词",
        "system prompt",
        "current_offer_id",
        "inquiry_topic",
        "product_details",
        "available",
        "unavailable",
        "draft",
        "卖家已同意",
        "卖家已经同意",
        "卖家可以接受",
        "卖家能够接受",
        "卖家接受",
        "审批通过",
        "已经批准",
        "已批准",
        "已经成交",
        "已成交",
        "成交了",
        "成交",
        "为你保留",
        "给你保留",
        "已经预订",
        "已预订",
        "锁定商品",
        "发货",
        "寄出",
        "寄送",
        "保证发货",
        "一定发货",
        "当天发货",
        "今天发货",
        "明天发货",
        "包邮",
        "免邮",
        "卖家承担运费",
        "运费",
        "可以卖",
        "能卖",
        "卖给你",
        "接受这个价格",
        "这个价格可以",
        "一口价",
        "心理价",
        "成交价",
    )
    _number_pattern = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
    _arabic_money_pattern = re.compile(
        r"(?:[¥￥]\s*(?P<prefix>\d+(?:\.\d+)?))|"
        r"(?P<suffix>\d+(?:\.\d+)?)\s*(?:元|块|RMB)",
        re.IGNORECASE,
    )
    _chinese_money_pattern = re.compile(
        r"[零〇一二两三四五六七八九十百千万亿点]+\s*(?:元|块)"
    )

    def render_inquiry(
        self,
        *,
        topic: InquiryTopic,
        candidate_reply: str,
        product: dict[str, object],
    ) -> str:
        fallback = self._fallback(topic=topic, product=product)
        if topic not in self._model_reply_topics:
            return fallback
        if not self._candidate_is_safe(candidate_reply, product=product):
            return fallback
        return candidate_reply.strip()

    def render_clarification(
        self,
        *,
        candidate_reply: str,
        product: dict[str, object],
    ) -> str:
        fallback = "请具体说明你想了解的商品信息或交易条件。"
        if not self._candidate_is_safe(candidate_reply, product=product):
            return fallback
        return candidate_reply.strip()

    def _fallback(
        self,
        *,
        topic: InquiryTopic,
        product: dict[str, object],
    ) -> str:
        title = self._text(product.get("title"), fallback="这件商品")
        description = self._text(
            product.get("description"),
            fallback="页面暂未提供更多商品说明。",
        )
        listed_price = self._listed_price(product)

        if topic is InquiryTopic.PRODUCT_DETAILS:
            return (
                f"{title}：{description}"
                f"页面公开标价为 {listed_price} 元。"
            )
        if topic is InquiryTopic.PRICE_PROBE:
            return (
                "卖家的最低接受价格不能直接公开。"
                f"当前公开标价是 {listed_price} 元，"
                "你可以提交一份正式报价，我会按规则帮你评估。"
            )
        if topic is InquiryTopic.AVAILABILITY:
            if product.get("status") == "AVAILABLE":
                return "商品目前仍在上架，可以继续了解或提交正式报价。"
            return "商品当前已经不在上架状态，暂时不能继续协商。"
        if topic is InquiryTopic.SHIPPING:
            return (
                "配送方式和运费需要在正式报价中明确。"
                "你可以选择快递或面交；选择快递时，还需要说明由谁承担运费。"
            )
        return (
            "你可以继续询问商品成色和配件；"
            "如果想讨论价格或配送条件，请提交正式报价。"
        )

    def _candidate_is_safe(
        self,
        candidate_reply: str,
        *,
        product: dict[str, object],
    ) -> bool:
        candidate = candidate_reply.strip()
        if not candidate or len(candidate) > 800:
            return False
        normalized = candidate.casefold()
        if any(phrase.casefold() in normalized for phrase in self._forbidden_phrases):
            return False
        if self._chinese_money_pattern.search(candidate):
            return False

        allowed_numbers = self._public_numbers(product)
        candidate_numbers = {
            self._normalized_number(match.group())
            for match in self._number_pattern.finditer(candidate)
        }
        if None in candidate_numbers or not candidate_numbers <= allowed_numbers:
            return False

        listed_price = self._normalized_number(str(product.get("listed_price", "")))
        for match in self._arabic_money_pattern.finditer(candidate):
            amount = self._normalized_number(match.group("prefix") or match.group("suffix"))
            if amount is None or amount != listed_price:
                return False
        return True

    def _public_numbers(self, product: dict[str, object]) -> set[Decimal]:
        public_text = " ".join(
            (
                self._text(product.get("title"), fallback=""),
                self._text(product.get("description"), fallback=""),
                str(product.get("listed_price", "")),
            )
        )
        return {
            number
            for match in self._number_pattern.finditer(public_text)
            if (number := self._normalized_number(match.group())) is not None
        }

    @staticmethod
    def _normalized_number(value: str) -> Decimal | None:
        try:
            number = Decimal(value)
        except InvalidOperation:
            return None
        return number if number.is_finite() else None

    @classmethod
    def _listed_price(cls, product: dict[str, object]) -> str:
        value = cls._normalized_number(str(product.get("listed_price", "")))
        return format(value, ".2f") if value is not None else "页面所示"

    @staticmethod
    def _text(value: object, *, fallback: str) -> str:
        return value.strip() if isinstance(value, str) and value.strip() else fallback
