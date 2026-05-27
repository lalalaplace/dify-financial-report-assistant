import argparse
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import fitz
import psycopg2
from psycopg2.extras import execute_values

from statement_table_schema import (
    FINANCIAL_ROW_KEYWORDS,
    STATEMENT_TITLE_RULES,
    StatementCandidate,
    compact_text,
    detect_unit,
    normalize_text,
    score_statement_candidate,
)
from statement_locator_quality import formal_statement_page_score, is_formal_statement_candidate


DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DBNAME", "financial_reports"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCATOR_METHOD = "rule_title_match_v2"
TOP_K_CANDIDATES = 3
MAX_END_PAGE_SCAN = 3
TOP_TITLE_SCAN_LINES = 18

CONTINUATION_HEADER_KEYWORDS = [
    "项目",
    "附注",
    "本期",
    "上期",
    "期末",
    "期初",
    "本报告期",
    "上年同期",
]

CONTINUATION_ROW_HINTS = {
    "balance_sheet": ["短期借款", "应付票据", "应付账款", "流动负债合计", "负债合计", "所有者权益合计"],
    "income": ["营业收入", "营业成本", "销售费用", "管理费用", "净利润", "利润总额"],
    "cash_flow": [
        "销售商品",
        "收到其他与经营活动有关的现金",
        "经营活动现金流入小计",
        "取得借款收到的现金",
        "偿还债务支付的现金",
        "筹资活动产生的现金流量净额",
    ],
}

CONTINUATION_ROW_HINTS["balance_sheet"].extend(
    ["其他流动资产", "非流动资产", "固定资产", "在建工程", "长期借款", "一年内到期的非流动负债"]
)

OPTIONAL_LOCATOR_COLUMNS = [
    ("end_page_guess", "INTEGER"),
    ("title_text", "TEXT"),
    ("matched_keywords", "TEXT"),
    ("header_text", "TEXT"),
    ("is_consolidated", "BOOLEAN"),
    ("is_parent_only", "BOOLEAN"),
    ("locator_confidence", "DOUBLE PRECISION"),
    ("candidate_rank", "INTEGER"),
    ("extra_info_json", "TEXT"),
]


