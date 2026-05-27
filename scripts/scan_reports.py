import argparse
import os
import re
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import execute_values


DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DBNAME", "financial_reports"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = PROJECT_ROOT / "input" / "reports"
MAX_PAGES = 3
TITLE_LINE_LIMIT = 20
FALLBACK_LINE_LIMIT = 120

# 报告期统一枚举：
# 第一季度 -> Q1
# 半年度 -> HY
# 第三季度 -> Q3
# 年度 -> FY
REPORT_TYPE_RULES = [
    ("Q1", ["第一季度报告", "一季度报告", "第一季度报告全文", "一季度报告全文"]),
    ("HY", ["半年度报告", "半年度报告摘要", "半年度摘要", "中期报告", "中期报告摘要"]),
    ("Q3", ["第三季度报告", "三季度报告", "第三季度报告全文", "三季度报告全文"]),
    ("FY", ["年度报告", "年度报告摘要", "年度摘要"]),
]

REPORT_YEAR_PATTERN = re.compile(r"(20\d{2})\s*年")

# 来源识别规则按优先级匹配，便于后续继续扩展其他来源。
SOURCE_EXCHANGE_RULES = [
    ("SSE", ["上交所", "上海证券交易所", "sse", "sh_", "_sh", "-sh", "\\sh\\", "/sh/", "沪市", "沪主板"]),
    ("SZSE", ["深交所", "深圳证券交易所", "szse", "sz_", "_sz", "-sz", "\\sz\\", "/sz/", "深市", "创业板"]),
]

OPTIONAL_REPORT_FILE_INDEX_COLUMNS = {"source_exchange", "report_type_text"}


def normalize_stock_code(code: str):
    """将股票代码标准化为 6 位字符串。"""
    if not code:
        return None
    code = str(code).strip()
    if code.endswith(".0"):
        code = code[:-2]
    return code.zfill(6)


def extract_stock_code_from_filename(file_name: str):
    """优先从文件名中提取 6 位股票代码。"""
    match = re.search(r"\b(\d{6})\b", file_name)
    if match:
        return normalize_stock_code(match.group(1))

    match = re.search(r"(\d{6})", file_name)
    if match:
        return normalize_stock_code(match.group(1))

    return None


def clean_filename_stem(file_name: str):
    """返回不含扩展名的文件名。"""
    return Path(file_name).stem.strip()


def infer_is_summary_from_filename(file_name: str):
    """根据文件名判断是否为摘要。"""
    stem = clean_filename_stem(file_name)
    return "摘要" in stem


def infer_report_type_from_text_chunk(text: str):
    """从文本中识别报告期枚举和原始报告类型文本。"""
    if not text:
        return None, None

    compact_text = re.sub(r"\s+", "", text)
    for report_period, keywords in REPORT_TYPE_RULES:
        for keyword in keywords:
            if keyword in compact_text:
                return report_period, keyword

    return None, None


def infer_report_period_from_text_chunk(text: str) -> Optional[str]:
    """从一段文本中识别报告期。"""
    report_period, _report_type_text = infer_report_type_from_text_chunk(text)
    return report_period


def infer_report_type_text_from_text_chunk(text: str) -> Optional[str]:
    """从一段文本中提取报告类型原始识别文本。"""
    _report_period, report_type_text = infer_report_type_from_text_chunk(text)
    return report_type_text


def infer_report_period_from_filename(file_name: str) -> Optional[str]:
    """从文件名中识别报告期。"""
    return infer_report_period_from_text_chunk(clean_filename_stem(file_name))


def infer_report_type_text_from_filename(file_name: str) -> Optional[str]:
    """从文件名中提取报告类型原始识别文本。"""
    return infer_report_type_text_from_text_chunk(clean_filename_stem(file_name))


