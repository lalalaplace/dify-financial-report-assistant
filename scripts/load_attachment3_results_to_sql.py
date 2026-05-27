import argparse
import os
import re
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values


DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DBNAME", "financial_reports"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATEMENT_JSON_DIR = PROJECT_ROOT / "output" / "statement_json"

SUPPORTED_TARGET_TABLES = ["balance_sheet", "income", "cash_flow"]
SOURCE_TO_FINAL_TABLE = {
    "balance_sheet": "balance_sheet",
    "income": "income_sheet",
    "cash_flow": "cash_flow_sheet",
}
METHOD_PRIORITY = {
    "manual_backfill": 0,
    "rule": 1,
    "rule_candidate_fill": 2,
}
PERIOD_ORDER = {
    "Q1": 1,
    "HY": 2,
    "Q3": 3,
    "FY": 4,
}
MIN_REVENUE_FOR_TINY_PROFIT_CHECK = Decimal("100000000")
MAX_TINY_PROFIT_ABS_VALUE = Decimal("1000")
REQUIRED_KEY_COLUMNS = ["stock_code", "report_year", "report_period"]
BASE_OPTIONAL_COLUMNS = ["serial_number", "stock_abbr", "company_name"]
SKIP_FINAL_FIELD_NAMES = {
    "file_id",
    "company_id",
    "serial_number",
    "stock_code",
    "stock_abbr",
    "report_year",
    "report_period",
}


def configure_console() -> None:
    """配置控制台编码，避免 Windows 输出异常。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def safe_text(text) -> str:
    """将任意对象转换为安全文本。"""
    if text is None:
        return ""
    try:
        return str(text)
    except Exception:
        return repr(text)


def safe_print(*args) -> None:
    """安全输出。"""
    text = " ".join(safe_text(arg) for arg in args)
    try:
        print(text)
    except UnicodeEncodeError:
        encoded = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        print(encoded)


def normalize_text(value) -> str:
    """标准化文本。"""
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\u3000", " ")
    text = text.replace("\xa0", " ")
    return text.strip()


def normalize_stock_code(value) -> str:
    """统一股票代码格式。"""
    text = normalize_text(value)
    if not text:
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    text = re.sub(r"\s+", "", text)
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return text


def normalize_period(period: Optional[str]) -> Optional[str]:
    """统一报告期枚举值。"""
    if period is None:
        return None

    period_text = normalize_text(period).upper()
    mapping = {
        "Q1": "Q1",
        "FIRST_QUARTER": "Q1",
        "HY": "HY",
        "H1": "HY",
        "SEMIANNUAL": "HY",
        "SEMI_ANNUAL": "HY",
        "HALF_YEAR": "HY",
        "Q3": "Q3",
        "THIRD_QUARTER": "Q3",
        "FY": "FY",
        "ANNUAL": "FY",
        "YEAR": "FY",
        "YEARLY": "FY",
    }
    return mapping.get(period_text, period_text)


def should_skip_final_field_name(final_field_name: str) -> bool:
    """过滤不应回写到最终标准表的元字段。"""
    normalized_name = normalize_text(final_field_name)
    return normalized_name in SKIP_FINAL_FIELD_NAMES


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="从 attachment3_extract_result 择优取值并写入附件3标准最终表。")
    parser.add_argument("--file-id", type=int, nargs="*", help="仅处理指定 file_id，可传多个。")
    parser.add_argument("--limit", type=int, help="若未指定 --file-id，则只处理前 N 个 file_id。")
    parser.add_argument(
        "--target-table",
        choices=SUPPORTED_TARGET_TABLES,
        nargs="*",
        help="仅处理指定 target_table，可传多个。",
    )
    return parser.parse_args()


def parse_statement_json_name(file_name: str) -> Optional[Tuple[int, str]]:
    """从 statement_json 文件名中解析 file_id 和报表类型。"""
    match = re.match(r"file_(\d+)_(balance_sheet|income|cash_flow)\.json$", file_name)
    if not match:
        return None
    return int(match.group(1)), match.group(2)


def resolve_scope_file_ids_from_statement_json(limit: Optional[int]) -> Optional[List[int]]:
    """优先按 statement_json 中存在的 file_id 限流，保持与抽取阶段一致。"""
    if limit is None or not STATEMENT_JSON_DIR.exists():
        return None

    file_ids = set()
    for path in STATEMENT_JSON_DIR.glob("file_*_*.json"):
        parsed = parse_statement_json_name(path.name)
        if parsed is None:
            continue
        file_ids.add(parsed[0])

    if not file_ids:
        return None
    return sorted(file_ids)[:limit]


def fetch_table_columns(conn, table_name: str) -> Set[str]:
    """读取目标表真实列集合。"""
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


def fetch_field_dict(conn, target_table: str) -> List[Dict]:
    """读取字段字典，并生成最终标准列名。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            target_table,
            field_code,
            split_part(field_code, '.', 2) AS final_field_name,
            field_name_cn,
            data_type,
            sort_order
        FROM attachment3_field_dict
        WHERE target_table = %s
        ORDER BY sort_order, field_code
        """,
        (target_table,),
    )
    rows = cur.fetchall()
    cur.close()

    fields: List[Dict] = []
    for row in rows:
        final_field_name = normalize_text(row[2])
        if not final_field_name or should_skip_final_field_name(final_field_name):
            continue
        fields.append(
            {
                "target_table": row[0],
                "field_code": row[1],
                "final_field_name": final_field_name,
                "field_name_cn": row[3],
                "data_type": normalize_text(row[4]),
                "sort_order": row[5],
            }
        )
    return fields


def resolve_scope_file_ids(conn, file_ids: Optional[List[int]], limit: Optional[int]) -> Optional[List[int]]:
    """解析本次处理范围的 file_id 集合。"""
    if file_ids:
        return sorted(set(file_ids))
    if limit is None:
        return None

    statement_json_file_ids = resolve_scope_file_ids_from_statement_json(limit)
    if statement_json_file_ids is not None:
        return statement_json_file_ids

    cur = conn.cursor()
    cur.execute(
        """
        SELECT file_id
        FROM report_file_index
        ORDER BY file_id
        LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    return [row[0] for row in rows]


