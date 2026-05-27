import re
from typing import Dict, Iterable, List

from statement_table_schema import FINANCIAL_ROW_KEYWORDS, compact_text, normalize_text


FORMAL_HEADER_KEYWORDS = {
    "期末余额",
    "期初余额",
    "本期发生额",
    "上期发生额",
    "本期金额",
    "上期金额",
    "本年累计",
    "上年同期",
    "202",
    "项目",
}

EXPLANATORY_MARKERS = {
    "变动",
    "原因",
    "所致",
    "较上年",
    "较期初",
    "比期初",
    "同比",
    "增幅",
    "减幅",
    "下降",
    "增长",
    "主要是",
    "主要原因",
}

NON_STATEMENT_MARKERS = {
    "股东信息",
    "普通股股东总数",
    "前10名股东",
    "前十名股东",
    "管理层讨论",
    "主要会计数据",
    "财务指标",
    "相关科目变动分析",
}

TITLE_NOISE_MARKERS = {
    "变动",
    "原因",
    "说明",
    "分析",
    "部分",
    "项目大幅",
    "相关科目",
}

STATEMENT_CORE_HINTS = {
    "balance_sheet": [
        "流动资产",
        "非流动资产",
        "资产总计",
        "流动负债",
        "非流动负债",
        "负债合计",
        "所有者权益",
        "股东权益",
    ],
    "income": [
        "营业收入",
        "营业成本",
        "营业利润",
        "利润总额",
        "净利润",
        "综合收益总额",
    ],
    "cash_flow": [
        "经营活动产生的现金流量",
        "销售商品",
        "购买商品",
        "投资活动产生的现金流量",
        "筹资活动产生的现金流量",
        "现金及现金等价物净增加额",
    ],
}


def _number_count(text: str) -> int:
    return len(re.findall(r"[-－—–]?\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?%?", normalize_text(text)))


def _count_hits(lines: Iterable[str], keywords: Iterable[str]) -> int:
    compact_lines = [compact_text(line) for line in lines if compact_text(line)]
    compact_keywords = [compact_text(keyword) for keyword in keywords if compact_text(keyword)]
    return sum(1 for keyword in compact_keywords if any(keyword in line for line in compact_lines))


def formal_statement_page_score(page: Dict, statement_type: str, title_text: str = "") -> float:
    """给候选页计算正式报表页分数；说明性段落和股东信息页会被明显降权。"""
    lines: List[str] = [normalize_text(line) for line in page.get("lines", []) if normalize_text(line)]
    compact_title = compact_text(title_text)
    start_index = 0
    if compact_title:
        for index, line in enumerate(lines):
            if compact_title and compact_title in compact_text(line):
                start_index = index
                break
    top_lines = lines[start_index : start_index + 50]
    compact_page = compact_text("\n".join(top_lines))

    header_hits = _count_hits(top_lines[:16], FORMAL_HEADER_KEYWORDS)
    core_hints = STATEMENT_CORE_HINTS.get(statement_type, []) + FINANCIAL_ROW_KEYWORDS.get(statement_type, [])
    core_hits = _count_hits(top_lines, core_hints)
    numeric_table_lines = sum(
        1
        for line in top_lines
        if _number_count(line) >= 2 and len(compact_text(line)) <= 42
    )
    explanatory_lines = sum(
        1
        for line in top_lines
        if any(compact_text(marker) in compact_text(line) for marker in EXPLANATORY_MARKERS)
    )
    non_statement_hits = sum(1 for marker in NON_STATEMENT_MARKERS if compact_text(marker) in compact_page)
    title_noise_hits = sum(1 for marker in TITLE_NOISE_MARKERS if compact_text(marker) in compact_title)

    score = 0.0
    score += min(header_hits, 4) * 2.5
    score += min(core_hits, 8) * 2.2
    score += min(numeric_table_lines, 12) * 0.9
    score -= min(explanatory_lines, 12) * 1.2
    score -= min(non_statement_hits, 4) * 6.0
    score -= min(title_noise_hits, 3) * 7.0
    return round(score, 4)


def is_formal_statement_candidate(page: Dict, statement_type: str, title_text: str = "") -> bool:
    """判断候选页是否值得进入正式报表定位结果。"""
    score = formal_statement_page_score(page, statement_type, title_text)
    if score >= 6.0:
        return True
    compact_title = compact_text(title_text)
    compact_page = compact_text(page.get("text") or "")
    has_exact_title = any(
        compact_text(token) in compact_title
        for token in ("合并资产负债表", "资产负债表", "合并利润表", "利润表", "合并现金流量表", "现金流量表")
    )
    has_non_statement_marker = any(compact_text(marker) in compact_page for marker in NON_STATEMENT_MARKERS)
    return has_exact_title and score >= 2.0 and not has_non_statement_marker
