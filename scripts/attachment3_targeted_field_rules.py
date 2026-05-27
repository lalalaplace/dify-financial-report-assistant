from typing import Dict, List, Optional, Tuple


TARGETED_FIELD_CODES = {
    "balance_sheet.equity_total_equity",
    "balance_sheet.liability_short_term_loans",
    "cash_flow.financing_cf_cash_from_borrowing",
    "cash_flow.financing_cf_cash_for_debt_repayment",
    "cash_flow.operating_cf_cash_from_sales",
    "income.other_income",
    "income.operating_expense_cost_of_sales",
}


LONGTAIL_CLEANUP_FIELD_CODES = {
    "balance_sheet.liability_short_term_loans",
    "cash_flow.financing_cf_cash_from_borrowing",
    "cash_flow.financing_cf_cash_for_debt_repayment",
    "income.other_income",
    "cash_flow.operating_cf_cash_from_sales",
}


TARGETED_EMPTY_VALUE_FIELD_CODES = {
    "balance_sheet.liability_short_term_loans",
    "cash_flow.financing_cf_cash_from_borrowing",
    "cash_flow.financing_cf_cash_for_debt_repayment",
    "income.other_income",
}


RAW_LINE_VALUE_PRIORITY_FIELD_CODES = {
    "balance_sheet.liability_short_term_loans",
    "cash_flow.financing_cf_cash_from_borrowing",
    "cash_flow.financing_cf_cash_for_debt_repayment",
    "income.other_income",
}


FIELD_ROW_MATCH_MIN_SCORE = {
    "balance_sheet.liability_short_term_loans": 52.0,
    "balance_sheet.equity_total_equity": 50.0,
    "income.other_income": 50.0,
    "income.operating_expense_cost_of_sales": 52.0,
    "cash_flow.operating_cf_cash_from_sales": 60.0,
    "cash_flow.financing_cf_cash_from_borrowing": 60.0,
    "cash_flow.financing_cf_cash_for_debt_repayment": 60.0,
}


TARGETED_SEMANTIC_BLACKLISTS: Dict[str, List[str]] = {
    "balance_sheet.equity_total_equity": [
        "少数股东权益",
        "归属于母公司所有者权益",
        "归属于母公司股东权益",
        "所有者权益（或股东权益））",
    ],
    "balance_sheet.liability_short_term_loans": [
        "长期借款",
        "一年内到期的非流动负债",
        "向中央银行借款",
        "拆入资金",
    ],
    "cash_flow.financing_cf_cash_from_borrowing": [
        "吸收投资收到的现金",
        "收到其他与筹资活动有关的现金",
        "筹资活动现金流入小计",
    ],
    "cash_flow.financing_cf_cash_for_debt_repayment": [
        "分配股利、利润或偿付利息支付的现金",
        "支付其他与筹资活动有关的现金",
        "筹资活动现金流出小计",
    ],
    "cash_flow.operating_cf_cash_from_sales": [
        "收到的税费返还",
        "收到其他与经营活动有关的现金",
        "经营活动现金流入小计",
    ],
    "income.other_income": [
        "其他综合收益",
        "投资收益",
        "营业外收入",
        "公允价值变动收益",
    ],
    "income.operating_expense_cost_of_sales": [
        "营业总成本",
        "营业总支出",
        "分行业",
        "分产品",
        "分地区",
        "毛利率",
    ],
}


