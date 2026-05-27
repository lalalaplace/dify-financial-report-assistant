import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


FORMAT_VERSION = "normalized_table_v1"

NUMBER_TOKEN_PATTERN = re.compile(r"[-－—–−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
DATE_HEADER_PATTERN = re.compile(r"20\d{2}\s*年(?:\d{1,2}\s*月(?:\d{1,2}\s*日)?)?")
NOTE_PREFIX_PATTERN = re.compile(r"^[（(]?\d+[）)]?$")

STATEMENT_TITLE_RULES: Dict[str, Dict[str, List[str]]] = {
    "balance_sheet": {
        "strong": ["合并资产负债表"],
        "weak": ["资产负债表"],
    },
    "income": {
        "strong": ["合并利润表"],
        "weak": ["利润表"],
    },
    "cash_flow": {
        "strong": ["合并现金流量表"],
        "weak": ["现金流量表"],
    },
}

FINANCIAL_ROW_KEYWORDS: Dict[str, List[str]] = {
    "balance_sheet": [
        "货币资金",
        "应收账款",
        "存货",
        "资产总计",
        "负债合计",
        "所有者权益合计",
    ],
    "income": [
        "营业收入",
        "营业成本",
        "税金及附加",
        "净利润",
        "营业利润",
        "利润总额",
    ],
    "cash_flow": [
        "经营活动产生的现金流量净额",
        "投资活动产生的现金流量净额",
        "筹资活动产生的现金流量净额",
        "现金及现金等价物净增加额",
    ],
}

HEADER_ROLE_ALIASES: Dict[str, Dict[str, List[str]]] = {
    "balance_sheet": {
        "item_name": ["项目"],
        "current_period": ["期末余额", "本期末", "本报告期末", "年末余额", "本期期末"],
        "previous_period": ["期初余额", "上年末", "年初余额", "上期期末"],
    },
    "income": {
        "item_name": ["项目"],
        "current_period": ["本期发生额", "本报告期", "本期金额", "本年累计", "本期"],
        "previous_period": ["上期发生额", "上年同期", "上期金额", "上年金额", "同期"],
    },
    "cash_flow": {
        "item_name": ["项目"],
        "current_period": ["本期发生额", "本报告期", "本期金额", "本年累计", "本期"],
        "previous_period": ["上期发生额", "上年同期", "上期金额", "上年金额", "同期"],
    },
}

ROW_NOISE_KEYWORDS = {
    "项目",
    "附注",
    "币种：人民币",
    "币种:人民币",
    "法定代表人",
    "主管会计工作负责人",
    "会计机构负责人",
}

ITEM_NAME_ALIASES = {
    "一、营业收入": "营业收入",
    "二、营业成本": "营业成本",
    "三、营业利润": "营业利润",
    "四、利润总额": "利润总额",
    "五、净利润": "净利润",
    "其中：货币资金": "货币资金",
    "其中:货币资金": "货币资金",
    "资产合计": "资产总计",
    "负债总计": "负债合计",
    "股东权益合计": "所有者权益合计",
}


@dataclass
class StatementCandidate:
    file_id: int
    statement_type: str
    start_page: Optional[int]
    end_page_guess: Optional[int]
    title_text: str = ""
    matched_keywords: List[str] = field(default_factory=list)
    header_text: str = ""
    is_consolidated: bool = False
    is_parent_only: bool = False
    locator_confidence: float = 0.0
    candidate_rank: int = 1
    locator_status: str = "not_found"
    locator_method: str = "rule_title_match_v2"
    source_text: str = ""
    extra_info: Dict = field(default_factory=dict)


@dataclass
class ColumnSchema:
    col_id: int
    raw_name: str
    role: str


@dataclass
class NormalizedRow:
    row_id: int
    raw_item_name: str
    normalized_item_name: str
    cells: Dict[str, str]
    source_page: Optional[int] = None
    raw_line_text: str = ""
    merge_confidence: float = 1.0
    merged_from_pages: List[int] = field(default_factory=list)
    extra_info: Dict = field(default_factory=dict)


@dataclass
class NormalizedTable:
    format_version: str
    file_id: int
    statement_type: str
    title: str
    pages: List[int]
    unit: str
    currency: str
    is_consolidated: bool
    column_schema: List[ColumnSchema]
    rows: List[NormalizedRow]
    parser_meta: Dict
    target_table: str
    report_year: Optional[int] = None
    report_period: Optional[str] = None
    stock_code: str = ""
    stock_abbr: str = ""
    company_id: Optional[int] = None
    title_text: str = ""
    header_text: str = ""
    locator_confidence: float = 0.0
    text: str = ""
    page_text_map: Dict[str, str] = field(default_factory=dict)


def normalize_text(text: Optional[str]) -> str:
    """标准化文本空白。"""
    if text is None:
        return ""
    result = str(text)
    result = result.replace("\u3000", " ")
    result = result.replace("\xa0", " ")
    result = re.sub(r"\r\n?", "\n", result)
    return result.strip()


def compact_text(text: Optional[str]) -> str:
    """压缩空白，便于匹配。"""
    return re.sub(r"\s+", "", normalize_text(text))


def normalize_item_name(text: Optional[str]) -> str:
    """归一化报表项目名。"""
    value = normalize_text(text)
    if not value:
        return ""

    value = re.sub(r"^[一二三四五六七八九十]+[、\.]", "", value)
    value = re.sub(r"^[（(]?[一二三四五六七八九十\d]+[）)]", "", value)
    value = re.sub(r"^(其中[:：])", "", value)
    value = re.sub(r"^(加[:：]|减[:：])", "", value)
    value = re.sub(r"^项目[:：]?", "", value)
    value = re.sub(r"\s+", "", value)
    value = value.replace("：", "").replace(":", "")
    value = value.replace("（", "(").replace("）", ")")
    value = value.strip("()[]【】")
    value = ITEM_NAME_ALIASES.get(value, value)
    return value


def detect_unit(lines_or_text: Iterable[str] | str) -> Tuple[str, str]:
    """识别单位与币种。"""
    if isinstance(lines_or_text, str):
        haystack = normalize_text(lines_or_text)
    else:
        haystack = "\n".join(normalize_text(item) for item in lines_or_text)

    compact = compact_text(haystack)
    currency = "人民币" if "人民币" in compact else ""

    if "单位：亿元" in compact or "单位:亿元" in compact:
        return "亿元", currency or "人民币"
    if "单位：万元" in compact or "单位:万元" in compact:
        return "万元", currency or "人民币"
    if "单位：千元" in compact or "单位:千元" in compact:
        return "千元", currency or "人民币"
    if "单位：元" in compact or "单位:元" in compact:
        return "元", currency or "人民币"
    return "", currency or "人民币"


def infer_column_roles(
    header_lines: Sequence[str],
    statement_type: str,
    header_cells: Optional[Sequence[str]] = None,
) -> List[ColumnSchema]:
    """根据表头推断列角色。"""
    candidates = [normalize_text(item) for item in (header_cells or []) if normalize_text(item)]
    joined_header = " ".join(normalize_text(line) for line in header_lines)

    if not candidates:
        candidates = _guess_header_cells_from_text(joined_header, statement_type)

    if not candidates:
        aliases = HEADER_ROLE_ALIASES.get(statement_type, {})
        candidates = [
            aliases.get("item_name", ["项目"])[0],
            aliases.get("current_period", ["本期"])[0],
            aliases.get("previous_period", ["上期"])[0],
        ]

    roles: List[ColumnSchema] = []
    for index, raw_name in enumerate(candidates):
        if index == 0:
            role = "item_name"
        else:
            role = _infer_single_column_role(raw_name, statement_type, index)
        roles.append(ColumnSchema(col_id=index, raw_name=raw_name, role=role))

    return roles


def score_statement_candidate(
    statement_type: str,
    title_text: str,
    page_text: str,
    header_text: str = "",
    is_consolidated: bool = False,
    is_parent_only: bool = False,
) -> Tuple[float, Dict]:
    """对候选报表页打分。"""
    page_compact = compact_text(page_text)
    title_compact = compact_text(title_text)
    rules = STATEMENT_TITLE_RULES.get(statement_type, {})
    matched_keywords: List[str] = []
    title_score = 0.0

    for keyword in rules.get("strong", []):
        if compact_text(keyword) in title_compact or compact_text(keyword) in page_compact:
            matched_keywords.append(keyword)
            title_score = max(title_score, 60.0)
    for keyword in rules.get("weak", []):
        if compact_text(keyword) in title_compact or compact_text(keyword) in page_compact:
            matched_keywords.append(keyword)
            title_score = max(title_score, 40.0)

    lines = [normalize_text(line) for line in normalize_text(page_text).splitlines() if normalize_text(line)]
    numeric_lines = sum(1 for line in lines[:50] if len(NUMBER_TOKEN_PATTERN.findall(line)) >= 2)
    table_density_score = min(numeric_lines, 8) * 4.0

    financial_keywords = FINANCIAL_ROW_KEYWORDS.get(statement_type, [])
    financial_row_score = sum(1 for keyword in financial_keywords if keyword in page_compact) * 5.0

    consolidated_bonus = 8.0 if is_consolidated else 0.0
    parent_only_penalty = 12.0 if is_parent_only else 0.0
    summary_penalty = 8.0 if "摘要" in page_compact else 0.0
    header_bonus = 6.0 if compact_text(header_text) else 0.0

    score = title_score + consolidated_bonus + table_density_score + financial_row_score + header_bonus
    score -= parent_only_penalty + summary_penalty
    confidence = max(0.0, min(score / 100.0, 1.0))

    return score, {
        "matched_keywords": sorted(set(matched_keywords)),
        "title_score": title_score,
        "table_density_score": table_density_score,
        "financial_row_score": financial_row_score,
        "consolidated_bonus": consolidated_bonus,
        "parent_only_penalty": parent_only_penalty,
        "summary_penalty": summary_penalty,
        "header_bonus": header_bonus,
        "confidence": confidence,
    }


def merge_multi_page_rows(rows: Sequence[NormalizedRow]) -> List[NormalizedRow]:
    """合并跨页重复行。"""
    merged: List[NormalizedRow] = []
    row_index_by_name: Dict[str, int] = {}

    for row in rows:
        key = normalize_item_name(row.normalized_item_name or row.raw_item_name)
        if not key:
            continue

        if key not in row_index_by_name:
            row.normalized_item_name = key
            merged.append(row)
            row_index_by_name[key] = len(merged) - 1
            continue

        current = merged[row_index_by_name[key]]
        for cell_key, cell_value in row.cells.items():
            if cell_key not in current.cells or not normalize_text(current.cells.get(cell_key)):
                current.cells[cell_key] = cell_value

        if not current.raw_line_text and row.raw_line_text:
            current.raw_line_text = row.raw_line_text

        current_pages = set(current.extra_info.get("source_pages", []))
        if current.source_page is not None:
            current_pages.add(current.source_page)
        if row.source_page is not None:
            current_pages.add(row.source_page)
        current.extra_info["source_pages"] = sorted(current_pages)

    for index, row in enumerate(merged):
        row.row_id = index

    return merged


def load_normalized_table_json(path: Path) -> NormalizedTable:
    """读取标准化表结构 JSON，并兼容旧格式。"""
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if payload.get("format_version") == FORMAT_VERSION and "rows" in payload:
        column_schema = [
            ColumnSchema(
                col_id=int(item.get("col_id", 0)),
                raw_name=normalize_text(item.get("raw_name")),
                role=normalize_text(item.get("role")),
            )
            for item in payload.get("column_schema", [])
        ]
        rows = [
            NormalizedRow(
                row_id=int(item.get("row_id", 0)),
                raw_item_name=normalize_text(item.get("raw_item_name")),
                normalized_item_name=normalize_item_name(item.get("normalized_item_name") or item.get("raw_item_name")),
                cells={str(key): normalize_text(value) for key, value in (item.get("cells") or {}).items()},
                source_page=item.get("source_page"),
                raw_line_text=normalize_text(item.get("raw_line_text") or ""),
                merge_confidence=float(item.get("merge_confidence") or 1.0),
                merged_from_pages=list(item.get("merged_from_pages") or ([] if item.get("source_page") is None else [item.get("source_page")])),
                extra_info=item.get("extra_info") or {},
            )
            for item in payload.get("rows", [])
        ]
        return NormalizedTable(
            format_version=payload.get("format_version", FORMAT_VERSION),
            file_id=payload.get("file_id"),
            statement_type=payload.get("statement_type"),
            title=normalize_text(payload.get("title")),
            pages=list(payload.get("pages") or []),
            unit=normalize_text(payload.get("unit")),
            currency=normalize_text(payload.get("currency")),
            is_consolidated=bool(payload.get("is_consolidated")),
            column_schema=column_schema,
            rows=rows,
            parser_meta=payload.get("parser_meta") or {},
            target_table=normalize_text(payload.get("target_table") or payload.get("statement_type")),
            report_year=payload.get("report_year"),
            report_period=payload.get("report_period"),
            stock_code=normalize_text(payload.get("stock_code")),
            stock_abbr=normalize_text(payload.get("stock_abbr")),
            company_id=payload.get("company_id"),
            title_text=normalize_text(payload.get("title_text") or payload.get("title")),
            header_text=normalize_text(payload.get("header_text")),
            locator_confidence=float(payload.get("locator_confidence") or 0.0),
            text=normalize_text(payload.get("text")),
            page_text_map={str(key): normalize_text(value) for key, value in (payload.get("page_text_map") or {}).items()},
        )

    return _convert_legacy_payload(payload)


def dump_normalized_table_json(table: NormalizedTable, path: Path) -> None:
    """写出标准化表结构 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(table)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def rows_to_simple_records(rows: Sequence[NormalizedRow]) -> List[Dict]:
    """将行对象转为简化字典。"""
    return [
        {
            "row_id": row.row_id,
            "raw_item_name": row.raw_item_name,
            "normalized_item_name": row.normalized_item_name,
            "cells": row.cells,
            "source_page": row.source_page,
            "merge_confidence": row.merge_confidence,
            "merged_from_pages": row.merged_from_pages,
        }
        for row in rows
    ]


def _infer_single_column_role(raw_name: str, statement_type: str, index: int) -> str:
    compact_name = compact_text(raw_name)
    aliases = HEADER_ROLE_ALIASES.get(statement_type, {})

    for alias in aliases.get("current_period", []):
        alias_compact = compact_text(alias)
        if alias_compact and (alias_compact in compact_name or compact_name in alias_compact):
            return "current_period"

    for alias in aliases.get("previous_period", []):
        alias_compact = compact_text(alias)
        if alias_compact and (alias_compact in compact_name or compact_name in alias_compact):
            return "previous_period"

    if DATE_HEADER_PATTERN.search(raw_name):
        return "current_period" if index == 1 else "previous_period"

    return "current_period" if index == 1 else "previous_period"


def _guess_header_cells_from_text(header_text: str, statement_type: str) -> List[str]:
    aliases = HEADER_ROLE_ALIASES.get(statement_type, {})
    current_label = aliases.get("current_period", ["本期"])[0]
    previous_label = aliases.get("previous_period", ["上期"])[0]

    date_headers = DATE_HEADER_PATTERN.findall(header_text)
    if len(date_headers) >= 2:
        return ["项目", date_headers[0], date_headers[1]]

    compact_header = compact_text(header_text)
    current_candidates = aliases.get("current_period", [])
    previous_candidates = aliases.get("previous_period", [])

    current_raw = current_label
    previous_raw = previous_label

    for alias in current_candidates:
        if compact_text(alias) in compact_header:
            current_raw = alias
            break
    for alias in previous_candidates:
        if compact_text(alias) in compact_header:
            previous_raw = alias
            break

    return ["项目", current_raw, previous_raw]


def _convert_legacy_payload(payload: Dict) -> NormalizedTable:
    """将旧格式 JSON 转换为兼容的标准化结构。"""
    statement_type = normalize_text(payload.get("statement_type"))
    pages_payload = payload.get("pages") or []
    page_numbers: List[int] = []
    page_text_map: Dict[str, str] = {}
    merged_text_parts: List[str] = []

    for item in pages_payload:
        page_no = item.get("page_no")
        page_text = normalize_text(item.get("text"))
        if page_no is not None:
            page_numbers.append(page_no)
            page_text_map[str(page_no)] = page_text
        if page_text:
            merged_text_parts.append(page_text)

    if not merged_text_parts and payload.get("text"):
        merged_text_parts.append(normalize_text(payload.get("text")))

    merged_text = "\n".join(part for part in merged_text_parts if part)
    unit, currency = detect_unit(merged_text)
    header_lines = []
    all_lines = [normalize_text(line) for line in merged_text.splitlines() if normalize_text(line)]
    for line in all_lines[:10]:
        if "项目" in line or DATE_HEADER_PATTERN.search(line):
            header_lines.append(line)

    column_schema = infer_column_roles(header_lines, statement_type)
    rows = _parse_legacy_rows(all_lines, column_schema)

    return NormalizedTable(
        format_version=FORMAT_VERSION,
        file_id=payload.get("file_id"),
        statement_type=statement_type,
        title=normalize_text(payload.get("title") or payload.get("title_text") or ""),
        pages=page_numbers,
        unit=unit,
        currency=currency,
        is_consolidated=bool(payload.get("is_consolidated")),
        column_schema=column_schema,
        rows=rows,
        parser_meta={"legacy_compat": True},
        target_table=normalize_text(payload.get("target_table") or statement_type),
        report_year=payload.get("report_year"),
        report_period=payload.get("report_period"),
        stock_code=normalize_text(payload.get("stock_code")),
        stock_abbr=normalize_text(payload.get("stock_abbr")),
        company_id=payload.get("company_id"),
        title_text=normalize_text(payload.get("title_text") or payload.get("title") or ""),
        header_text="\n".join(header_lines),
        locator_confidence=float(payload.get("locator_confidence") or 0.0),
        text=merged_text,
        page_text_map=page_text_map,
    )


def _parse_legacy_rows(lines: Sequence[str], column_schema: Sequence[ColumnSchema]) -> List[NormalizedRow]:
    rows: List[NormalizedRow] = []
    row_id = 0
    data_col_count = max(1, len([item for item in column_schema if item.role != "item_name"]))

    for line in lines:
        compact_line = compact_text(line)
        if not compact_line or compact_line in {compact_text(item) for item in ROW_NOISE_KEYWORDS}:
            continue
        tokens = NUMBER_TOKEN_PATTERN.findall(line)
        if not tokens:
            continue

        first_number = NUMBER_TOKEN_PATTERN.search(line)
        if first_number is None:
            continue
        raw_item_name = normalize_text(line[: first_number.start()])
        if not raw_item_name:
            continue

        values = tokens[-data_col_count:]
        cells = {str(index + 1): normalize_text(value) for index, value in enumerate(values)}
        rows.append(
            NormalizedRow(
                row_id=row_id,
                raw_item_name=raw_item_name,
                normalized_item_name=normalize_item_name(raw_item_name),
                cells=cells,
                source_page=None,
                raw_line_text=line,
            )
        )
        row_id += 1

    return merge_multi_page_rows(rows)