def infer_report_year_from_text_chunk(text: str, report_period: Optional[str]) -> Optional[int]:
    """从文本中识别报告年份。"""
    if not text:
        return None

    compact_text = re.sub(r"\s+", "", text)
    if report_period:
        for period_value, keywords in REPORT_TYPE_RULES:
            if period_value != report_period:
                continue
            for keyword in keywords:
                match = re.search(rf"(20\d{{2}})年{re.escape(keyword)}", compact_text)
                if match:
                    return int(match.group(1))

    match = REPORT_YEAR_PATTERN.search(compact_text)
    if match:
        return int(match.group(1))

    return None


def infer_report_year_from_filename(file_name: str, report_period: Optional[str]) -> Optional[int]:
    """从文件名中识别报告年份。"""
    return infer_report_year_from_text_chunk(clean_filename_stem(file_name), report_period)


def infer_source_exchange(file_path: str, file_name: str) -> str:
    """根据路径和文件名识别来源交易所。"""
    haystack = f"{file_path} {file_name}".lower().replace("\\", "/")
    for source_exchange, keywords in SOURCE_EXCHANGE_RULES:
        for keyword in keywords:
            normalized_keyword = keyword.lower().replace("\\", "/")
            if normalized_keyword in haystack:
                return source_exchange
    return "UNKNOWN"


def get_table_columns(conn, table_name: str, schema_name: str = "public"):
    """读取指定表的字段集合。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema_name, table_name),
    )
    rows = cur.fetchall()
    cur.close()
    return {row[0] for row in rows}


def warn_missing_optional_columns(table_columns):
    """提示当前库表缺少的可选字段。"""
    missing_columns = sorted(OPTIONAL_REPORT_FILE_INDEX_COLUMNS - table_columns)
    if missing_columns:
        print(
            "检测到 report_file_index 缺少可选字段："
            f"{', '.join(missing_columns)}。"
            "脚本将自动按现有字段兼容执行。"
        )


def build_report_file_row(match_result, file_name: str, file_path: str):
    """构造一条报告索引记录。"""
    report_period = infer_report_period_from_filename(file_name)
    report_type_text = infer_report_type_text_from_filename(file_name)
    report_year = infer_report_year_from_filename(file_name, report_period)
    source_exchange = infer_source_exchange(file_path, file_name)
    is_summary = infer_is_summary_from_filename(file_name)
    parse_status = "parsed" if report_period is not None and report_year is not None else "pending"

    return {
        "company_id": match_result["company_id"],
        "stock_code": normalize_stock_code(match_result["stock_code"]),
        "stock_abbr": match_result["stock_abbr"],
        "company_name": match_result["company_name"],
        "file_name": file_name,
        "file_path": file_path,
        "report_year": report_year,
        "report_period": report_period,
        "source_exchange": source_exchange,
        "report_type_text": report_type_text,
        "match_method": match_result["match_method"],
        "parse_status": parse_status,
        "is_summary": is_summary,
    }


def build_dynamic_insert_sql(table_columns, insert_columns):
    """按当前数据库字段生成插入 SQL。"""
    update_columns = [column for column in insert_columns if column != "file_path"]
    update_assignments = [f"{column} = EXCLUDED.{column}" for column in update_columns]
    if "updated_at" in table_columns:
        update_assignments.append("updated_at = CURRENT_TIMESTAMP")

    insert_columns_sql = ",\n            ".join(insert_columns)
    update_sql = ",\n            ".join(update_assignments)

    return f"""
        INSERT INTO report_file_index (
            {insert_columns_sql}
        )
        VALUES %s
        ON CONFLICT (file_path)
        DO UPDATE SET
            {update_sql}
    """


def get_alias_maps(conn):
    """读取公司及别名映射。"""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            d.company_id,
            d.stock_code,
            d.stock_abbr,
            d.company_name,
            a.alias_name,
            a.alias_type
        FROM company_dim d
        LEFT JOIN company_alias a
          ON d.company_id = a.company_id
        """
    )
    rows = cur.fetchall()
    cur.close()

    code_map = {}
    alias_map = {}

    for company_id, stock_code, stock_abbr, company_name, alias_name, _alias_type in rows:
        stock_code = normalize_stock_code(stock_code)
        info = {
            "company_id": company_id,
            "stock_code": stock_code,
            "stock_abbr": stock_abbr,
            "company_name": company_name,
        }

        if stock_code:
            code_map[stock_code] = info

        if alias_name:
            key = alias_name.strip().lower()
            alias_map.setdefault(key, []).append(info)

    return code_map, alias_map