STRICT_CANDIDATE_FILL_RULE_OVERRIDES = {
    "balance_sheet.liability_short_term_loans": {
        "allow_exact": ["短期借款", "短期借款余额", "流动负债短期借款"],
        "deny_contains": ["长期借款", "一年内到期的非流动负债", "向中央银行借款", "拆入资金"],
    },
    "balance_sheet.equity_total_equity": {
        "allow_exact": ["所有者权益合计", "股东权益合计", "所有者权益总计", "股东权益总计", "所有者权益（或股东权益）合计"],
        "deny_contains": ["归属于母公司", "少数股东权益", "所有者权益（或股东权益））"],
    },
    "cash_flow.financing_cf_cash_from_borrowing": {
        "allow_exact": [
            "取得借款收到的现金",
            "取得借款收到现金",
            "借款收到的现金",
            "取得借款所收到的现金",
            "融资性现金流-取得借款收到的现金",
            "融资活动产生的现金流量-取得借款收到的现金",
        ],
        "deny_contains": ["吸收投资", "收到其他与筹资活动有关的现金", "筹资活动现金流入小计"],
    },
    "cash_flow.financing_cf_cash_for_debt_repayment": {
        "allow_exact": [
            "偿还债务支付的现金",
            "偿还债务支付现金",
            "偿还债务所支付的现金",
            "融资性现金流-偿还债务支付的现金",
            "融资活动产生的现金流量-偿还债务支付的现金",
        ],
        "deny_contains": ["分配股利", "支付其他与筹资活动有关的现金", "筹资活动现金流出小计"],
    },
    "cash_flow.operating_cf_cash_from_sales": {
        "allow_exact": ["销售商品、提供劳务收到的现金", "销售商品提供劳务收到的现金", "销售商品收到的现金"],
        "deny_contains": ["收到的税费返还", "收到其他与经营活动有关的现金", "经营活动现金流入小计"],
    },
    "income.other_income": {
        "allow_exact": ["其他收益", "加：其他收益"],
        "deny_contains": ["其他综合收益", "投资收益", "营业外收入", "公允价值变动收益"],
    },
    "income.operating_expense_cost_of_sales": {
        "allow_exact": ["营业成本"],
        "deny_contains": ["营业总成本", "营业总支出", "分行业", "分产品", "分地区", "毛利率"],
    },
}


TARGETED_PRECISE_ALIASES: Dict[str, List[str]] = {
    "balance_sheet.liability_short_term_loans": [
        "短期借款",
        "短期借款余额",
        "其中：短期借款",
        "负债-短期借款",
        "流动负债短期借款",
    ],
    "cash_flow.financing_cf_cash_from_borrowing": [
        "取得借款收到的现金",
        "取得借款收到现金",
        "借款收到的现金",
        "取得借款所收到的现金",
    ],
    "cash_flow.financing_cf_cash_for_debt_repayment": [
        "偿还债务支付的现金",
        "偿还债务支付现金",
        "偿还债务所支付的现金",
    ],
    "cash_flow.operating_cf_cash_from_sales": [
        "销售商品、提供劳务收到的现金",
        "销售商品提供劳务收到的现金",
        "销售商品收到的现金",
    ],
    "income.other_income": [
        "其他收益",
        "加：其他收益",
    ],
}


TARGETED_FRAGMENT_BLACKLISTS: Dict[str, List[str]] = {
    "balance_sheet.liability_short_term_loans": [
        "长期借款",
        "一年内到期的非流动负债",
        "预付款项",
        "应收账款",
        "其他应收款",
    ],
    "cash_flow.financing_cf_cash_from_borrowing": [
        "收到其他与筹资活动有关的现金",
        "筹资活动现金流入小计",
        "吸收投资收到的现金",
        "筹资活动产生的现金流量净额变动原因说明",
    ],
    "cash_flow.financing_cf_cash_for_debt_repayment": [
        "支付其他与筹资活动有关的现金",
        "筹资活动现金流出小计",
        "分配股利、利润或偿付利息支付的现金",
        "筹资活动产生的现金流量净额变动原因说明",
    ],
    "cash_flow.operating_cf_cash_from_sales": [
        "收到其他与经营活动有关的现金",
        "收到的税费返还",
        "经营活动现金流入小计",
        "销售费用",
        "销售模式营业收入",
    ],
}


TARGETED_PRECISE_ALIAS_OVERRIDES: Dict[str, List[str]] = {
    "cash_flow.financing_cf_cash_from_borrowing": [
        "融资性现金流-取得借款收到的现金",
        "融资性现金流-取得借款收到现金",
        "融资活动产生的现金流量-取得借款收到的现金",
    ],
    "cash_flow.financing_cf_cash_for_debt_repayment": [
        "融资性现金流-偿还债务支付的现金",
        "融资性现金流-偿还债务支付现金",
        "融资活动产生的现金流量-偿还债务支付的现金",
    ],
}


