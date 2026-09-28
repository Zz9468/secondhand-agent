import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.agent.decision import (
    DialogueAct,
    DialogueActKind,
    DialogueSubject,
)


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


class ReplyDirectiveKind(StrEnum):
    """后端允许回复层表达的原子指令。"""

    PRODUCT_DETAILS = "PRODUCT_DETAILS"
    LISTED_PRICE = "LISTED_PRICE"
    PRIVATE_PRICE = "PRIVATE_PRICE"
    AVAILABILITY = "AVAILABILITY"
    FORMAL_OFFER_TERM = "FORMAL_OFFER_TERM"
    SELLER_CONFIRMATION = "SELLER_CONFIRMATION"
    ASK_MISSING_TERM = "ASK_MISSING_TERM"
    GENERAL = "GENERAL"
    CANNOT_HANDLE = "CANNOT_HANDLE"


@dataclass(frozen=True, slots=True)
class ReplyDirective:
    """一项经过后端策略裁决、可以安全表达的回复指令。"""

    kind: ReplyDirectiveKind
    subject: DialogueSubject


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    """与自然语言措辞解耦的可信回复计划。"""

    directives: tuple[ReplyDirective, ...]
    allow_model_candidate: bool
    needs_clarification: bool


@dataclass(frozen=True, slots=True)
class DialogueReply:
    """对话策略执行结果。"""

    text: str
    needs_clarification: bool