def match_company_by_filename(file_name: str, code_map, alias_map):
    """根据文件名匹配公司。"""
    stock_code = extract_stock_code_from_filename(file_name)
    if stock_code and stock_code in code_map:
        info = code_map[stock_code]
        return {
            "company_id": info["company_id"],
            "stock_code": info["stock_code"],
            "stock_abbr": info["stock_abbr"],
            "company_name": info["company_name"],
            "match_method": "by_stock_code",
        }

    stem = clean_filename_stem(file_name).lower()
    alias_candidates = sorted(alias_map.keys(), key=len, reverse=True)

    for alias in alias_candidates:
        if alias and alias in stem:
            infos = alias_map[alias]
            if len(infos) == 1:
                info = infos[0]
                return {
                    "company_id": info["company_id"],
                    "stock_code": info["stock_code"],
                    "stock_abbr": info["stock_abbr"],
                    "company_name": info["company_name"],
                    "match_method": "by_filename_name",
                }

            return {
                "company_id": None,
                "stock_code": None,
                "stock_abbr": None,
                "company_name": None,
                "match_method": "ambiguous_alias",
            }

    return {
        "company_id": None,
        "stock_code": normalize_stock_code(stock_code),
        "stock_abbr": None,
        "company_name": None,
        "match_method": "unmatched",
    }


def scan_all_pdfs(report_root: Path):
    """递归扫描目录下全部 PDF 文件。"""
    return sorted(str(path.resolve()) for path in report_root.rglob("*.pdf"))


def scan_reports_into_db(conn, report_root: Path):
    """扫描 PDF 并写入 report_file_index。"""
    if not report_root.exists():
        raise FileNotFoundError(f"扫描目录不存在：{report_root}")

    table_columns = get_table_columns(conn, "report_file_index")
    warn_missing_optional_columns(table_columns)

    code_map, alias_map = get_alias_maps(conn)
    pdf_files = scan_all_pdfs(report_root)

    print(f"共扫描到 PDF 文件：{len(pdf_files)} 个")

    rows_to_insert = []
    for file_path in pdf_files:
        file_name = os.path.basename(file_path)
        match_result = match_company_by_filename(file_name, code_map, alias_map)
        row_data = build_report_file_row(match_result, file_name, file_path)
        rows_to_insert.append(row_data)

        print(
            f"[扫描] file={file_name} | "
            f"company={match_result['company_name']} | "
            f"code={row_data['stock_code']} | "
            f"source_exchange={row_data['source_exchange']} | "
            f"period={row_data['report_period']} | "
            f"report_type_text={row_data['report_type_text']} | "
            f"year={row_data['report_year']} | "
            f"summary={row_data['is_summary']} | "
            f"match={match_result['match_method']}"
        )

    if not rows_to_insert:
        print("未发现 PDF 文件。")
        return 0

    insert_columns = [
        "company_id",
        "stock_code",
        "stock_abbr",
        "company_name",
        "file_name",
        "file_path",
        "report_year",
        "report_period",
        "match_method",
        "parse_status",
        "is_summary",
    ]
    for optional_column in ("source_exchange", "report_type_text"):
        if optional_column in table_columns:
            insert_columns.insert(insert_columns.index("match_method"), optional_column)

    insert_rows = [tuple(row_data[column] for column in insert_columns) for row_data in rows_to_insert]

    cur = conn.cursor()
    insert_sql = build_dynamic_insert_sql(table_columns, insert_columns)
    execute_values(cur, insert_sql, insert_rows, page_size=200)
    cur.close()

    print(f"成功写入 report_file_index：{len(rows_to_insert)} 条")
    return len(rows_to_insert)