def merge_strict_candidate_fill_rules(base_rules: Dict) -> Dict:
    merged_rules = dict(base_rules)
    for field_code, rule in STRICT_CANDIDATE_FILL_RULE_OVERRIDES.items():
        merged_rules[field_code] = rule
    return merged_rules


def get_targeted_precise_aliases(field_code: str) -> List[str]:
    merged_aliases = list(TARGETED_PRECISE_ALIASES.get(field_code) or [])
    merged_aliases.extend(TARGETED_PRECISE_ALIAS_OVERRIDES.get(field_code) or [])
    return list(dict.fromkeys(alias for alias in merged_aliases if alias))


def get_targeted_fragment_blacklist(field_code: str) -> List[str]:
    return list(TARGETED_FRAGMENT_BLACKLISTS.get(field_code) or [])


def should_prioritize_raw_line_numeric(
    field_code: str,
    cell_value_text: str,
    cell_role: str,
    cell_resolution: str,
    primary_role: str,
    parse_decimal_fn,
    is_empty_cell_value_fn,
    normalize_text_fn,
) -> bool:
    if field_code not in RAW_LINE_VALUE_PRIORITY_FIELD_CODES:
        return False
    if not cell_value_text:
        return True
    if is_empty_cell_value_fn(cell_value_text):
        return True
    if parse_decimal_fn(cell_value_text) is None:
        return True
    normalized_role = normalize_text_fn(cell_role)
    if normalized_role and normalized_role != normalize_text_fn(primary_role):
        return True
    return cell_resolution in {
        "duplicate_header_single_value_fallback",
        "same_row_numeric_adjacent_fallback",
    }


def find_targeted_blacklist_hit(
    field_code: str,
    texts: List[str],
    compact_row_label_fn,
) -> Tuple[bool, str]:
    blacklist = TARGETED_SEMANTIC_BLACKLISTS.get(field_code) or []
    haystacks = [compact_row_label_fn(text) for text in texts if compact_row_label_fn(text)]
    for blocked_text in blacklist:
        blocked_compact = compact_row_label_fn(blocked_text)
        if not blocked_compact:
            continue
        for haystack in haystacks:
            if blocked_compact in haystack:
                return True, blocked_text
    return False, ""


def should_reject_targeted_row(
    field_code: str,
    texts: List[str],
    compact_row_label_fn,
) -> Tuple[bool, str]:
    blocked, blocked_text = find_targeted_blacklist_hit(
        field_code=field_code,
        texts=texts,
        compact_row_label_fn=compact_row_label_fn,
    )
    if blocked:
        return True, blocked_text
    return False, ""


def build_targeted_semantic_rule_overrides(base_rules: Dict) -> Dict:
    merged_rules = dict(base_rules)
    for field_code, blacklist in TARGETED_SEMANTIC_BLACKLISTS.items():
        merged_rules[field_code] = {"deny": list(blacklist)}
    return merged_rules


def get_targeted_field_min_score(field_code: str) -> Optional[float]:
    return FIELD_ROW_MATCH_MIN_SCORE.get(field_code)


# 追加一组明确的中文规则，覆盖历史文件中乱码别名导致的重点字段兜底失效问题。
TARGETED_SEMANTIC_BLACKLISTS.update(
    {
        "balance_sheet.liability_short_term_loans": [
            "长期借款",
            "一年内到期的非流动负债",
            "向中央银行借款",
            "拆入资金",
            "其他流动负债",
            "流动负债合计",
        ],
        "cash_flow.financing_cf_cash_from_borrowing": [
            "吸收投资收到的现金",
            "收到其他与筹资活动有关的现金",
            "筹资活动现金流入小计",
            "筹资活动产生的现金流量净额",
        ],
        "cash_flow.financing_cf_cash_for_debt_repayment": [
            "分配股利、利润或偿付利息支付的现金",
            "支付其他与筹资活动有关的现金",
            "筹资活动现金流出小计",
            "筹资活动产生的现金流量净额",
        ],
        "cash_flow.operating_cf_cash_from_sales": [
            "收到的税费返还",
            "收到其他与经营活动有关的现金",
            "经营活动现金流入小计",
            "销售费用",
            "销售模式营业收入",
        ],
        "income.other_income": [
            "其他综合收益",
            "他综合收益",
            "综合收益",
            "投资收益",
            "营业外收入",
            "公允价值变动收益",
        ],
        "income.operating_expense_cost_of_sales": [
            "营业总成本",
            "营业总支出",
            "毛利率",
        ],
    }
)

