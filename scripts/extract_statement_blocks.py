import argparse
import os
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2

from statement_table_schema import (
    FORMAT_VERSION,
    NormalizedTable,
    dump_normalized_table_json,
    load_normalized_table_json,
    normalize_text,
)
from table_geometry_recover import recover_table_from_pdf


DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DBNAME", "financial_reports"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_ROOT / "output" / "statement_json"
STALE_OUTPUT_DIR = PROJECT_ROOT / "output" / "statement_json_stale"
ALLOWED_STATEMENT_TYPES = ["balance_sheet", "income", "cash_flow"]
STATEMENT_TARGET_TABLE_MAP = {
    "balance_sheet": "balance_sheet",
    "income": "income",
    "cash_flow": "cash_flow",
}


def ensure_output_dir() -> None:
    """确保输出目录存在。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def resolve_pdf_path(file_path: str) -> Path:
    """将数据库中的路径解析为本地绝对路径。"""
    path = Path(file_path)
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def build_output_path(file_id: int, statement_type: str) -> Path:
    """生成 statement_json 输出路径。"""
    return OUTPUT_DIR / f"file_{file_id}_{statement_type}.json"


def quarantine_stale_output(output_path: Path, reason: str) -> None:
    """把已不再对应当前定位结果的旧中间 JSON 移出抽取目录。"""
    if not output_path.exists():
        return
    STALE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target_path = STALE_OUTPUT_DIR / f"{output_path.stem}__{reason}.json"
    counter = 1
    while target_path.exists():
        target_path = STALE_OUTPUT_DIR / f"{output_path.stem}__{reason}_{counter}.json"
        counter += 1
    output_path.replace(target_path)
    print(f"[清理旧JSON] source={output_path} | target={target_path} | reason={reason}")


def cleanup_unusable_locator_outputs(
    conn,
    file_ids: Optional[List[int]] = None,
    statement_types: Optional[List[str]] = None,
) -> None:
    """定位结果已变为 not_found 或无 page_start 时，避免旧 statement_json 继续被下游抽取。"""
    where_parts = ["(l.page_start IS NULL OR l.locator_status NOT IN ('success', 'weak_match'))"]
    params: List = []
    if file_ids:
        where_parts.append("l.file_id = ANY(%s)")
        params.append(file_ids)
    if statement_types:
        where_parts.append("l.statement_type = ANY(%s)")
        params.append(statement_types)
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT l.file_id, l.statement_type, COALESCE(l.locator_status, '')
        FROM report_statement_locator l
        WHERE {' AND '.join(where_parts)}
        """,
        params,
    )
    rows = cur.fetchall()
    cur.close()
    for file_id, statement_type, locator_status in rows:
        reason = normalize_text(locator_status) or "no_page_start"
        quarantine_stale_output(build_output_path(int(file_id), normalize_text(statement_type)), reason)


def locator_task_signature(task: Dict) -> Dict:
    """提取决定 statement_json 是否可复用的定位信息。"""
    page_start = task.get("page_start")
    page_end = task.get("page_end_guess") or page_start
    expected_pages = list(range(int(page_start), int(page_end) + 1)) if page_start is not None else []
    return {
        "statement_type": normalize_text(task.get("statement_type")),
        "pages": expected_pages,
        "locator_status": normalize_text(task.get("locator_status")),
        "locator_method": normalize_text(task.get("locator_method")),
        "title_text": normalize_text(task.get("title_text")),
        "header_text": normalize_text(task.get("header_text")),
        "is_consolidated": bool(task.get("is_consolidated")),
    }


def existing_statement_matches_task(output_path: Path, task: Dict) -> bool:
    """当定位页段或标题变化时，强制重建旧的 statement_json。"""
    try:
        existing = load_normalized_table_json(output_path)
    except Exception:
        return False

    expected = locator_task_signature(task)
    existing_pages = sorted(int(page) for page in (existing.pages or []) if page is not None)
    existing_meta = existing.parser_meta or {}
    return (
        normalize_text(existing.statement_type) == expected["statement_type"]
        and existing_pages == expected["pages"]
        and normalize_text(existing_meta.get("locator_status")) == expected["locator_status"]
        and normalize_text(existing_meta.get("locator_method")) == expected["locator_method"]
        and normalize_text(existing.title_text) == expected["title_text"]
        and normalize_text(existing.header_text) == expected["header_text"]
        and bool(existing.is_consolidated) == expected["is_consolidated"]
    )