def extract_text_from_pdf(pdf_path: str, max_pages: int = 3) -> str:
    """提取 PDF 前几页文本。"""
    import fitz

    doc = fitz.open(pdf_path)
    texts = []
    try:
        page_count = min(len(doc), max_pages)
        for index in range(page_count):
            text = doc[index].get_text("text")
            if text:
                texts.append(text)
    finally:
        doc.close()

    return "\n".join(texts)


def normalize_text(text: str) -> str:
    """标准化文本空白字符。"""
    if not text:
        return ""
    text = text.replace("\u3000", " ")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def get_text_lines(text: str, max_lines: Optional[int] = None):
    """将文本按行拆分并去空白。"""
    lines = [line.strip() for line in normalize_text(text).splitlines() if line.strip()]
    if max_lines is None:
        return lines
    return lines[:max_lines]


def infer_report_period(text: str):
    """从正文中识别报告期。"""
    for line in get_text_lines(text, TITLE_LINE_LIMIT):
        report_period = infer_report_period_from_text_chunk(line)
        if report_period:
            return report_period

    for line in get_text_lines(text, FALLBACK_LINE_LIMIT):
        report_period = infer_report_period_from_text_chunk(line)
        if report_period:
            return report_period

    return infer_report_period_from_text_chunk(text)


def infer_report_type_text(text: str):
    """从正文中提取报告类型原始识别文本。"""
    for line in get_text_lines(text, TITLE_LINE_LIMIT):
        report_type_text = infer_report_type_text_from_text_chunk(line)
        if report_type_text:
            return report_type_text

    for line in get_text_lines(text, FALLBACK_LINE_LIMIT):
        report_type_text = infer_report_type_text_from_text_chunk(line)
        if report_type_text:
            return report_type_text

    return infer_report_type_text_from_text_chunk(text)


def infer_report_year(text: str, report_period: Optional[str]):
    """从正文中识别报告年份。"""
    for line in get_text_lines(text, TITLE_LINE_LIMIT):
        report_year = infer_report_year_from_text_chunk(line, report_period)
        if report_year is not None:
            return report_year

    for line in get_text_lines(text, FALLBACK_LINE_LIMIT):
        report_year = infer_report_year_from_text_chunk(line, report_period)
        if report_year is not None:
            return report_year

    return infer_report_year_from_text_chunk(text, report_period)


def detect_meta_from_pdf(pdf_path: str, file_name: str):
    """优先用文件名识别，不足时回退到 PDF 正文。"""
    report_period = infer_report_period_from_filename(file_name)
    report_type_text = infer_report_type_text_from_filename(file_name)
    report_year = infer_report_year_from_filename(file_name, report_period)
    source_exchange = infer_source_exchange(pdf_path, file_name)
    is_summary = infer_is_summary_from_filename(file_name)

    if report_period is not None and report_year is not None and report_type_text is not None:
        return {
            "report_year": report_year,
            "report_period": report_period,
            "report_type_text": report_type_text,
            "source_exchange": source_exchange,
            "is_summary": is_summary,
        }

    raw_text = extract_text_from_pdf(pdf_path, max_pages=MAX_PAGES)
    text = normalize_text(raw_text)

    if report_period is None:
        report_period = infer_report_period(text)
    if report_type_text is None:
        report_type_text = infer_report_type_text(text)
    if report_year is None:
        report_year = infer_report_year(text, report_period)

    return {
        "report_year": report_year,
        "report_period": report_period,
        "report_type_text": report_type_text,
        "source_exchange": source_exchange,
        "is_summary": is_summary,
    }