def fetch_report_meta(conn, file_ids: Optional[List[int]] = None) -> Dict[int, Dict]:
    """读取报告元数据。"""
    params: List = []
    where_sql = ""
    if file_ids:
        where_sql = "WHERE file_id = ANY(%s)"
        params.append(file_ids)

    report_columns = fetch_table_columns(conn, "report_file_index")
    is_summary_sql = "COALESCE(is_summary, FALSE) AS is_summary" if "is_summary" in report_columns else "FALSE AS is_summary"

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            file_id,
            stock_code,
            stock_abbr,
            company_name,
            report_year,
            report_period,
            {is_summary_sql}
        FROM report_file_index
        {where_sql}
        ORDER BY file_id
        """,
        params,
    )
    rows = cur.fetchall()
    cur.close()

    meta_map: Dict[int, Dict] = {}
    for row in rows:
        meta_map[row[0]] = {
            "file_id": row[0],
            "stock_code": normalize_stock_code(row[1]),
            "stock_abbr": normalize_text(row[2]),
            "company_name": normalize_text(row[3]),
            "report_year": row[4],
            "report_period": normalize_period(row[5]),
            "is_summary": bool(row[6]),
        }
    return meta_map


def build_logical_key_from_meta(meta: Dict) -> Optional[Tuple[str, int, str]]:
    """根据元数据构造逻辑键。"""
    stock_code = normalize_stock_code(meta.get("stock_code"))
    report_year = meta.get("report_year")
    report_period = normalize_period(meta.get("report_period"))
    if not stock_code or report_year is None or not report_period:
        return None
    return stock_code, report_year, report_period


def build_scope_key_map(meta_map: Dict[int, Dict]) -> Dict[Tuple[str, int, str], Set[int]]:
    """构造逻辑键到 file_id 集合的映射。"""
    scope_key_map: Dict[Tuple[str, int, str], Set[int]] = defaultdict(set)
    for file_id, meta in meta_map.items():
        logical_key = build_logical_key_from_meta(meta)
        if logical_key is None:
            continue
        scope_key_map[logical_key].add(file_id)
    return scope_key_map


def fetch_extract_results(conn, target_table: str, file_ids: Optional[List[int]] = None) -> List[Dict]:
    """读取候选抽取结果，并补齐择优所需信息。"""
    params: List = [target_table]
    file_filter_sql = ""
    if file_ids:
        file_filter_sql = "AND e.file_id = ANY(%s)"
        params.append(file_ids)

    extract_columns = fetch_table_columns(conn, "attachment3_extract_result")
    report_columns = fetch_table_columns(conn, "report_file_index")
    locator_columns = fetch_table_columns(conn, "report_statement_locator")

    result_confidence_sql = "COALESCE(e.confidence, 0) AS result_confidence" if "confidence" in extract_columns else "0 AS result_confidence"
    is_summary_sql = "COALESCE(r.is_summary, FALSE) AS is_summary" if "is_summary" in report_columns else "FALSE AS is_summary"

    if locator_columns:
        locator_confidence_sql = (
            "COALESCE(l.locator_confidence, 0) AS locator_confidence"
            if "locator_confidence" in locator_columns else "0 AS locator_confidence"
        )
        is_consolidated_sql = (
            "COALESCE(l.is_consolidated, FALSE) AS is_consolidated"
            if "is_consolidated" in locator_columns else "FALSE AS is_consolidated"
        )
        is_parent_only_sql = (
            "COALESCE(l.is_parent_only, FALSE) AS is_parent_only"
            if "is_parent_only" in locator_columns else "FALSE AS is_parent_only"
        )
        locator_join_sql = """
        LEFT JOIN report_statement_locator l
            ON e.file_id = l.file_id
           AND e.target_table = l.statement_type
        """
    else:
        locator_confidence_sql = "0 AS locator_confidence"
        is_consolidated_sql = "FALSE AS is_consolidated"
        is_parent_only_sql = "FALSE AS is_parent_only"
        locator_join_sql = ""

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            e.file_id,
            e.field_code,
            e.value_text,
            e.extract_method,
            e.llm_status,
            {result_confidence_sql},
            e.raw_line_name,
            e.normalized_line_name,
            e.source_page,
            e.source_column_role,
            e.source_text,
            r.stock_code,
            r.stock_abbr,
            r.company_name,
            r.report_year,
            r.report_period,
            {is_summary_sql},
            {locator_confidence_sql},
            {is_consolidated_sql},
            {is_parent_only_sql}
        FROM attachment3_extract_result e
        INNER JOIN report_file_index r
            ON e.file_id = r.file_id
        {locator_join_sql}
        WHERE e.target_table = %s
          AND e.extract_method IN ('rule', 'rule_candidate_fill', 'manual_backfill')
          AND COALESCE(e.field_code, '') <> ''
          AND COALESCE(e.value_text, '') <> ''
          {file_filter_sql}
        ORDER BY e.file_id, e.field_code, e.extract_method
        """,
        params,
    )
    rows = cur.fetchall()
    cur.close()

    results: List[Dict] = []
    for row in rows:
        results.append(
            {
                "file_id": row[0],
                "field_code": normalize_text(row[1]),
                "value_text": row[2],
                "extract_method": normalize_text(row[3]),
                "llm_status": normalize_text(row[4]),
                "result_confidence": float(row[5] or 0.0),
                "raw_line_name": normalize_text(row[6]),
                "normalized_line_name": normalize_text(row[7]),
                "source_page": row[8],
                "source_column_role": normalize_text(row[9]),
                "source_text": normalize_text(row[10]),
                "stock_code": normalize_stock_code(row[11]),
                "stock_abbr": normalize_text(row[12]),
                "company_name": normalize_text(row[13]),
                "report_year": row[14],
                "report_period": normalize_period(row[15]),
                "is_summary": bool(row[16]),
                "locator_confidence": float(row[17] or 0.0),
                "is_consolidated": bool(row[18]),
                "is_parent_only": bool(row[19]),
                "target_table": target_table,
            }
        )
    return results