STRICT_CANDIDATE_FILL_RULE_OVERRIDES.update(
    {
        "balance_sheet.liability_short_term_loans": {
            "allow_exact": ["短期借款", "短期借款余额", "其中：短期借款", "流动负债短期借款"],
            "deny_contains": ["长期借款", "一年内到期的非流动负债", "向中央银行借款", "拆入资金", "流动负债合计"],
        },
        "cash_flow.financing_cf_cash_from_borrowing": {
            "allow_exact": [
                "取得借款收到的现金",
                "取得借款收到现金",
                "借款收到的现金",
                "取得借款所收到的现金",
                "筹资活动产生的现金流量取得借款收到的现金",
            ],
            "deny_contains": ["吸收投资", "收到其他与筹资活动有关的现金", "筹资活动现金流入小计", "筹资活动产生的现金流量净额"],
        },
        "cash_flow.financing_cf_cash_for_debt_repayment": {
            "allow_exact": [
                "偿还债务支付的现金",
                "偿还债务支付现金",
                "偿还债务所支付的现金",
                "筹资活动产生的现金流量偿还债务支付的现金",
            ],
            "deny_contains": ["分配股利", "支付其他与筹资活动有关的现金", "筹资活动现金流出小计", "筹资活动产生的现金流量净额"],
        },
        "cash_flow.operating_cf_cash_from_sales": {
            "allow_exact": [
                "销售商品、提供劳务收到的现金",
                "销售商品提供劳务收到的现金",
                "销售商品收到的现金",
            ],
            "deny_contains": ["收到的税费返还", "收到其他与经营活动有关的现金", "经营活动现金流入小计"],
        },
        "income.other_income": {
            "allow_exact": ["其他收益", "加：其他收益"],
            "deny_contains": ["其他综合收益", "他综合收益", "综合收益", "投资收益", "营业外收入", "公允价值变动收益"],
        },
    }
)

TARGETED_PRECISE_ALIASES.update(
    {
        "balance_sheet.liability_short_term_loans": ["短期借款", "短期借款余额", "其中：短期借款", "流动负债短期借款"],
        "cash_flow.financing_cf_cash_from_borrowing": [
            "取得借款收到的现金",
            "取得借款收到现金",
            "借款收到的现金",
            "取得借款所收到的现金",
        ],
        "cash_flow.financing_cf_cash_for_debt_repayment": [
            "偿还债务支付的现金",
            "偿还债务支付现金",
            "偿还债务所支付的现金",
        ],
        "cash_flow.operating_cf_cash_from_sales": [
            "销售商品、提供劳务收到的现金",
            "销售商品提供劳务收到的现金",
            "销售商品收到的现金",
        ],
        "income.other_income": ["其他收益", "加：其他收益"],
    }
)

TARGETED_FRAGMENT_BLACKLISTS.update(
    {
        "balance_sheet.liability_short_term_loans": ["长期借款", "一年内到期的非流动负债", "其他流动负债", "流动负债合计"],
        "cash_flow.financing_cf_cash_from_borrowing": ["收到其他与筹资活动有关的现金", "筹资活动现金流入小计", "吸收投资收到的现金"],
        "cash_flow.financing_cf_cash_for_debt_repayment": ["支付其他与筹资活动有关的现金", "筹资活动现金流出小计", "分配股利"],
        "cash_flow.operating_cf_cash_from_sales": ["收到其他与经营活动有关的现金", "收到的税费返还", "经营活动现金流入小计"],
    }
)

# 明确区分利润表中两个容易互串的减值损失字段。
TARGETED_SEMANTIC_BLACKLISTS.update(
    {
        "income.asset_impairment_loss": [
            "信用减值损失",
            "加：信用减值损失",
            "信用减值损失（损失以",
        ],
        "income.credit_impairment_loss": [
            "资产减值损失",
            "加：资产减值损失",
            "资产减值损失（损失以",
        ],
    }
)