def inspect_report_meta(conn):
    """补全尚未识别完成的报告元数据。"""
    table_columns = get_table_columns(conn, "report_file_index")
    warn_missing_optional_columns(table_columns)

    select_columns = [
        "file_id",
        "file_name",
        "file_path",
        "report_year",
        "report_period",
        "is_summary",
        "parse_status",
    ]
    if "source_exchange" in table_columns:
        select_columns.append("source_exchange")
    if "report_type_text" in table_columns:
        select_columns.append("report_type_text")

    where_clauses = ["COALESCE(parse_status, 'pending') <> 'parsed'"]
    if "source_exchange" in table_columns:
        where_clauses.append("source_exchange IS NULL")
    if "report_type_text" in table_columns:
        where_clauses.append("report_type_text IS NULL")

    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT
            {", ".join(select_columns)}
        FROM report_file_index
        WHERE {" OR ".join(where_clauses)}
        ORDER BY file_id
        """
    )
    rows = cur.fetchall()

    print(f"待修正记录数：{len(rows)}")
    update_count = 0

    for row in rows:
        row_dict = dict(zip(select_columns, row))
        file_id = row_dict["file_id"]
        file_name = row_dict["file_name"]
        file_path = row_dict["file_path"]
        old_year = row_dict["report_year"]
        old_period = row_dict["report_period"]
        old_source_exchange = row_dict.get("source_exchange")
        old_report_type_text = row_dict.get("report_type_text")
        try:
            meta = detect_meta_from_pdf(file_path, file_name)
            new_year = meta["report_year"]
            new_period = meta["report_period"]
            new_source_exchange = meta["source_exchange"]
            new_report_type_text = meta["report_type_text"]
            new_summary = meta["is_summary"]

            effective_year = new_year if new_year is not None else old_year
            effective_period = new_period if new_period is not None else old_period
            effective_source_exchange = new_source_exchange or old_source_exchange or "UNKNOWN"
            effective_report_type_text = new_report_type_text or old_report_type_text
            parse_status = "parsed" if effective_year is not None and effective_period is not None else "pending"

            update_assignments = [
                "report_year = COALESCE(%s, report_year)",
                "report_period = COALESCE(%s, report_period)",
                "is_summary = COALESCE(%s, is_summary)",
                "parse_status = %s",
            ]
            update_params = [new_year, new_period, new_summary, parse_status]

            if "source_exchange" in table_columns:
                update_assignments.insert(2, "source_exchange = COALESCE(%s, source_exchange)")
                update_params.insert(2, effective_source_exchange)

            if "report_type_text" in table_columns:
                insert_at = 3 if "source_exchange" in table_columns else 2
                update_assignments.insert(insert_at, "report_type_text = COALESCE(%s, report_type_text)")
                update_params.insert(insert_at, effective_report_type_text)

            if "updated_at" in table_columns:
                update_assignments.append("updated_at = CURRENT_TIMESTAMP")

            update_params.append(file_id)
            cur.execute(
                f"""
                UPDATE report_file_index
                SET
                    {", ".join(update_assignments)}
                WHERE file_id = %s
                """,
                tuple(update_params),
            )

            update_count += 1
            print(
                f"[完成] file_id={file_id} | file={file_name} | "
                f"source_exchange={effective_source_exchange} | "
                f"year={new_year} | period={new_period} | "
                f"report_type_text={effective_report_type_text} | "
                f"summary={new_summary} | status={parse_status}"
            )
        except Exception as error:
            print(f"[失败] file_id={file_id} | file={file_name} | error={error}")

    cur.close()
    print(f"成功处理记录数：{update_count}")
    return update_count


def run(mode: str = "all", report_root: Optional[str] = None):
    """运行扫描和补全过程。"""
    resolved_report_root = Path(report_root).resolve() if report_root else REPORT_ROOT

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False

    try:
        if mode in ("scan", "all"):
            scan_reports_into_db(conn, resolved_report_root)

        if mode in ("inspect", "all"):
            inspect_report_meta(conn)

        conn.commit()
    except Exception as error:
        conn.rollback()
        print(f"执行失败，已回滚。错误信息：{error}")
        raise
    finally:
        conn.close()


def main():
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="扫描财报 PDF 并写入 report_file_index，可选补全报告年份和报告期。"
    )
    parser.add_argument(
        "--mode",
        choices=["scan", "inspect", "all"],
        default="all",
        help="scan=只扫描入库，inspect=只补全元数据，all=两步都执行",
    )
    parser.add_argument(
        "--report-root",
        default=str(REPORT_ROOT),
        help="PDF 扫描目录，默认是 input/reports",
    )
    args = parser.parse_args()
    run(mode=args.mode, report_root=args.report_root)


if __name__ == "__main__":
    main()
