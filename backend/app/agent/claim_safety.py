import re

_UNSAFE_CLAIMS = {
    "PRIVATE_PRICE_LEAK": re.compile(
        r"(?:(?:卖家)?(?:底价|底價|最低(?:接受)?价|最低(?:接受)?價|floor price)"
        r"\s*(?:是|为|為|=|:|：|is)\s*[¥￥]?\s*\d)"
        r"|(?:[¥￥]?\s*\d+(?:\.\d+)?\s*(?:元|块|rmb)?\s*"
        r"(?:就是|是|为|為)?\s*(?:卖家)?(?:底价|底價|最低(?:接受)?价|floor price))",
        re.IGNORECASE,
    ),
    "SYSTEM_PROMPT_LEAK": re.compile(
        r"(?:系统提示词|系統提示詞|system prompt)\s*(?:是|为|為|如下|内容|內容|:|：)",
        re.IGNORECASE,
    ),
    "FALSE_APPROVAL": re.compile(
        r"(?:(?:卖家|賣家)?(?:已经|已經|已)(?:审批通过|審批通過|同意|批准))"
        r"|(?:(?:审批通过|審批通過|卖家同意|賣家同意)(?:了|啦|。|$))"
        r"|(?:seller (?:has )?approved|approval (?:has been )?granted)",
        re.IGNORECASE,
    ),
    "UNAUTHORIZED_FREE_SHIPPING": re.compile(
        r"(?:(?:我|我们|我們|卖家|賣家)(?:可以|同意|承诺|承諾|确认|確認|"
        r"会|會|将|將|保证|保證)(?:给你|給你)?(?:包邮|包郵|免邮|免郵))"
        r"|(?:(?:可以|同意|会|會|将|將|保证|保證)(?:给你|給你)?"
        r"(?:包邮|包郵|免邮|免郵))"
        r"|(?:(?:包邮|包郵|免邮|免郵)(?:没问题|沒問題|可以|已确认|已確認))"
        r"|(?:free shipping (?:is )?(?:confirmed|included))",
        re.IGNORECASE,
    ),
    "UNAUTHORIZED_DISPATCH": re.compile(
        r"(?:(?:保证|保證|承诺|承諾|确认|確認|会|會|将|將|一定)"
        r".{0,6}(?:今天发货|今天發貨|当日发货|當日發貨))"
        r"|(?:(?:will|guarantee to) (?:ship|dispatch) today)",
        re.IGNORECASE,
    ),
    "UNAUTHORIZED_RESERVATION": re.compile(
        r"(?:(?:已经|已經|已|为你|為你|给你|給你)(?:预订|預訂|预留|預留|保留))"
        r"|(?:reserved for you|hold it for you)",
        re.IGNORECASE,
    ),
    "FALSE_TRANSACTION": re.compile(
        r"(?:(?:已经|已經|已|确认|確認|正式)(?:成交|达成交易|達成交易))"
        r"|(?:deal is done|deal (?:has been )?confirmed)",
        re.IGNORECASE,
    ),
}
_NEGATION_NEAR_CLAIM = re.compile(
    r"(?:不|未|没有|沒有|尚未|无法|無法|不能|不会|不會|不可|拒绝|拒絕|"
    r"not|cannot|can't|won't|never).{0,10}$",
    re.IGNORECASE,
)


def find_unsafe_claims(reply: str) -> set[str]:
    """识别肯定式越权声明，同时排除紧邻声明的拒绝或否定话术。"""

    violations: set[str] = set()
    for code, pattern in _UNSAFE_CLAIMS.items():
        for match in pattern.finditer(reply):
            prefix = reply[max(0, match.start() - 16) : match.start()]
            if _NEGATION_NEAR_CLAIM.search(prefix):
                continue
            violations.add(code)
            break
    return violations