STRICT_CANDIDATE_FILL_RULE_OVERRIDES.update(
    {
        "income.asset_impairment_loss": {
            "allow_exact": [
                "资产减值损失",
                "加：资产减值损失",
                "资产减值损失（损失以“-”号填列）",
                "资产减值损失（损失以",
            ],
            "deny_contains": ["信用减值损失"],
        },
        "income.credit_impairment_loss": {
            "allow_exact": [
                "信用减值损失",
                "加：信用减值损失",
                "信用减值损失（损失以“-”号填列）",
                "信用减值损失（损失以",
            ],
            "deny_contains": ["资产减值损失"],
        },
    }
)

TARGETED_PRECISE_ALIASES.update(
    {
        "income.asset_impairment_loss": [
            "资产减值损失",
            "加：资产减值损失",
            "资产减值损失（损失以“-”号填列）",
            "资产减值损失（损失以",
        ],
        "income.credit_impairment_loss": [
            "信用减值损失",
            "加：信用减值损失",
            "信用减值损失（损失以“-”号填列）",
            "信用减值损失（损失以",
        ],
    }
)

TARGETED_FRAGMENT_BLACKLISTS.update(
    {
        "income.asset_impairment_loss": ["信用减值损失"],
        "income.credit_impairment_loss": ["资产减值损失"],
    }
)

# 资产负债表字段不允许用“流动负债/资产总计”附近的拼接文本兜底取值。
STRICT_CANDIDATE_FILL_RULE_OVERRIDES["balance_sheet.liability_short_term_loans"] = {
    "allow_exact": ["短期借款", "短期借款余额", "其中：短期借款"],
    "deny_contains": [
        "资产总计",
        "流动负债",
        "非流动负债",
        "长期借款",
        "一年内到期的非流动负债",
        "向中央银行借款",
        "拆入资金",
        "流动负债合计",
    ],
}

TARGETED_PRECISE_ALIASES["balance_sheet.liability_short_term_loans"] = [
    "短期借款",
    "短期借款余额",
    "其中：短期借款",
]

TARGETED_SEMANTIC_BLACKLISTS["balance_sheet.liability_short_term_loans"] = [
    "资产总计",
    "流动负债",
    "非流动负债",
    "长期借款",
    "一年内到期的非流动负债",
    "向中央银行借款",
    "拆入资金",
    "流动负债合计",
]

TARGETED_FRAGMENT_BLACKLISTS["balance_sheet.liability_short_term_loans"] = [
    "资产总计",
    "流动负债",
    "非流动负债",
    "长期借款",
    "一年内到期的非流动负债",
    "其他流动负债",
    "流动负债合计",
]

TARGETED_SEMANTIC_BLACKLISTS["cash_flow.financing_cf_cash_for_debt_repayment"] = list(
    dict.fromkeys(
        (TARGETED_SEMANTIC_BLACKLISTS.get("cash_flow.financing_cf_cash_for_debt_repayment") or [])
        + [
            "吸收投资收到的现金",
            "收到其他与筹资活动有关的现金",
            "筹资活动现金流入小计",
        ]
    )
)

STRICT_CANDIDATE_FILL_RULE_OVERRIDES["cash_flow.financing_cf_cash_for_debt_repayment"] = {
    "allow_exact": [
        "偿还债务支付的现金",
        "偿还债务支付现金",
        "偿还债务所支付的现金",
        "筹资活动产生的现金流量偿还债务支付的现金",
    ],
    "deny_contains": [
        "吸收投资",
        "收到其他与筹资活动有关的现金",
        "筹资活动现金流入小计",
        "分配股利",
        "支付其他与筹资活动有关的现金",
        "筹资活动现金流出小计",
        "筹资活动产生的现金流量净额",
    ],
}

TARGETED_FRAGMENT_BLACKLISTS["cash_flow.financing_cf_cash_for_debt_repayment"] = list(
    dict.fromkeys(
        (TARGETED_FRAGMENT_BLACKLISTS.get("cash_flow.financing_cf_cash_for_debt_repayment") or [])
        + [
            "吸收投资收到的现金",
            "收到其他与筹资活动有关的现金",
            "筹资活动现金流入小计",
        ]
    )
)