def is_valid_candidate(row: Dict) -> bool:
    """判断候选结果是否可用于最终表。"""
    value_text = normalize_text(row.get("value_text"))
    extract_method = normalize_text(row.get("extract_method"))
    if not value_text:
        return False

    if extract_method == "rule":
        return True
    if extract_method == "manual_backfill":
        return True
    if extract_method == "rule_candidate_fill":
        return True
    return False


def parse_value(value_text: str, data_type: str):
    """按字段类型解析最终值。"""
    raw_text = normalize_text(value_text)
    if raw_text == "":
        return None

    dtype = normalize_text(data_type).lower()
    cleaned = raw_text.replace(",", "")
    cleaned = cleaned.replace("（", "(").replace("）", ")")
    cleaned = cleaned.replace("－", "-").replace("—", "-").replace("–", "-")
    cleaned = cleaned.strip()

    null_tokens = {"", "-", "--", "---", "不适用", "n/a", "nan", "无"}
    if cleaned.lower() in null_tokens:
        return None

    parentheses_match = re.match(r"^\((.+)\)$", cleaned)
    if parentheses_match:
        cleaned = f"-{parentheses_match.group(1).strip()}"

    cleaned = re.sub(r"\s+", "", cleaned)
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]

    if "int" in dtype:
        try:
            return int(Decimal(cleaned))
        except (InvalidOperation, ValueError):
            return None

    if "decimal" in dtype or "numeric" in dtype:
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None

    if "varchar" in dtype or "char" in dtype or "text" in dtype:
        return raw_text

    return raw_text