class DialoguePolicyService:
    """将模型解析出的语义指令裁决为可信回复计划并安全表达。"""

    _model_reply_subjects = {
        DialogueSubject.PRODUCT_DETAILS,
        DialogueSubject.GENERAL,
    }
    _forbidden_phrases = (
        "底价",
        "底價",
        "最低",
        "最低价",
        "最低價",
        "最低接受",
        "最低净收入",
        "自动接受阈值",
        "接受阈值",
        "内部规则",
        "系统提示词",
        "system prompt",
        "internal rule",
        "floor price",
        "minimum price",
        "lowest price",
        "precio mínimo",
        "current_offer_id",
        "dialogue_acts",
        "requested_value",
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
        "審批通過",
        "已经批准",
        "已批准",
        "seller approved",
        "seller has approved",
        "approved by seller",
        "approval granted",
        "seller agreed",
        "seller accepts",
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
        "deal confirmed",
        "deal is done",
        "sold to you",
        "reserved for you",
        "hold it for you",
        "free shipping",
        "shipping is free",
        "seller pays shipping",
        "ship today",
        "ship tomorrow",
        "dispatch today",
        "guaranteed shipping",
        "guarantee delivery",
        "envío gratis",
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

    def resolve(
        self,
        *,
        acts: list[DialogueAct],
        candidate_reply: str,
        product: dict[str, object],
    ) -> DialogueReply:
        plan = self.build_plan(acts)
        if plan.allow_model_candidate and self._candidate_is_safe(
            candidate_reply,
            product=product,
        ):
            return DialogueReply(
                text=candidate_reply.strip(),
                needs_clarification=False,
            )
        return DialogueReply(
            text=self._render_plan(plan=plan, product=product),
            needs_clarification=plan.needs_clarification,
        )

    def build_plan(self, acts: list[DialogueAct]) -> ReplyPlan:
        """语义相同的不同措辞会得到同一份后端回复计划。"""

        directives: list[ReplyDirective] = []
        needs_clarification = False
        seen: set[tuple[ReplyDirectiveKind, DialogueSubject]] = set()

        for act in acts:
            directive, missing_term = self._resolve_act(act)
            key = (directive.kind, directive.subject)
            if key not in seen:
                directives.append(directive)
                seen.add(key)
            needs_clarification = needs_clarification or missing_term

        if not directives:
            directives.append(
                ReplyDirective(
                    kind=ReplyDirectiveKind.CANNOT_HANDLE,
                    subject=DialogueSubject.OTHER,
                )
            )
            needs_clarification = True

        allow_model_candidate = all(
            directive.kind in {
                ReplyDirectiveKind.PRODUCT_DETAILS,
                ReplyDirectiveKind.GENERAL,
            }
            and directive.subject in self._model_reply_subjects
            for directive in directives
        )
        return ReplyPlan(
            directives=tuple(directives),
            allow_model_candidate=allow_model_candidate,
            needs_clarification=needs_clarification,
        )

    @staticmethod
    def _resolve_act(act: DialogueAct) -> tuple[ReplyDirective, bool]:
        subject = act.subject
        if (
            act.kind is DialogueActKind.ASK_PRIVATE_INFO
            or subject is DialogueSubject.PRICE_FLOOR
        ):
            return ReplyDirective(ReplyDirectiveKind.PRIVATE_PRICE, subject), False
        if act.kind is DialogueActKind.REQUEST_COMMITMENT:
            return ReplyDirective(ReplyDirectiveKind.SELLER_CONFIRMATION, subject), False
        if act.kind is DialogueActKind.UNKNOWN or subject is DialogueSubject.OTHER:
            return ReplyDirective(ReplyDirectiveKind.CANNOT_HANDLE, subject), True
        if act.kind is DialogueActKind.GENERAL or subject is DialogueSubject.GENERAL:
            return ReplyDirective(ReplyDirectiveKind.GENERAL, subject), False
        if subject is DialogueSubject.PRODUCT_DETAILS:
            return ReplyDirective(ReplyDirectiveKind.PRODUCT_DETAILS, subject), False
        if subject is DialogueSubject.LISTED_PRICE:
            return ReplyDirective(ReplyDirectiveKind.LISTED_PRICE, subject), False
        if subject is DialogueSubject.AVAILABILITY:
            return ReplyDirective(ReplyDirectiveKind.AVAILABILITY, subject), False
        if subject in {
            DialogueSubject.SHIPPING_PAYER,
            DialogueSubject.DELIVERY_METHOD,
        } and act.kind is DialogueActKind.REQUEST_TERM and act.requested_value is None:
            return ReplyDirective(ReplyDirectiveKind.ASK_MISSING_TERM, subject), True
        if subject in {
            DialogueSubject.OFFER_PRICE,
            DialogueSubject.SHIPPING_PAYER,
            DialogueSubject.SHIPPING_COST,
            DialogueSubject.DELIVERY_METHOD,
        }:
            return ReplyDirective(ReplyDirectiveKind.FORMAL_OFFER_TERM, subject), False
        if subject in {
            DialogueSubject.DISPATCH_DEADLINE,
            DialogueSubject.RESERVATION,
        }:
            return ReplyDirective(ReplyDirectiveKind.SELLER_CONFIRMATION, subject), False
        return ReplyDirective(ReplyDirectiveKind.CANNOT_HANDLE, subject), True

    def _render_plan(
        self,
        *,
        plan: ReplyPlan,
        product: dict[str, object],
    ) -> str:
        clauses = [
            self._render_directive(directive, product=product)
            for directive in plan.directives
        ]
        return "；".join(dict.fromkeys(clauses)) + "。"

    def _render_directive(
        self,
        directive: ReplyDirective,
        *,
        product: dict[str, object],
    ) -> str:
        title = self._text(product.get("title"), fallback="这件商品")
        description = self._text(
            product.get("description"),
            fallback="页面暂未提供更多商品说明。",
        )
        listed_price = self._listed_price(product)

        if directive.kind is ReplyDirectiveKind.PRODUCT_DETAILS:
            return f"{title}：{description}页面公开标价为 {listed_price} 元"
        if directive.kind is ReplyDirectiveKind.LISTED_PRICE:
            return f"当前公开标价是 {listed_price} 元"
        if directive.kind is ReplyDirectiveKind.PRIVATE_PRICE:
            return (
                "卖家的最低接受价格不能直接公开，"
                f"当前公开标价是 {listed_price} 元，"
                "你可以提交一份正式报价，我会按规则帮你评估"
            )
        if directive.kind is ReplyDirectiveKind.AVAILABILITY:
            if product.get("status") == "AVAILABLE":
                return "商品目前仍在上架，可以继续了解或提交正式报价"
            return "商品当前已经不在上架状态，暂时不能继续协商"
        if directive.kind is ReplyDirectiveKind.FORMAL_OFFER_TERM:
            if directive.subject is DialogueSubject.OFFER_PRICE:
                return "价格条件需要通过正式报价提交，系统会按规则评估"
            if directive.subject is DialogueSubject.DELIVERY_METHOD:
                return "快递或面交可以作为正式报价中的交易条件提交"
            return "运费承担方式可以作为正式报价条件提交；选择快递时请说明由谁承担运费"
        if directive.kind is ReplyDirectiveKind.SELLER_CONFIRMATION:
            if directive.subject is DialogueSubject.DISPATCH_DEADLINE:
                return "具体发货时间需要卖家确认，我目前不能替卖家作出保证"
            if directive.subject is DialogueSubject.RESERVATION:
                return "是否保留商品需要卖家确认，我目前不能替卖家作出保证"
            return "这项履约承诺需要卖家确认，我目前不能替卖家作出保证"
        if directive.kind is ReplyDirectiveKind.ASK_MISSING_TERM:
            if directive.subject is DialogueSubject.SHIPPING_PAYER:
                return "请说明你希望运费由买家还是卖家承担"
            return "请说明你希望快递还是面交"
        if directive.kind is ReplyDirectiveKind.GENERAL:
            return "你可以继续询问商品信息，或通过正式报价讨论价格和配送条件"
        return "我还不能确定你想了解商品信息、价格条件还是配送安排，请明确其中一项"

    @classmethod
    def _candidate_is_safe(
        cls,
        candidate_reply: str,
        *,
        product: dict[str, object],
    ) -> bool:
        candidate = candidate_reply.strip()
        if not candidate or len(candidate) > 800:
            return False
        normalized = unicodedata.normalize("NFKC", candidate).casefold()
        compact = cls._compact_for_safety(normalized)
        if any(
            unicodedata.normalize("NFKC", phrase).casefold() in normalized
            or cls._compact_for_safety(phrase) in compact
            for phrase in cls._forbidden_phrases
        ):
            return False
        if cls._chinese_money_pattern.search(normalized) or cls._chinese_money_pattern.search(
            compact
        ):
            return False

        allowed_numbers = cls._public_numbers(product)
        candidate_numbers = {
            cls._normalized_number(match.group())
            for match in cls._number_pattern.finditer(candidate)
        }
        if None in candidate_numbers or not candidate_numbers <= allowed_numbers:
            return False

        listed_price = cls._normalized_number(str(product.get("listed_price", "")))
        for match in cls._arabic_money_pattern.finditer(candidate):
            amount = cls._normalized_number(match.group("prefix") or match.group("suffix"))
            if amount is None or amount != listed_price:
                return False
        return True

    @staticmethod
    def _compact_for_safety(value: str) -> str:
        """折叠空白和标点，避免用全角字符或拆词绕过敏感承诺检查。"""

        normalized = unicodedata.normalize("NFKC", value).casefold()
        return "".join(character for character in normalized if character.isalnum())

    @classmethod
    def _public_numbers(cls, product: dict[str, object]) -> set[Decimal]:
        public_text = " ".join(
            (
                cls._text(product.get("title"), fallback=""),
                cls._text(product.get("description"), fallback=""),
                str(product.get("listed_price", "")),
            )
        )
        return {
            number
            for match in cls._number_pattern.finditer(public_text)
            if (number := cls._normalized_number(match.group())) is not None
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