def should_skip_existing(task: Dict, output_path: Path) -> bool:
    """若输出文件较新，则直接跳过。"""
    if not output_path.exists():
        return False
    pdf_path = resolve_pdf_path(task["file_path"])
    if not pdf_path.exists():
        return False
    try:
        if output_path.stat().st_mtime < pdf_path.stat().st_mtime:
            return False
        return existing_statement_matches_task(output_path, task)
    except OSError:
        return False


def fetch_statement_tasks(
    conn,
    file_ids: Optional[List[int]] = None,
    statement_types: Optional[List[str]] = None,
    limit: Optional[int] = None,
) -> List[Dict]:
    """读取待处理任务。"""
    cur = conn.cursor()
    where_parts = [
        "COALESCE(r.parse_status, 'pending') IN ('pending', 'parsed')",
        "l.page_start IS NOT NULL",
        "l.locator_status IN ('success', 'weak_match')",
    ]
    params: List = []

    if file_ids:
        where_parts.append("r.file_id = ANY(%s)")
        params.append(file_ids)

    if statement_types:
        where_parts.append("l.statement_type = ANY(%s)")
        params.append(statement_types)

    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(limit)

    cur.execute(
        f"""
        SELECT
            r.file_id,
            r.file_name,
            r.file_path,
            r.company_id,
            r.stock_code,
            r.stock_abbr,
            r.report_year,
            r.report_period,
            l.statement_type,
            l.page_start,
            COALESCE(l.end_page_guess, l.page_end, l.page_start),
            l.locator_status,
            l.locator_method,
            COALESCE(l.title_text, ''),
            COALESCE(l.header_text, ''),
            COALESCE(l.is_consolidated, FALSE),
            COALESCE(l.is_parent_only, FALSE),
            COALESCE(l.locator_confidence, 0),
            COALESCE(l.extra_info_json, '')
        FROM report_file_index r
        INNER JOIN report_statement_locator l
            ON r.file_id = l.file_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY r.file_id, l.statement_type
        {limit_sql}
        """,
        params,
    )
    rows = cur.fetchall()
    cur.close()

    tasks = []
    for row in rows:
        tasks.append(
            {
                "file_id": row[0],
                "file_name": row[1],
                "file_path": row[2],
                "company_id": row[3],
                "stock_code": row[4],
                "stock_abbr": row[5],
                "report_year": row[6],
                "report_period": row[7],
                "statement_type": row[8],
                "page_start": row[9],
                "page_end_guess": row[10],
                "locator_status": row[11],
                "locator_method": row[12],
                "title_text": row[13],
                "header_text": row[14],
                "is_consolidated": row[15],
                "is_parent_only": row[16],
                "locator_confidence": float(row[17] or 0.0),
                "extra_info_json": row[18],
            }
        )
    return tasks


def build_normalized_table(task: Dict, recovery_result) -> NormalizedTable:
    """将几何恢复结果映射到最终输出结构。"""
    parser_meta = dict(recovery_result.parser_meta)
    parser_meta.update(
        {
            "locator_status": task["locator_status"],
            "locator_method": task["locator_method"],
            "failure_reason": "" if recovery_result.rows else "no_rows_parsed",
        }
    )

    table = NormalizedTable(
        format_version=FORMAT_VERSION,
        file_id=task["file_id"],
        statement_type=task["statement_type"],
        title=normalize_text(task.get("title_text")) or (
            recovery_result.column_schema[0].raw_name if recovery_result.column_schema else task["statement_type"]
        ),
        pages=sorted(int(page_no) for page_no in recovery_result.page_text_map.keys()),
        unit=parser_meta.get("unit_detected", "元") or "元",
        currency=parser_meta.get("currency_detected", "人民币") or "人民币",
        is_consolidated=bool(task.get("is_consolidated")),
        column_schema=recovery_result.column_schema,
        rows=recovery_result.rows,
        parser_meta=parser_meta,
        target_table=STATEMENT_TARGET_TABLE_MAP[task["statement_type"]],
        report_year=task.get("report_year"),
        report_period=task.get("report_period"),
        stock_code=normalize_text(task.get("stock_code")),
        stock_abbr=normalize_text(task.get("stock_abbr")),
        company_id=task.get("company_id"),
        title_text=normalize_text(task.get("title_text")),
        header_text=normalize_text(task.get("header_text")),
        locator_confidence=float(task.get("locator_confidence") or 0.0),
        text=recovery_result.text,
        page_text_map=recovery_result.page_text_map,
    )
    return table