def get_decimal_value(record: Dict, field_name: str) -> Optional[Decimal]:
    """读取记录中的数值字段并转为 Decimal。"""
    value = record.get(field_name)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return None


def add_rejection(rejections: List[str], field_name: str, reason: str) -> None:
    """记录字段被拦截的原因。"""
    rejections.append(f"{field_name}:{reason}")


def parse_rejection_field(rejection: str) -> str:
    """从校验拦截文本中取出最终字段名。"""
    return rejection.split(":", 1)[0].strip()


def build_rejection_source_context(rejections: List[str], selected_candidates: Dict[str, Dict]) -> str:
    """生成校验拦截对应的来源行信息，便于从日志直接定位错抽来源。"""
    field_names: List[str] = []
    for rejection in rejections:
        field_name = parse_rejection_field(rejection)
        field_names.extend(name for name in field_name.split("/") if name)
    source_parts: List[str] = []
    for item_field_name in field_names:
        candidate = selected_candidates.get(item_field_name)
        if not candidate:
            continue
        source_parts.append(
            ",".join(
                [
                    f"field={item_field_name}",
                    f"file_id={candidate.get('file_id')}",
                    f"method={candidate.get('extract_method')}",
                    f"raw_line={normalize_text(candidate.get('raw_line_name'))}",
                    f"normalized_line={normalize_text(candidate.get('normalized_line_name'))}",
                    f"value={normalize_text(candidate.get('value_text'))}",
                    f"role={normalize_text(candidate.get('source_column_role'))}",
                    f"page={candidate.get('source_page')}",
                    f"confidence={candidate.get('result_confidence')}",
                ]
            )
        )
    if not source_parts:
        return "source=未找到已选候选"
    return "sources=" + " || ".join(source_parts)


def apply_record_validations(target_table: str, record: Dict) -> Tuple[Dict, List[str]]:
    """对最终记录做业务合理性校验。"""
    validated = dict(record)
    rejections: List[str] = []

    if target_table == "balance_sheet":
        total_assets = get_decimal_value(validated, "asset_total_assets")
        component_fields = [
            "asset_cash_and_cash_equivalents",
            "asset_accounts_receivable",
            "asset_inventory",
            "asset_trading_financial_assets",
            "asset_construction_in_progress",
            "liability_total_liabilities",
            "equity_total_equity",
        ]
        component_values = [
            value
            for value in (get_decimal_value(validated, field_name) for field_name in component_fields)
            if value is not None
        ]
        if total_assets is not None and component_values and total_assets < max(component_values):
            validated["asset_total_assets"] = None
            add_rejection(rejections, "asset_total_assets", "小于关键组成项或负债/权益，判定为错值")

        total_liabilities = get_decimal_value(validated, "liability_total_liabilities")
        if total_assets is not None and total_liabilities is not None and total_liabilities > total_assets:
            validated["liability_total_liabilities"] = None
            add_rejection(rejections, "liability_total_liabilities", "大于总资产，判定为错值")

    if target_table == "income":
        total_revenue = get_decimal_value(validated, "total_operating_revenue")
        net_profit = get_decimal_value(validated, "net_profit")
        if (
            total_revenue is not None
            and total_revenue >= MIN_REVENUE_FOR_TINY_PROFIT_CHECK
            and net_profit is not None
            and abs(net_profit) <= MAX_TINY_PROFIT_ABS_VALUE
        ):
            validated["net_profit"] = None
            add_rejection(rejections, "net_profit", "营业收入很大但净利润接近 0/1，疑似抽到序号或标记")

    if target_table == "cash_flow":
        net_fields = [
            "operating_cf_net_amount",
            "investing_cf_net_amount",
            "financing_cf_net_amount",
        ]
        non_null_values = [get_decimal_value(validated, field_name) for field_name in net_fields]
        non_null_values = [value for value in non_null_values if value is not None]
        if len(non_null_values) >= 3 and len(set(non_null_values)) == 1:
            for field_name in net_fields:
                validated[field_name] = None
            add_rejection(rejections, "operating/investing/financing_cf_net_amount", "三个净额完全相同，疑似重复填值")

    return validated, rejections