def resolve_pdf_path(file_path: str) -> Path:
    """将数据库中的路径转换为本地绝对路径。"""
    path = Path(file_path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def get_table_columns(conn, table_name: str) -> set:
    """读取真实字段集合。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    rows = cur.fetchall()
    cur.close()
    return {row[0] for row in rows}


def ensure_locator_schema(conn) -> set:
    """按需补齐 report_statement_locator 可选字段。"""
    cur = conn.cursor()
    try:
        for column_name, column_type in OPTIONAL_LOCATOR_COLUMNS:
            cur.execute(
                f"ALTER TABLE report_statement_locator ADD COLUMN IF NOT EXISTS {column_name} {column_type}"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return get_table_columns(conn, "report_statement_locator")


def fetch_target_files(conn, file_ids: Optional[List[int]] = None, limit: Optional[int] = None) -> List[Dict]:
    """读取待处理文件。"""
    cur = conn.cursor()
    where_parts = ["COALESCE(parse_status, 'pending') IN ('pending', 'parsed')"]
    params: List = []

    if file_ids:
        where_parts.append("file_id = ANY(%s)")
        params.append(file_ids)

    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(limit)

    cur.execute(
        f"""
        SELECT
            file_id,
            file_name,
            file_path,
            company_id,
            stock_code,
            stock_abbr,
            report_year,
            report_period,
            is_summary
        FROM report_file_index
        WHERE {' AND '.join(where_parts)}
        ORDER BY file_id
        {limit_sql}
        """,
        params,
    )
    rows = cur.fetchall()
    cur.close()

    result = []
    for row in rows:
        result.append(
            {
                "file_id": row[0],
                "file_name": row[1],
                "file_path": row[2],
                "company_id": row[3],
                "stock_code": row[4],
                "stock_abbr": row[5],
                "report_year": row[6],
                "report_period": row[7],
                "is_summary": row[8],
            }
        )
    return result


def read_pdf_pages(file_path: str) -> List[Dict]:
    """逐页读取 PDF 文本。"""
    path = resolve_pdf_path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF 文件不存在：{path}")

    pages: List[Dict] = []
    with fitz.open(path) as doc:
        for index, page in enumerate(doc):
            text = normalize_text(page.get_text("text"))
            lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
            pages.append(
                {
                    "page_num": index + 1,
                    "text": text,
                    "compact_text": compact_text(text),
                    "lines": lines,
                }
            )
    return pages


def extract_title_and_header(page: Dict, statement_type: str) -> Dict:
    """在页首区域提取标题与表头。"""
    lines = page["lines"][:20]
    title_text = ""
    matched_keywords: List[str] = []

    for line in lines:
        compact_line = compact_text(line)
        if not compact_line:
            continue
        for group in ("strong", "weak"):
            for keyword in STATEMENT_TITLE_RULES[statement_type][group]:
                keyword_compact = compact_text(keyword)
                if keyword_compact in compact_line:
                    if len(compact_line) <= 40 or keyword_compact == compact_line:
                        title_text = line
                        matched_keywords.append(keyword)
                        break
            if title_text:
                break
        if title_text:
            break

    if not title_text:
        for line in lines:
            compact_line = compact_text(line)
            if any(compact_text(keyword) in compact_line for keyword in STATEMENT_TITLE_RULES[statement_type]["strong"]):
                title_text = line
                matched_keywords.extend(STATEMENT_TITLE_RULES[statement_type]["strong"])
                break
            if any(compact_text(keyword) in compact_line for keyword in STATEMENT_TITLE_RULES[statement_type]["weak"]):
                title_text = line
                matched_keywords.extend(STATEMENT_TITLE_RULES[statement_type]["weak"])
                break

    header_lines: List[str] = []
    started = False
    for line in lines:
        compact_line = compact_text(line)
        if not compact_line:
            continue
        if title_text and compact_text(title_text) == compact_line:
            started = True
            continue
        if (
            "项目" in line
            or "附注" in line
            or "本期" in line
            or "上期" in line
            or "期末" in line
            or "期初" in line
            or "本报告期" in line
            or "上年同期" in line
            or "单位" in line
            or "币种" in line
            or "年月日" in compact_line
            or "年" in line and "月" in line and "日" in line
        ):
            header_lines.append(line)
            started = True
        elif started and header_lines and len(header_lines) < 4:
            header_lines.append(line)

    return {
        "title_text": title_text,
        "matched_keywords": sorted(set(matched_keywords)),
        "header_text": "\n".join(header_lines[:4]),
    }


def infer_parent_only(page: Dict, title_text: str) -> bool:
    """判断是否是母公司表。"""
    compact_page = page["compact_text"]
    compact_title = compact_text(title_text)

    if "母公司" in compact_title:
        return True
    if "母公司" in compact_page and "合并" not in compact_title:
        return True
    return False


def calc_table_density_score(page: Dict) -> float:
    """计算表格密度分。"""
    numeric_lines = 0
    aligned_lines = 0
    for line in page["lines"][:60]:
        number_count = len(re_find_numbers(line))
        if number_count >= 2:
            numeric_lines += 1
        if number_count >= 2 and len(line) >= 20:
            aligned_lines += 1
    return min(numeric_lines * 2.5 + aligned_lines * 1.5, 30.0)


def re_find_numbers(text: str) -> List[str]:
    """识别数字片段。"""
    from statement_table_schema import NUMBER_TOKEN_PATTERN

    return NUMBER_TOKEN_PATTERN.findall(text or "")


def calc_financial_keyword_score(page: Dict, statement_type: str) -> float:
    """根据典型项目名评分。"""
    compact_page = page["compact_text"]
    keywords = FINANCIAL_ROW_KEYWORDS.get(statement_type, [])
    hits = [keyword for keyword in keywords if compact_text(keyword) in compact_page]
    return min(len(hits) * 4.0, 20.0)


def page_has_title_near_top(page: Dict, statement_type: str) -> bool:
    """要求报表标题出现在页首区域，避免正文说明页误入候选。"""
    title_rules = STATEMENT_TITLE_RULES.get(statement_type, {})
    for line in page["lines"][:TOP_TITLE_SCAN_LINES]:
        compact_line = compact_text(line)
        if not compact_line:
            continue
        for group in ("strong", "weak"):
            for keyword in title_rules.get(group, []):
                if compact_text(keyword) in compact_line:
                    return True
    return False


def calc_continuation_structured_score(page: Dict, statement_type: str) -> float:
    """结合重复表头和典型明细行，对续页进行结构化评分。"""
    lines = page["lines"][:35]
    top_lines = lines[:10]
    compact_lines = [compact_text(line) for line in lines if compact_text(line)]
    top_compact_lines = [compact_text(line) for line in top_lines if compact_text(line)]
    compact_header_keywords = [compact_text(item) for item in CONTINUATION_HEADER_KEYWORDS]

    header_hits = sum(1 for line in top_compact_lines if any(keyword in line for keyword in compact_header_keywords))
    numeric_lines = sum(1 for line in lines if len(re_find_numbers(line)) >= 2)

    row_hint_hits = 0
    row_hints = FINANCIAL_ROW_KEYWORDS.get(statement_type, []) + CONTINUATION_ROW_HINTS.get(statement_type, [])
    for hint in row_hints:
        compact_hint = compact_text(hint)
        if compact_hint and any(compact_hint in line for line in compact_lines):
            row_hint_hits += 1

    long_sentence_lines = sum(1 for line in lines[:20] if len(compact_text(line)) >= 40 and len(re_find_numbers(line)) <= 1)
    score = header_hits * 2.5 + min(numeric_lines, 8) * 0.8 + min(row_hint_hits, 6) * 1.4
    score -= min(long_sentence_lines, 4) * 0.6
    return score


def looks_like_table_continuation(page: Dict, statement_type: str) -> bool:
    """判断下一页是否仍像当前报表。"""
    table_density = calc_table_density_score(page)
    keyword_score = calc_financial_keyword_score(page, statement_type)
    unit, _currency = detect_unit(page["lines"][:10])
    score = table_density + keyword_score + (2.0 if unit else 0.0)
    return score >= 8.0


def guess_end_page(pages: Sequence[Dict], start_index: int, statement_type: str) -> Optional[int]:
    """从起始页向后猜测报表结束页。"""
    if start_index < 0 or start_index >= len(pages):
        return None

    end_page = pages[start_index]["page_num"]
    for offset in range(1, MAX_END_PAGE_SCAN + 1):
        page_index = start_index + offset
        if page_index >= len(pages):
            break
        if looks_like_table_continuation(pages[page_index], statement_type):
            end_page = pages[page_index]["page_num"]
        else:
            break
    return end_page


def build_candidate(file_id: int, statement_type: str, page: Dict, page_index: int) -> StatementCandidate:
    """构造候选报表对象。"""
    extracted = extract_title_and_header(page, statement_type)
    title_text = extracted["title_text"]
    header_text = extracted["header_text"]
    is_consolidated = "合并" in compact_text(title_text or page["text"][:80])
    is_parent_only = infer_parent_only(page, title_text)

    base_score, score_meta = score_statement_candidate(
        statement_type=statement_type,
        title_text=title_text,
        page_text=page["text"],
        header_text=header_text,
        is_consolidated=is_consolidated,
        is_parent_only=is_parent_only,
    )

    table_density_score = calc_table_density_score(page)
    financial_row_score = calc_financial_keyword_score(page, statement_type)
    final_score = base_score + table_density_score * 0.3 + financial_row_score * 0.3

    return StatementCandidate(
        file_id=file_id,
        statement_type=statement_type,
        start_page=page["page_num"],
        end_page_guess=None,
        title_text=title_text,
        matched_keywords=sorted(set(extracted["matched_keywords"] + score_meta["matched_keywords"])),
        header_text=header_text,
        is_consolidated=is_consolidated,
        is_parent_only=is_parent_only,
        locator_confidence=max(0.0, min(final_score / 100.0, 1.0)),
        candidate_rank=1,
        locator_status="success" if final_score >= 35 else "weak_match",
        locator_method=LOCATOR_METHOD,
        source_text="\n".join(page["lines"][:12])[:1500],
        extra_info={
            "score": round(final_score, 2),
            "page_num": page["page_num"],
            "title_score": score_meta["title_score"],
            "table_density_score": round(table_density_score, 2),
            "financial_row_score": round(financial_row_score, 2),
            "header_bonus": score_meta["header_bonus"],
            "summary_penalty": score_meta["summary_penalty"],
            "parent_only_penalty": score_meta["parent_only_penalty"],
        },
    )


def locate_statement_candidates(file_id: int, pages: Sequence[Dict], statement_type: str) -> List[StatementCandidate]:
    """定位单个报表的候选页。"""
    candidates: List[StatementCandidate] = []
    title_rules = STATEMENT_TITLE_RULES.get(statement_type, {})

    for page_index, page in enumerate(pages):
        compact_page = page["compact_text"]
        if not compact_page:
            continue

        page_has_title = False
        for group in ("strong", "weak"):
            if any(compact_text(keyword) in compact_page for keyword in title_rules.get(group, [])):
                page_has_title = True
                break

        if not page_has_title:
            continue

        candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
        candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -(item.extra_info.get("score") or 0.0),
            not item.is_consolidated,
            item.is_parent_only,
            item.start_page or 999999,
        )
    )

    top_candidates = candidates[:TOP_K_CANDIDATES]
    for index, candidate in enumerate(top_candidates, start=1):
        candidate.candidate_rank = index
    return top_candidates


def find_standalone_title_positions(page: Dict, statement_type: str) -> List[int]:
    """查找页面内独立报表标题所在的行号。"""
    positions: List[int] = []
    for index, line in enumerate(page["lines"][:220]):
        if is_standalone_statement_title_line(line, statement_type):
            positions.append(index)
    return positions


def page_has_late_title(page: Dict, statement_type: str) -> bool:
    """判断标题是否出现在页中后段，用于识别页末起表。"""
    positions = find_standalone_title_positions(page, statement_type)
    if not positions:
        return False
    last_position = positions[-1]
    total_lines = max(len(page["lines"]), 1)
    return last_position >= 55 or last_position >= int(total_lines * 0.6)


def page_has_dense_header_block(page: Dict, statement_type: str) -> bool:
    """判断页面开头是否像报表正文，允许上一页提供标题。"""
    lines = page["lines"][:35]
    compact_lines = [compact_text(line) for line in lines if compact_text(line)]
    if not compact_lines:
        return False

    header_hits = sum(
        1
        for line in compact_lines[:12]
        if any(compact_text(keyword) in line for keyword in CONTINUATION_HEADER_KEYWORDS)
    )
    numeric_lines = sum(1 for line in lines[:25] if len(re_find_numbers(line)) >= 2)
    row_hint_hits = 0
    for hint in FINANCIAL_ROW_KEYWORDS.get(statement_type, []) + CONTINUATION_ROW_HINTS.get(statement_type, []):
        compact_hint = compact_text(hint)
        if compact_hint and any(compact_hint in line for line in compact_lines[:25]):
            row_hint_hits += 1

    if header_hits >= 1 and numeric_lines >= 3 and row_hint_hits >= 1:
        return True
    return looks_like_table_continuation(page, statement_type)


def page_has_body_after_last_title(page: Dict, statement_type: str) -> bool:
    """判断页末标题后是否已进入报表正文。"""
    positions = find_standalone_title_positions(page, statement_type)
    if not positions:
        return False
    last_position = positions[-1]
    trailing_lines = [line for line in page["lines"][last_position + 1 :] if compact_text(line)]
    trailing_nonempty = len(trailing_lines)
    trailing_numeric = sum(1 for line in trailing_lines[:18] if len(re_find_numbers(line)) >= 1)
    trailing_row_hints = 0
    for hint in FINANCIAL_ROW_KEYWORDS.get(statement_type, []) + CONTINUATION_ROW_HINTS.get(statement_type, []):
        compact_hint = compact_text(hint)
        if compact_hint and any(compact_hint in compact_text(line) for line in trailing_lines[:20]):
            trailing_row_hints += 1
    return trailing_nonempty >= 8 and (trailing_numeric >= 2 or trailing_row_hints >= 1)


def calc_restatement_penalty(page: Dict) -> float:
    """对会计政策变更、追溯调整等重列表页面施加惩罚，避免压过原始报表页。"""
    compact_page = page["compact_text"]
    penalty = 0.0
    penalty_keywords = [
        "调整当年年初财务报表",
        "首次执行新会计准则",
        "会计政策变更",
        "调整数",
        "追溯调整",
    ]
    for keyword in penalty_keywords:
        if compact_text(keyword) in compact_page:
            penalty += 16.0
    if "2022年1月1日" in page["text"] and "2021年12月31日" in page["text"] and "调整数" in page["text"]:
        penalty += 20.0
    return penalty


def page_has_title_near_top(page: Dict, statement_type: str) -> bool:
    """优先识别页首标题，同时允许页末起表场景进入候选。"""
    positions = find_standalone_title_positions(page, statement_type)
    if not positions:
        return False
    first_position = positions[0]
    total_lines = max(len(page["lines"]), 1)
    return first_position <= 45 or first_position <= int(total_lines * 0.35)


def build_candidate(
    file_id: int,
    statement_type: str,
    page: Dict,
    page_index: int,
    title_page: Optional[Dict] = None,
    shifted_start: bool = False,
) -> StatementCandidate:
    """构造候选，并支持上一页标题、下一页正文的页末起表场景。"""
    title_source_page = title_page or page
    extracted = extract_title_and_header(title_source_page, statement_type)
    title_text = extracted["title_text"]
    header_text = extracted["header_text"]
    title_near_top = page_has_title_near_top(title_source_page, statement_type)
    is_consolidated = "合并" in compact_text(title_text or title_source_page["text"][:120])
    is_parent_only = infer_parent_only(title_source_page, title_text)

    page_text_for_score = page["text"]
    if shifted_start:
        page_text_for_score = f"{title_source_page['text']}\n{page['text']}"

    base_score, score_meta = score_statement_candidate(
        statement_type=statement_type,
        title_text=title_text,
        page_text=page_text_for_score,
        header_text=header_text,
        is_consolidated=is_consolidated,
        is_parent_only=is_parent_only,
    )

    table_density_score = calc_table_density_score(page)
    financial_row_score = calc_financial_keyword_score(page, statement_type)
    formal_page_score = formal_statement_page_score(page, statement_type, title_text)
    restatement_penalty = calc_restatement_penalty(page) + (0.5 * calc_restatement_penalty(title_source_page))
    final_score = base_score + table_density_score * 0.3 + financial_row_score * 0.3 + formal_page_score * 2.0 - restatement_penalty
    if title_near_top:
        final_score += 18.0
    elif title_text:
        final_score += 6.0
    else:
        final_score -= 22.0
    if shifted_start:
        final_score += 10.0
    if not compact_text(header_text):
        final_score -= 4.0

    source_lines: List[str] = []
    if shifted_start:
        source_lines.extend(title_source_page["lines"][-12:])
    source_lines.extend(page["lines"][:12])

    return StatementCandidate(
        file_id=file_id,
        statement_type=statement_type,
        start_page=page["page_num"],
        end_page_guess=None,
        title_text=title_text,
        matched_keywords=sorted(set(extracted["matched_keywords"] + score_meta["matched_keywords"])),
        header_text=header_text,
        is_consolidated=is_consolidated,
        is_parent_only=is_parent_only,
        locator_confidence=max(0.0, min(final_score / 100.0, 1.0)),
        candidate_rank=1,
        locator_status="success" if final_score >= 35 else "weak_match",
        locator_method=LOCATOR_METHOD,
        source_text="\n".join(source_lines)[:1500],
        extra_info={
            "score": round(final_score, 2),
            "page_num": page["page_num"],
            "title_near_top": title_near_top,
            "title_score": score_meta["title_score"],
            "table_density_score": round(table_density_score, 2),
            "financial_row_score": round(financial_row_score, 2),
            "formal_page_score": formal_page_score,
            "header_bonus": score_meta["header_bonus"],
            "summary_penalty": score_meta["summary_penalty"],
            "parent_only_penalty": score_meta["parent_only_penalty"],
            "restatement_penalty": round(restatement_penalty, 2),
            "shifted_start": shifted_start,
            "title_page_num": title_source_page["page_num"],
        },
    )


def locate_statement_candidates(file_id: int, pages: Sequence[Dict], statement_type: str) -> List[StatementCandidate]:
    """定位单个报表候选页，补充页末起表和重列表降权。"""
    candidates: List[StatementCandidate] = []
    seen_keys = set()

    for page_index, page in enumerate(pages):
        compact_page = page["compact_text"]
        if not compact_page:
            continue

        if page_has_title_near_top(page, statement_type):
            candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            key = (candidate.start_page, candidate.extra_info.get("title_page_num"), False)
            if key not in seen_keys and is_formal_statement_candidate(page, statement_type, candidate.title_text):
                seen_keys.add(key)
                candidates.append(candidate)

        elif page_has_late_title(page, statement_type) and page_has_body_after_last_title(page, statement_type):
            current_candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            current_candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            key = (current_candidate.start_page, current_candidate.extra_info.get("title_page_num"), False)
            if key not in seen_keys and is_formal_statement_candidate(page, statement_type, current_candidate.title_text):
                seen_keys.add(key)
                candidates.append(current_candidate)
        elif (
            statement_type in {"income", "cash_flow"}
            and formal_statement_page_score(page, statement_type, "") >= 10.0
            and page_has_dense_header_block(page, statement_type)
        ):
            # 部分季度报告的 PDF 文本顺序会丢失利润表/现金流量表标题，但正文页结构仍然清晰。
            titleless_candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            titleless_candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            titleless_candidate.extra_info["titleless_formal_fallback"] = True
            key = (titleless_candidate.start_page, titleless_candidate.extra_info.get("title_page_num"), "titleless")
            if key not in seen_keys:
                seen_keys.add(key)
                candidates.append(titleless_candidate)
        elif (
            statement_type in {"income", "cash_flow"}
            and formal_statement_page_score(page, statement_type, "") >= 10.0
            and page_has_dense_header_block(page, statement_type)
        ):
            # 部分季度报告的 PDF 文本顺序会丢失利润表/现金流量表标题，但正文页结构仍然清晰。
            titleless_candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            titleless_candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            titleless_candidate.extra_info["titleless_formal_fallback"] = True
            key = (titleless_candidate.start_page, titleless_candidate.extra_info.get("title_page_num"), "titleless")
            if key not in seen_keys:
                seen_keys.add(key)
                candidates.append(titleless_candidate)

        allow_shifted_start = statement_type == "cash_flow" or not page_has_body_after_last_title(page, statement_type)
        if allow_shifted_start and page_has_late_title(page, statement_type) and page_index + 1 < len(pages):
            next_page = pages[page_index + 1]
            if page_has_dense_header_block(next_page, statement_type):
                shifted_candidate = build_candidate(
                    file_id=file_id,
                    statement_type=statement_type,
                    page=next_page,
                    page_index=page_index + 1,
                    title_page=page,
                    shifted_start=True,
                )
                shifted_candidate.end_page_guess = guess_end_page(pages, page_index + 1, statement_type)
                key = (shifted_candidate.start_page, shifted_candidate.extra_info.get("title_page_num"), True)
                if key not in seen_keys and is_formal_statement_candidate(next_page, statement_type, shifted_candidate.title_text):
                    seen_keys.add(key)
                    candidates.append(shifted_candidate)

    candidates.sort(
        key=lambda item: (
            -(item.extra_info.get("score") or 0.0),
            item.extra_info.get("restatement_penalty") or 0.0,
            not item.is_consolidated,
            item.is_parent_only,
            item.start_page or 999999,
        )
    )

    top_candidates = candidates[:TOP_K_CANDIDATES]
    for index, candidate in enumerate(top_candidates, start=1):
        candidate.candidate_rank = index
    return top_candidates


def calc_continuation_structured_score(page: Dict, statement_type: str) -> float:
    """增强续页结构评分，优先识别负债页和筹资活动页。"""
    lines = page["lines"][:45]
    compact_lines = [compact_text(line) for line in lines if compact_text(line)]
    top_compact_lines = compact_lines[:12]
    compact_header_keywords = [compact_text(item) for item in CONTINUATION_HEADER_KEYWORDS]

    header_hits = sum(1 for line in top_compact_lines if any(keyword in line for keyword in compact_header_keywords))
    numeric_lines = sum(1 for line in lines if len(re_find_numbers(line)) >= 2)
    short_row_lines = sum(1 for line in lines if len(re_find_numbers(line)) >= 1 and 2 <= len(compact_text(line)) <= 24)

    row_hint_hits = 0
    for hint in FINANCIAL_ROW_KEYWORDS.get(statement_type, []) + CONTINUATION_ROW_HINTS.get(statement_type, []):
        compact_hint = compact_text(hint)
        if compact_hint and any(compact_hint in line for line in compact_lines):
            row_hint_hits += 1

    long_sentence_lines = sum(1 for line in lines[:20] if len(compact_text(line)) >= 40 and len(re_find_numbers(line)) <= 1)
    score = (
        header_hits * 2.0
        + min(numeric_lines, 12) * 1.0
        + min(short_row_lines, 12) * 0.35
        + min(row_hint_hits, 8) * 1.8
    )
    score -= min(long_sentence_lines, 5) * 0.5
    return score


def looks_like_table_continuation(page: Dict, statement_type: str) -> bool:
    """放宽对续页的识别，但仍避免纯正文页误扩。"""
    table_density = calc_table_density_score(page)
    keyword_score = calc_financial_keyword_score(page, statement_type)
    structured_score = calc_continuation_structured_score(page, statement_type)
    unit, _currency = detect_unit(page["lines"][:12])
    score = table_density + keyword_score + structured_score + (2.0 if unit else 0.0)

    if statement_type == "balance_sheet":
        if structured_score >= 8.0 and table_density >= 4.0:
            return True
        return score >= 16.0
    if statement_type == "cash_flow":
        if structured_score >= 7.0 and table_density >= 4.0:
            return True
        return score >= 14.0
    if structured_score >= 7.0 and table_density >= 4.0:
        return True
    return score >= 13.0


def is_standalone_statement_title_line(line: str, statement_type: str) -> bool:
    """识别真正的报表标题行，过滤掉正文说明中的长句引用。"""
    compact_line = compact_text(line)
    if not compact_line:
        return False

    normalized_prefix = compact_line
    while normalized_prefix and normalized_prefix[0] in "0123456789一二三四五六七八九十()（）.．、":
        normalized_prefix = normalized_prefix[1:]

    if len(normalized_prefix) > 18:
        return False

    for group in ("strong", "weak"):
        for keyword in STATEMENT_TITLE_RULES.get(statement_type, {}).get(group, []):
            compact_keyword = compact_text(keyword)
            if normalized_prefix == compact_keyword:
                return True
    return False


def page_has_title_near_top(page: Dict, statement_type: str) -> bool:
    """允许标题出现在页中下部，但必须是独立短标题行。"""
    for line in page["lines"][:40]:
        if is_standalone_statement_title_line(line, statement_type):
            return True
    return False


def calc_continuation_structured_score(page: Dict, statement_type: str) -> float:
    """结合短行密度、重复表头和典型项目行判断是否为续页。"""
    lines = page["lines"][:40]
    top_lines = lines[:12]
    compact_lines = [compact_text(line) for line in lines if compact_text(line)]
    top_compact_lines = [compact_text(line) for line in top_lines if compact_text(line)]
    compact_header_keywords = [compact_text(item) for item in CONTINUATION_HEADER_KEYWORDS]

    header_hits = sum(1 for line in top_compact_lines if any(keyword in line for keyword in compact_header_keywords))
    numeric_lines = sum(1 for line in lines if len(re_find_numbers(line)) >= 2)
    short_row_lines = sum(1 for line in lines if 1 <= len(re_find_numbers(line)) <= 2 and 2 <= len(compact_text(line)) <= 18)

    row_hint_hits = 0
    row_hints = FINANCIAL_ROW_KEYWORDS.get(statement_type, []) + CONTINUATION_ROW_HINTS.get(statement_type, [])
    for hint in row_hints:
        compact_hint = compact_text(hint)
        if compact_hint and any(compact_hint in line for line in compact_lines):
            row_hint_hits += 1

    long_sentence_lines = sum(1 for line in lines[:20] if len(compact_text(line)) >= 40 and len(re_find_numbers(line)) <= 1)
    score = (
        header_hits * 2.5
        + min(numeric_lines, 10) * 0.9
        + min(short_row_lines, 10) * 0.45
        + min(row_hint_hits, 8) * 1.5
    )
    score -= min(long_sentence_lines, 4) * 0.6
    return score


def looks_like_table_continuation(page: Dict, statement_type: str) -> bool:
    """增强续页判断，避免跨页报表被过早截断。"""
    table_density = calc_table_density_score(page)
    keyword_score = calc_financial_keyword_score(page, statement_type)
    structured_score = calc_continuation_structured_score(page, statement_type)
    unit, _currency = detect_unit(page["lines"][:12])
    score = table_density + keyword_score + structured_score + (2.0 if unit else 0.0)
    if table_density >= 12.0 and structured_score >= 7.0:
        return True
    if structured_score >= 8.0 and table_density >= 6.0:
        return True
    return score >= 12.0


def looks_like_table_continuation(page: Dict, statement_type: str) -> bool:
    """增强续页判断，降低跨页报表被提前截断的概率。"""
    table_density = calc_table_density_score(page)
    keyword_score = calc_financial_keyword_score(page, statement_type)
    structured_score = calc_continuation_structured_score(page, statement_type)
    unit, _currency = detect_unit(page["lines"][:10])
    score = table_density + keyword_score + structured_score + (2.0 if unit else 0.0)
    if structured_score >= 6.0 and table_density >= 4.0:
        return True
    return score >= 10.0


def build_candidate(file_id: int, statement_type: str, page: Dict, page_index: int) -> StatementCandidate:
    """要求首页标题出现在页首，并对缺标题候选做强惩罚。"""
    extracted = extract_title_and_header(page, statement_type)
    title_text = extracted["title_text"]
    header_text = extracted["header_text"]
    title_near_top = page_has_title_near_top(page, statement_type)
    is_consolidated = "鍚堝苟" in compact_text(title_text or page["text"][:80])
    is_parent_only = infer_parent_only(page, title_text)

    base_score, score_meta = score_statement_candidate(
        statement_type=statement_type,
        title_text=title_text,
        page_text=page["text"],
        header_text=header_text,
        is_consolidated=is_consolidated,
        is_parent_only=is_parent_only,
    )

    table_density_score = calc_table_density_score(page)
    financial_row_score = calc_financial_keyword_score(page, statement_type)
    final_score = base_score + table_density_score * 0.3 + financial_row_score * 0.3
    if title_near_top:
        final_score += 18.0
    elif title_text:
        final_score += 6.0
    else:
        final_score -= 22.0
    if not compact_text(header_text):
        final_score -= 4.0

    return StatementCandidate(
        file_id=file_id,
        statement_type=statement_type,
        start_page=page["page_num"],
        end_page_guess=None,
        title_text=title_text,
        matched_keywords=sorted(set(extracted["matched_keywords"] + score_meta["matched_keywords"])),
        header_text=header_text,
        is_consolidated=is_consolidated,
        is_parent_only=is_parent_only,
        locator_confidence=max(0.0, min(final_score / 100.0, 1.0)),
        candidate_rank=1,
        locator_status="success" if final_score >= 35 else "weak_match",
        locator_method=LOCATOR_METHOD,
        source_text="\n".join(page["lines"][:12])[:1500],
        extra_info={
            "score": round(final_score, 2),
            "page_num": page["page_num"],
            "title_near_top": title_near_top,
            "title_score": score_meta["title_score"],
            "table_density_score": round(table_density_score, 2),
            "financial_row_score": round(financial_row_score, 2),
            "header_bonus": score_meta["header_bonus"],
            "summary_penalty": score_meta["summary_penalty"],
            "parent_only_penalty": score_meta["parent_only_penalty"],
        },
    )


def locate_statement_candidates(file_id: int, pages: Sequence[Dict], statement_type: str) -> List[StatementCandidate]:
    """只接受页首区域出现标题的候选页，避免正文说明页误选。"""
    candidates: List[StatementCandidate] = []

    for page_index, page in enumerate(pages):
        compact_page = page["compact_text"]
        if not compact_page:
            continue
        if not page_has_title_near_top(page, statement_type):
            continue

        candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
        candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
        candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            -(item.extra_info.get("score") or 0.0),
            not item.is_consolidated,
            item.is_parent_only,
            item.start_page or 999999,
        )
    )

    top_candidates = candidates[:TOP_K_CANDIDATES]
    for index, candidate in enumerate(top_candidates, start=1):
        candidate.candidate_rank = index
    return top_candidates


def choose_best_candidate(candidates: Sequence[StatementCandidate]) -> StatementCandidate:
    """选择最终候选。"""
    if candidates:
        return candidates[0]

    return StatementCandidate(
        file_id=-1,
        statement_type="",
        start_page=None,
        end_page_guess=None,
        locator_status="not_found",
        locator_method=LOCATOR_METHOD,
    )


def upsert_locator_rows(conn, file_id: int, rows: List[StatementCandidate], table_columns: set) -> None:
    """写入或更新定位结果。"""
    insert_columns = [
        "file_id",
        "statement_type",
        "page_start",
        "page_end",
        "locator_method",
        "locator_status",
        "source_text",
    ]
    value_getters = {
        "file_id": lambda row: file_id,
        "statement_type": lambda row: row.statement_type,
        "page_start": lambda row: row.start_page,
        "page_end": lambda row: row.end_page_guess,
        "locator_method": lambda row: row.locator_method,
        "locator_status": lambda row: row.locator_status,
        "source_text": lambda row: row.source_text,
        "end_page_guess": lambda row: row.end_page_guess,
        "title_text": lambda row: row.title_text,
        "matched_keywords": lambda row: json.dumps(row.matched_keywords, ensure_ascii=False),
        "header_text": lambda row: row.header_text,
        "is_consolidated": lambda row: row.is_consolidated,
        "is_parent_only": lambda row: row.is_parent_only,
        "locator_confidence": lambda row: row.locator_confidence,
        "candidate_rank": lambda row: row.candidate_rank,
        "extra_info_json": lambda row: json.dumps(row.extra_info, ensure_ascii=False),
    }

    for optional_column, _column_type in OPTIONAL_LOCATOR_COLUMNS:
        if optional_column in table_columns:
            insert_columns.append(optional_column)

    update_columns = [column for column in insert_columns if column not in {"file_id", "statement_type"}]
    cur = conn.cursor()
    try:
        insert_rows = [tuple(value_getters[column](row) for column in insert_columns) for row in rows]
        execute_values(
            cur,
            f"""
            INSERT INTO report_statement_locator ({", ".join(insert_columns)})
            VALUES %s
            ON CONFLICT (file_id, statement_type)
            DO UPDATE SET
            {", ".join(f"{column} = EXCLUDED.{column}" for column in update_columns)},
            updated_at = CURRENT_TIMESTAMP
            """,
            insert_rows,
            page_size=100,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def process_single_file(conn, table_columns: set, file_meta: Dict) -> None:
    """处理单个 PDF。"""
    file_id = file_meta["file_id"]
    file_name = file_meta["file_name"]

    try:
        pages = read_pdf_pages(file_meta["file_path"])
    except Exception as error:
        print(f"[失败] file_id={file_id} | file_name={file_name} | error={error}")
        return

    rows_to_write: List[StatementCandidate] = []
    for statement_type in ["balance_sheet", "income", "cash_flow"]:
        candidates = locate_statement_candidates(file_id=file_id, pages=pages, statement_type=statement_type)

        if candidates:
            for candidate in candidates:
                print(
                    f"[候选] file_id={file_id} | statement_type={statement_type} | "
                    f"page={candidate.start_page} | end_guess={candidate.end_page_guess} | "
                    f"score={candidate.extra_info.get('score')} | confidence={candidate.locator_confidence:.2f} | "
                    f"title={candidate.title_text or '无'} | consolidated={candidate.is_consolidated} | "
                    f"parent_only={candidate.is_parent_only} | rank={candidate.candidate_rank}"
                )

            best_candidate = candidates[0]
            best_candidate.extra_info["top_candidates"] = [
                {
                    "page": candidate.start_page,
                    "end_page_guess": candidate.end_page_guess,
                    "title_text": candidate.title_text,
                    "score": candidate.extra_info.get("score"),
                    "candidate_rank": candidate.candidate_rank,
                    "is_consolidated": candidate.is_consolidated,
                    "is_parent_only": candidate.is_parent_only,
                }
                for candidate in candidates
            ]
            rows_to_write.append(best_candidate)
            print(
                f"[选中] file_id={file_id} | statement_type={statement_type} | "
                f"page={best_candidate.start_page} | end_guess={best_candidate.end_page_guess} | "
                f"confidence={best_candidate.locator_confidence:.2f} | title={best_candidate.title_text or '无'}"
            )
        else:
            not_found = StatementCandidate(
                file_id=file_id,
                statement_type=statement_type,
                start_page=None,
                end_page_guess=None,
                locator_status="not_found",
                locator_method=LOCATOR_METHOD,
                source_text="",
                extra_info={"reason": "no_candidate_found"},
            )
            rows_to_write.append(not_found)
            print(f"[未找到] file_id={file_id} | statement_type={statement_type}")

    upsert_locator_rows(conn, file_id=file_id, rows=rows_to_write, table_columns=table_columns)


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="定位三大财务报表候选页并写入 report_statement_locator。")
    parser.add_argument("--file-id", type=int, nargs="*", help="仅处理指定 file_id，可传多个。")
    parser.add_argument("--limit", type=int, help="限制处理文件数量。")
    return parser.parse_args()


def main():
    """主流程。"""
    args = parse_args()
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        table_columns = ensure_locator_schema(conn)
        files = fetch_target_files(conn, file_ids=args.file_id, limit=args.limit)
        print(f"待处理文件数：{len(files)}")

        for file_meta in files:
            process_single_file(conn, table_columns=table_columns, file_meta=file_meta)

        print("处理完成。")
    finally:
        conn.close()


def page_has_title_near_top(page: Dict, statement_type: str) -> bool:
    """最终生效版：优先识别页首标题，同时兼容页中较靠前的独立标题。"""
    positions = find_standalone_title_positions(page, statement_type)
    if not positions:
        return False
    first_position = positions[0]
    total_lines = max(len(page["lines"]), 1)
    return first_position <= 45 or first_position <= int(total_lines * 0.35)


def build_candidate(
    file_id: int,
    statement_type: str,
    page: Dict,
    page_index: int,
    title_page: Optional[Dict] = None,
    shifted_start: bool = False,
) -> StatementCandidate:
    """最终生效版：支持上一页标题、下一页正文的页末起表场景。"""
    title_source_page = title_page or page
    extracted = extract_title_and_header(title_source_page, statement_type)
    title_text = extracted["title_text"]
    if not compact_text(title_text):
        positions = find_standalone_title_positions(title_source_page, statement_type)
        if positions:
            title_text = title_source_page["lines"][positions[-1]]
    header_text = extracted["header_text"]
    title_near_top = page_has_title_near_top(title_source_page, statement_type)
    is_consolidated = "合并" in compact_text(title_text or title_source_page["text"][:120])
    is_parent_only = infer_parent_only(title_source_page, title_text)

    page_text_for_score = page["text"]
    if shifted_start:
        page_text_for_score = f"{title_source_page['text']}\n{page['text']}"

    base_score, score_meta = score_statement_candidate(
        statement_type=statement_type,
        title_text=title_text,
        page_text=page_text_for_score,
        header_text=header_text,
        is_consolidated=is_consolidated,
        is_parent_only=is_parent_only,
    )

    table_density_score = calc_table_density_score(page)
    financial_row_score = calc_financial_keyword_score(page, statement_type)
    formal_page_score = formal_statement_page_score(page, statement_type, title_text)
    restatement_penalty = calc_restatement_penalty(page) + (0.5 * calc_restatement_penalty(title_source_page))
    final_score = base_score + table_density_score * 0.3 + financial_row_score * 0.3 + formal_page_score * 2.0 - restatement_penalty
    if title_near_top:
        final_score += 18.0
    elif title_text:
        final_score += 6.0
    else:
        final_score -= 22.0
    if shifted_start:
        final_score += 10.0
        if page_has_body_after_last_title(title_source_page, statement_type):
            final_score -= 18.0
    if not compact_text(header_text):
        final_score -= 4.0

    source_lines: List[str] = []
    if shifted_start:
        source_lines.extend(title_source_page["lines"][-12:])
    source_lines.extend(page["lines"][:12])

    return StatementCandidate(
        file_id=file_id,
        statement_type=statement_type,
        start_page=page["page_num"],
        end_page_guess=None,
        title_text=title_text,
        matched_keywords=sorted(set(extracted["matched_keywords"] + score_meta["matched_keywords"])),
        header_text=header_text,
        is_consolidated=is_consolidated,
        is_parent_only=is_parent_only,
        locator_confidence=max(0.0, min(final_score / 100.0, 1.0)),
        candidate_rank=1,
        locator_status="success" if final_score >= 35 else "weak_match",
        locator_method=LOCATOR_METHOD,
        source_text="\n".join(source_lines)[:1500],
        extra_info={
            "score": round(final_score, 2),
            "page_num": page["page_num"],
            "title_near_top": title_near_top,
            "title_score": score_meta["title_score"],
            "table_density_score": round(table_density_score, 2),
            "financial_row_score": round(financial_row_score, 2),
            "formal_page_score": formal_page_score,
            "header_bonus": score_meta["header_bonus"],
            "summary_penalty": score_meta["summary_penalty"],
            "parent_only_penalty": score_meta["parent_only_penalty"],
            "restatement_penalty": round(restatement_penalty, 2),
            "shifted_start": shifted_start,
            "title_page_num": title_source_page["page_num"],
        },
    )


def locate_statement_candidates(file_id: int, pages: Sequence[Dict], statement_type: str) -> List[StatementCandidate]:
    """最终生效版：补充页末起表，并给追溯调整报表降权。"""
    candidates: List[StatementCandidate] = []
    seen_keys = set()

    for page_index, page in enumerate(pages):
        compact_page = page["compact_text"]
        if not compact_page:
            continue

        if page_has_title_near_top(page, statement_type):
            candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            key = (candidate.start_page, candidate.extra_info.get("title_page_num"), False)
            if key not in seen_keys and is_formal_statement_candidate(page, statement_type, candidate.title_text):
                seen_keys.add(key)
                candidates.append(candidate)
        elif page_has_late_title(page, statement_type) and page_has_body_after_last_title(page, statement_type):
            current_candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            current_candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            key = (current_candidate.start_page, current_candidate.extra_info.get("title_page_num"), False)
            if key not in seen_keys and is_formal_statement_candidate(page, statement_type, current_candidate.title_text):
                seen_keys.add(key)
                candidates.append(current_candidate)

        if page_has_late_title(page, statement_type) and page_index + 1 < len(pages):
            next_page = pages[page_index + 1]
            if page_has_dense_header_block(next_page, statement_type):
                shifted_candidate = build_candidate(
                    file_id=file_id,
                    statement_type=statement_type,
                    page=next_page,
                    page_index=page_index + 1,
                    title_page=page,
                    shifted_start=True,
                )
                shifted_candidate.end_page_guess = guess_end_page(pages, page_index + 1, statement_type)
                key = (shifted_candidate.start_page, shifted_candidate.extra_info.get("title_page_num"), True)
                if key not in seen_keys and is_formal_statement_candidate(next_page, statement_type, shifted_candidate.title_text):
                    seen_keys.add(key)
                    candidates.append(shifted_candidate)

    candidates.sort(
        key=lambda item: (
            -(item.extra_info.get("score") or 0.0),
            item.extra_info.get("restatement_penalty") or 0.0,
            not item.is_consolidated,
            item.is_parent_only,
            item.start_page or 999999,
        )
    )

    top_candidates = candidates[:TOP_K_CANDIDATES]
    for index, candidate in enumerate(top_candidates, start=1):
        candidate.candidate_rank = index
    return top_candidates


def locate_statement_candidates(file_id: int, pages: Sequence[Dict], statement_type: str) -> List[StatementCandidate]:
    """最终定位：过滤说明页，兼容标题缺失但结构清晰的利润表/现金流量表正文页。"""
    candidates: List[StatementCandidate] = []
    seen_keys = set()

    def acceptable_candidate(candidate: StatementCandidate) -> bool:
        restatement_penalty = float(candidate.extra_info.get("restatement_penalty") or 0.0)
        formal_page_score = float(candidate.extra_info.get("formal_page_score") or 0.0)
        return restatement_penalty < 20.0 or formal_page_score >= 12.0

    for page_index, page in enumerate(pages):
        if not page["compact_text"]:
            continue

        if page_has_title_near_top(page, statement_type):
            candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            key = (candidate.start_page, candidate.extra_info.get("title_page_num"), "title")
            if key not in seen_keys and acceptable_candidate(candidate) and is_formal_statement_candidate(page, statement_type, candidate.title_text):
                seen_keys.add(key)
                candidates.append(candidate)
        elif page_has_late_title(page, statement_type) and page_has_body_after_last_title(page, statement_type):
            candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            key = (candidate.start_page, candidate.extra_info.get("title_page_num"), "late_title")
            if key not in seen_keys and acceptable_candidate(candidate) and is_formal_statement_candidate(page, statement_type, candidate.title_text):
                seen_keys.add(key)
                candidates.append(candidate)
        elif (
            statement_type in {"income", "cash_flow"}
            and "季度报告" in page["compact_text"]
            and formal_statement_page_score(page, statement_type, "") >= 10.0
            and page_has_dense_header_block(page, statement_type)
            and calc_financial_keyword_score(page, statement_type) >= 4.0
        ):
            candidate = build_candidate(file_id=file_id, statement_type=statement_type, page=page, page_index=page_index)
            candidate.end_page_guess = guess_end_page(pages, page_index, statement_type)
            candidate.extra_info["titleless_formal_fallback"] = True
            key = (candidate.start_page, "titleless")
            if key not in seen_keys and acceptable_candidate(candidate):
                seen_keys.add(key)
                candidates.append(candidate)

        if page_has_late_title(page, statement_type) and page_index + 1 < len(pages):
            next_page = pages[page_index + 1]
            if page_has_dense_header_block(next_page, statement_type):
                shifted_candidate = build_candidate(
                    file_id=file_id,
                    statement_type=statement_type,
                    page=next_page,
                    page_index=page_index + 1,
                    title_page=page,
                    shifted_start=True,
                )
                shifted_candidate.end_page_guess = guess_end_page(pages, page_index + 1, statement_type)
                key = (shifted_candidate.start_page, shifted_candidate.extra_info.get("title_page_num"), "shifted")
                if key not in seen_keys and acceptable_candidate(shifted_candidate) and is_formal_statement_candidate(next_page, statement_type, shifted_candidate.title_text):
                    seen_keys.add(key)
                    candidates.append(shifted_candidate)

    candidates.sort(
        key=lambda item: (
            -(item.extra_info.get("score") or 0.0),
            item.extra_info.get("restatement_penalty") or 0.0,
            not item.is_consolidated,
            item.is_parent_only,
            item.start_page or 999999,
        )
    )

    top_candidates = candidates[:TOP_K_CANDIDATES]
    for index, candidate in enumerate(top_candidates, start=1):
        candidate.candidate_rank = index
    return top_candidates


if __name__ == "__main__":
    main()