def save_statement_json(task: Dict, table: NormalizedTable) -> Path:
    """保存标准化表 JSON。"""
    output_path = build_output_path(task["file_id"], task["statement_type"])
    dump_normalized_table_json(table, output_path)
    return output_path


def process_single_task(task: Dict) -> Optional[Path]:
    """处理单个 file_id + statement_type。"""
    output_path = build_output_path(task["file_id"], task["statement_type"])
    if should_skip_existing(task, output_path):
        print(
            f"[跳过] file_id={task['file_id']} | statement_type={task['statement_type']} | "
            f"reason=statement_json 已存在且新于源 PDF | output={output_path}"
        )
        return output_path

    resolved_path = resolve_pdf_path(task["file_path"])
    if not resolved_path.exists():
        print(
            f"[失败] file_id={task['file_id']} | file_name={task['file_name']} | "
            f"statement_type={task['statement_type']} | error=PDF 文件不存在：{resolved_path}"
        )
        return None

    page_start = task.get("page_start")
    page_end = task.get("page_end_guess") or page_start
    page_numbers = list(range(int(page_start), int(page_end) + 1)) if page_start is not None else []

    try:
        recovery_result = recover_table_from_pdf(
            pdf_path=resolved_path,
            page_numbers=page_numbers,
            statement_type=task["statement_type"],
            title_text=task.get("title_text") or "",
            is_consolidated=bool(task.get("is_consolidated")),
        )
        table = build_normalized_table(task, recovery_result)
        output_path = save_statement_json(task, table)

        parser_meta = table.parser_meta
        print(
            f"[几何恢复] file_id={task['file_id']} | statement_type={task['statement_type']} | "
            f"pages={table.pages} | regions={len(parser_meta.get('detected_regions', []))} | "
            f"columns={[(item.col_id, item.role) for item in table.column_schema]} | "
            f"rows={len(table.rows)} | note_column={parser_meta.get('note_column_detected')} | "
            f"note_score={parser_meta.get('note_column_score')} | "
            f"repeated_headers_removed={parser_meta.get('repeated_headers_removed')} | "
            f"cross_page_merges={parser_meta.get('cross_page_merges')} | "
            f"ambiguous_parent_mix={parser_meta.get('ambiguous_parent_mix')} | "
            f"parse_confidence={parser_meta.get('parse_confidence')} | output={output_path}"
        )
        return output_path
    except Exception as error:
        print(
            f"[失败] file_id={task['file_id']} | file_name={task['file_name']} | "
            f"statement_type={task['statement_type']} | pages={page_numbers} | error={error}"
        )
        return None


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="按定位结果执行几何级表格恢复，并输出 normalized_table_v1 JSON。")
    parser.add_argument("--file-id", type=int, nargs="*", help="仅处理指定 file_id，可传多个。")
    parser.add_argument("--statement-type", choices=ALLOWED_STATEMENT_TYPES, nargs="*", help="仅处理指定报表类型。")
    parser.add_argument("--limit", type=int, help="限制处理任务数量。")
    return parser.parse_args()


def main() -> None:
    """主流程。"""
    args = parse_args()
    ensure_output_dir()

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        cleanup_unusable_locator_outputs(
            conn,
            file_ids=args.file_id,
            statement_types=args.statement_type,
        )
        tasks = fetch_statement_tasks(
            conn,
            file_ids=args.file_id,
            statement_types=args.statement_type,
            limit=args.limit,
        )
    finally:
        conn.close()

    print(f"待处理报表任务数：{len(tasks)}")
    success_count = 0
    fail_count = 0

    for task in tasks:
        output_path = process_single_task(task)
        if output_path is not None:
            success_count += 1
        else:
            fail_count += 1

    print(f"处理完成：成功 {success_count} 个，失败 {fail_count} 个。")


if __name__ == "__main__":
    main()