def build_row_priority(row: Dict) -> Tuple:
    """构造显式择优排序键。"""
    return (
        1 if row.get("is_summary") else 0,
        0 if row.get("is_consolidated") else 1,
        1 if row.get("is_parent_only") else 0,
        METHOD_PRIORITY.get(normalize_text(row.get("extract_method")), 999),
        -float(row.get("result_confidence") or 0.0),
        -float(row.get("locator_confidence") or 0.0),
        int(row.get("file_id") or 0),
    )


def build_priority_reason(row: Dict) -> str:
    """生成择优原因文本。"""
    return (
        f"is_summary={row.get('is_summary')} | "
        f"is_consolidated={row.get('is_consolidated')} | "
        f"is_parent_only={row.get('is_parent_only')} | "
        f"extract_method={row.get('extract_method')} | "
        f"result_confidence={row.get('result_confidence')} | "
        f"locator_confidence={row.get('locator_confidence')} | "
        f"file_id={row.get('file_id')}"
    )


def build_candidate_records(
    target_table: str,
    field_defs: List[Dict],
    candidate_rows: List[Dict],
) -> Tuple[List[Dict], List[str], List[str], Set[Tuple[str, int, str]]]:
    """将中间抽取结果聚合成附件3标准记录，并输出择优审计日志。"""
    field_map = {field["field_code"]: field for field in field_defs}
    grouped_candidates: Dict[Tuple[str, int, str], Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
    base_record_map: Dict[Tuple[str, int, str], Dict] = {}

    for row in candidate_rows:
        if not is_valid_candidate(row):
            continue
        field_def = field_map.get(row["field_code"])
        if field_def is None:
            continue

        logical_key = build_logical_key_from_meta(row)
        if logical_key is None:
            continue

        parsed_value = parse_value(row["value_text"], field_def["data_type"])
        if parsed_value is None:
            continue

        final_field_name = field_def["final_field_name"]
        if should_skip_final_field_name(final_field_name):
            continue
        candidate = dict(row)
        candidate["parsed_value"] = parsed_value
        candidate["final_field_name"] = final_field_name
        grouped_candidates[logical_key][final_field_name].append(candidate)
        base_record_map.setdefault(
            logical_key,
            {
                "stock_code": logical_key[0],
                "stock_abbr": normalize_text(row.get("stock_abbr")),
                "company_name": normalize_text(row.get("company_name")),
                "report_year": logical_key[1],
                "report_period": logical_key[2],
            },
        )

    records: List[Dict] = []
    validation_logs: List[str] = []
    selection_logs: List[str] = []
    selected_keys: Set[Tuple[str, int, str]] = set()

    sorted_keys = sorted(
        grouped_candidates.keys(),
        key=lambda item: (item[0], item[1], PERIOD_ORDER.get(item[2], 999), item[2]),
    )

    for logical_key in sorted_keys:
        record = dict(base_record_map[logical_key])
        field_candidates = grouped_candidates[logical_key]
        selected_candidates: Dict[str, Dict] = {}
        for field_def in field_defs:
            final_field_name = field_def["final_field_name"]
            candidates = field_candidates.get(final_field_name, [])
            if not candidates:
                continue
            best_candidate = sorted(candidates, key=build_row_priority)[0]
            record[final_field_name] = best_candidate["parsed_value"]
            selected_candidates[final_field_name] = best_candidate
            selection_logs.append(
                " | ".join(
                    [
                        f"target_table={target_table}",
                        f"stock_code={logical_key[0]}",
                        f"report_year={logical_key[1]}",
                        f"report_period={logical_key[2]}",
                        f"field_code={best_candidate['field_code']}",
                        f"source_file_id={best_candidate['file_id']}",
                        f"source_extract_method={best_candidate['extract_method']}",
                        f"source_confidence={best_candidate['result_confidence']}",
                        f"source_target_table={best_candidate['target_table']}",
                        f"reason={build_priority_reason(best_candidate)}",
                    ]
                )
            )

        validated_record, rejections = apply_record_validations(target_table, record)
        records.append(validated_record)
        selected_keys.add(logical_key)

        if rejections:
            validation_logs.append(
                " | ".join(
                    [
                        f"stock_code={logical_key[0]}",
                        f"report_year={logical_key[1]}",
                        f"report_period={logical_key[2]}",
                        f"target_table={target_table}",
                        f"rejections={';'.join(rejections)}",
                        build_rejection_source_context(rejections, selected_candidates),
                    ]
                )
            )

    return records, validation_logs, selection_logs, selected_keys


def build_insert_plan(conn, final_table_name: str, field_defs: List[Dict]) -> Tuple[List[str], List[str], Set[str]]:
    """探测目标表真实列，并生成本次实际写入列集合。"""
    actual_columns = fetch_table_columns(conn, final_table_name)
    if not actual_columns:
        raise RuntimeError(f"最终表不存在或不可访问：{final_table_name}")

    missing_required = [column for column in REQUIRED_KEY_COLUMNS if column not in actual_columns]
    if missing_required:
        raise RuntimeError(
            f"最终表缺少必要列：table={final_table_name} | missing_required={','.join(missing_required)}"
        )

    desired_columns = BASE_OPTIONAL_COLUMNS + REQUIRED_KEY_COLUMNS
    for field in field_defs:
        final_field_name = field["final_field_name"]
        if final_field_name not in desired_columns:
            desired_columns.append(final_field_name)

    missing_optional = [column for column in desired_columns if column not in actual_columns and column not in REQUIRED_KEY_COLUMNS]
    insert_columns = [column for column in desired_columns if column in actual_columns]

    safe_print(f"[目标表列] table={final_table_name} | actual_columns={','.join(sorted(actual_columns))}")
    if missing_optional:
        safe_print(f"[缺失可选列] table={final_table_name} | missing_optional={','.join(missing_optional)}")
    safe_print(f"[实际写入列] table={final_table_name} | insert_columns={','.join(insert_columns)}")

    return insert_columns, missing_optional, actual_columns


def fetch_existing_serial_numbers(
    conn,
    final_table_name: str,
    logical_keys: Iterable[Tuple[str, int, str]],
) -> Tuple[Dict[Tuple[str, int, str], int], int]:
    """读取已有序号，并返回当前最大序号。"""
    key_list = sorted(set(logical_keys))
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COALESCE(MAX(serial_number), 0) FROM {final_table_name}")
        max_serial = int(cur.fetchone()[0] or 0)
        if not key_list:
            return {}, max_serial

        execute_values(
            cur,
            f"""
            SELECT target.stock_code, target.report_year, target.report_period, target.serial_number
            FROM {final_table_name} AS target
            INNER JOIN (VALUES %s) AS scope(stock_code, report_year, report_period)
                ON target.stock_code = scope.stock_code
               AND target.report_year = scope.report_year
               AND target.report_period = scope.report_period
            """,
            key_list,
            template="(%s, %s, %s)",
            page_size=200,
        )
        rows = cur.fetchall()
        serial_map = {(row[0], row[1], row[2]): int(row[3]) for row in rows if row[3] is not None}
        return serial_map, max_serial
    finally:
        cur.close()


def upsert_records(conn, final_table_name: str, insert_columns: List[str], records: List[Dict]) -> int:
    """按附件3标准字段写入最终表。"""
    if not records:
        return 0

    values = [tuple(record.get(column) for column in insert_columns) for record in records]
    update_columns = [column for column in insert_columns if column not in REQUIRED_KEY_COLUMNS]

    if update_columns:
        conflict_action = sql.SQL("DO UPDATE SET {updates}").format(
            updates=sql.SQL(", ").join(
                sql.SQL("{column} = EXCLUDED.{column}").format(column=sql.Identifier(column))
                for column in update_columns
            )
        )
    else:
        conflict_action = sql.SQL("DO NOTHING")

    query = sql.SQL(
        """
        INSERT INTO {table} ({columns})
        VALUES %s
        ON CONFLICT ({conflict_keys})
        {conflict_action}
        """
    ).format(
        table=sql.Identifier(final_table_name),
        columns=sql.SQL(", ").join(sql.Identifier(column) for column in insert_columns),
        conflict_keys=sql.SQL(", ").join(sql.Identifier(column) for column in REQUIRED_KEY_COLUMNS),
        conflict_action=conflict_action,
    )

    cur = conn.cursor()
    try:
        execute_values(cur, query.as_string(conn), values, page_size=100)
        conn.commit()
        return len(records)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def delete_final_rows_by_keys(conn, final_table_name: str, logical_keys: Iterable[Tuple[str, int, str]]) -> int:
    """按逻辑键删除最终表旧记录。"""
    key_list = sorted(set(logical_keys))
    if not key_list:
        return 0

    cur = conn.cursor()
    try:
        execute_values(
            cur,
            f"""
            DELETE FROM {final_table_name} AS target
            USING (VALUES %s) AS stale(stock_code, report_year, report_period)
            WHERE target.stock_code = stale.stock_code
              AND target.report_year = stale.report_year
              AND target.report_period = stale.report_period
            """,
            key_list,
            template="(%s, %s, %s)",
            page_size=200,
        )
        deleted_count = cur.rowcount
        conn.commit()
        return deleted_count
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def clear_final_table(conn, final_table_name: str) -> int:
    """全量重建时清空目标最终表，避免 serial_number 唯一约束冲突。"""
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {final_table_name}")
        deleted_count = cur.rowcount
        conn.commit()
        return deleted_count
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def resequence_serial_numbers(conn, final_table_name: str) -> int:
    """按最终表全量数据重新连续编号 serial_number。"""
    cur = conn.cursor()
    try:
        cur.execute(
            sql.SQL(
                """
                WITH ordered AS (
                    SELECT
                        stock_code,
                        report_year,
                        report_period,
                        ROW_NUMBER() OVER (
                            ORDER BY
                                stock_code,
                                report_year,
                                CASE report_period
                                    WHEN 'Q1' THEN 1
                                    WHEN 'HY' THEN 2
                                    WHEN 'Q3' THEN 3
                                    WHEN 'FY' THEN 4
                                    ELSE 999
                                END,
                                report_period
                        ) AS new_serial_number
                    FROM {table}
                )
                UPDATE {table} AS target
                SET serial_number = ordered.new_serial_number
                FROM ordered
                WHERE target.stock_code = ordered.stock_code
                  AND target.report_year = ordered.report_year
                  AND target.report_period = ordered.report_period
                """
            ).format(table=sql.Identifier(final_table_name))
        )
        updated_count = cur.rowcount
        conn.commit()
        return updated_count
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def process_target_table(
    conn,
    target_table: str,
    scope_file_ids: Optional[List[int]] = None,
    is_full_rebuild: bool = False,
) -> None:
    """处理单个中间目标表并写入对应最终表。"""
    final_table_name = SOURCE_TO_FINAL_TABLE.get(target_table)
    if not final_table_name:
        safe_print(f"[跳过] target_table={target_table} | 原因=未配置最终表映射")
        return

    field_defs = fetch_field_dict(conn, target_table)
    if not field_defs:
        safe_print(f"[跳过] target_table={target_table} | 原因=attachment3_field_dict 中无字段定义")
        return

    scope_meta = fetch_report_meta(conn, scope_file_ids) if scope_file_ids else {}
    scope_key_map = build_scope_key_map(scope_meta)
    if scope_file_ids:
        safe_print(f"[处理范围] target_table={target_table} | file_ids={','.join(str(file_id) for file_id in scope_file_ids)}")

    candidate_rows = fetch_extract_results(conn, target_table, file_ids=scope_file_ids)
    records, validation_logs, selection_logs, selected_keys = build_candidate_records(target_table, field_defs, candidate_rows)

    if scope_file_ids:
        stale_keys = set(scope_key_map.keys()) - selected_keys
        deleted_count = delete_final_rows_by_keys(conn, final_table_name, stale_keys)
        safe_print(
            f"[范围清理] target_table={target_table} | final_table={final_table_name} | "
            f"stale_keys={len(stale_keys)} | deleted_rows={deleted_count}"
        )
        for logical_key in sorted(stale_keys):
            file_id_text = ",".join(str(file_id) for file_id in sorted(scope_key_map.get(logical_key, set())))
            safe_print(
                f"[清理旧记录] target_table={target_table} | "
                f"stock_code={logical_key[0]} | report_year={logical_key[1]} | report_period={logical_key[2]} | "
                f"source_file_ids={file_id_text} | reason=当前范围内无候选结果"
            )

    if not records:
        safe_print(f"[跳过] target_table={target_table} | 原因=最终标准记录为空")
        return

    insert_columns, missing_optional, _actual_columns = build_insert_plan(conn, final_table_name, field_defs)
    effective_insert_columns = list(insert_columns)
    if is_full_rebuild:
        deleted_count = clear_final_table(conn, final_table_name)
        safe_print(f"[全量清空] final_table={final_table_name} | deleted_rows={deleted_count}")
    if not is_full_rebuild and "serial_number" in insert_columns:
        logical_keys = [
            (record["stock_code"], record["report_year"], record["report_period"])
            for record in records
        ]
        existing_serial_map, max_serial = fetch_existing_serial_numbers(conn, final_table_name, logical_keys)
        next_serial = max_serial
        for record in records:
            logical_key = (record["stock_code"], record["report_year"], record["report_period"])
            if logical_key in existing_serial_map:
                record["serial_number"] = existing_serial_map[logical_key]
            else:
                next_serial += 1
                record["serial_number"] = next_serial
        safe_print(
            f"[子集更新] final_table={final_table_name} | "
            f"existing_serials={len(existing_serial_map)} | start_new_serial={max_serial + 1}"
        )

    for index, record in enumerate(
        sorted(
            records,
            key=lambda item: (
                item["stock_code"],
                item["report_year"],
                PERIOD_ORDER.get(item["report_period"], 999),
                item["report_period"],
            ),
        ),
        start=1,
    ):
        if is_full_rebuild and "serial_number" in insert_columns:
            record["serial_number"] = index

    upsert_count = upsert_records(conn, final_table_name, effective_insert_columns, records)

    if is_full_rebuild and "serial_number" in insert_columns:
        resequenced_rows = resequence_serial_numbers(conn, final_table_name)
        safe_print(f"[重排序号] final_table={final_table_name} | resequence_rows={resequenced_rows}")
    else:
        safe_print(f"[跳过重排] final_table={final_table_name} | 原因=当前为子集更新或目标表无 serial_number 列")

    safe_print(
        f"[完成] source_target_table={target_table} | "
        f"final_table={final_table_name} | "
        f"source_rows={len(candidate_rows)} | "
        f"upsert_rows={upsert_count} | "
        f"validation_rejections={len(validation_logs)} | "
        f"missing_optional_columns={len(missing_optional)}"
    )
    for log_line in selection_logs:
        safe_print(f"[择优] {log_line}")
    for log_line in validation_logs:
        safe_print(f"[拦截] {log_line}")


def main() -> int:
    """主流程。"""
    configure_console()
    args = parse_args()
    target_tables = args.target_table or SUPPORTED_TARGET_TABLES
    failed_tables = []

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        scope_file_ids = resolve_scope_file_ids(conn, args.file_id, args.limit)
        is_full_rebuild = scope_file_ids is None

        for target_table in target_tables:
            try:
                process_target_table(
                    conn,
                    target_table,
                    scope_file_ids=scope_file_ids,
                    is_full_rebuild=is_full_rebuild,
                )
            except Exception as exc:
                conn.rollback()
                safe_print(f"[失败] target_table={target_table} | error={safe_text(exc)}")
                failed_tables.append(target_table)
    finally:
        conn.close()

    if failed_tables:
        safe_print(f"[加载总结] failed_target_tables={','.join(failed_tables)} | failed_count={len(failed_tables)}")
        return 1
    safe_print("[加载总结] failed_target_tables=0 | failed_count=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
