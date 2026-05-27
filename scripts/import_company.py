import argparse
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd
import psycopg2
import fitz
from psycopg2.extras import execute_values


DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DBNAME", "financial_reports"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_PATTERNS = [
    "示例数据/*中药上市公司基本信息*.xlsx",
    "input/attachment/*中药上市公司基本信息*.xlsx",
    "*中药上市公司基本信息*.xlsx",
]
REPORT_ROOT = PROJECT_ROOT / "input" / "reports"


def normalize_text(value) -> str:
    """标准化文本，避免空白差异影响匹配。"""
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return text.replace("\u3000", " ").replace("\xa0", " ").strip()


def normalize_stock_code(value) -> str:
    """将股票代码统一为 6 位字符串。"""
    text = normalize_text(value)
    if not text:
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    text = text.replace(" ", "")
    return text.zfill(6)


def find_col(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    """从候选列名中找到首个存在的列。"""
    columns = list(df.columns)
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def find_source_file(explicit_path: Optional[str]) -> Path:
    """自动定位公司主数据 Excel，避免依赖硬编码路径。"""
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"未找到指定公司主数据文件：{path}")
        return path

    for pattern in DEFAULT_SOURCE_PATTERNS:
        matches = sorted(PROJECT_ROOT.glob(pattern))
        if matches:
            return matches[0]

    raise FileNotFoundError("未找到公司主数据 Excel，请通过 --excel-path 显式指定。")


def extract_stock_code_from_filename(file_name: str) -> str:
    """从文件名中提取 6 位股票代码。"""
    match = re.search(r"(\d{6})", file_name)
    if not match:
        return ""
    return normalize_stock_code(match.group(1))


def extract_stock_abbr_from_filename(file_name: str) -> str:
    """从“公司简称：报告名.pdf”样式文件名中提取简称。"""
    stem = Path(file_name).stem.strip()
    for separator in ["：", ":", "_"]:
        if separator in stem:
            candidate = normalize_text(stem.split(separator, 1)[0])
            if candidate and not candidate.isdigit():
                return candidate
    return ""


def load_company_dataframe(excel_path: Path) -> pd.DataFrame:
    """读取并清洗公司主数据。"""
    df = pd.read_excel(excel_path)
    print("原始列名：", list(df.columns))

    stock_code_col = find_col(df, ["股票代码", "证券代码", "代码"])
    stock_abbr_col = find_col(df, ["A股简称", "股票简称", "证券简称", "简称"])
    company_name_col = find_col(df, ["公司名称", "公司全称", "上市公司名称"])

    print("识别到的列名：")
    print("股票代码列：", stock_code_col)
    print("股票简称列：", stock_abbr_col)
    print("公司名称列：", company_name_col)

    if not stock_code_col or not stock_abbr_col or not company_name_col:
        raise ValueError(
            "未找到关键列，请检查表头。"
            f"股票代码列={stock_code_col}, 股票简称列={stock_abbr_col}, 公司名称列={company_name_col}"
        )

    result = df[[stock_code_col, stock_abbr_col, company_name_col]].copy()
    result.columns = ["stock_code", "stock_abbr", "company_name"]
    result["stock_code"] = result["stock_code"].apply(normalize_stock_code)
    result["stock_abbr"] = result["stock_abbr"].apply(normalize_text)
    result["company_name"] = result["company_name"].apply(normalize_text)

    result = result[
        (result["stock_code"] != "")
        & (result["stock_abbr"] != "")
        & (result["company_name"] != "")
    ].copy()
    result = result.drop_duplicates(subset=["stock_code"], keep="first")

    print("清洗后数据预览：")
    print(result.head())
    print(f"清洗后公司数量：{len(result)}")
    return result


def normalize_company_name_candidate(text: str) -> str:
    """清洗 PDF 中识别到的公司全称候选。"""
    result = normalize_text(text)
    if not result:
        return ""
    return result.rstrip("：: ").strip()


def extract_company_info_from_pdf(pdf_path: Path) -> Optional[Dict[str, str]]:
    """从财报首页提取股票代码、简称和公司全称。"""
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return None

    try:
        text = "\n".join(doc[i].get_text("text") for i in range(min(20, len(doc))))
    finally:
        doc.close()

    normalized_text = text.replace("\u3000", " ").replace("\xa0", " ")
    stock_code = extract_stock_code_from_filename(pdf_path.name)
    if not stock_code:
        code_match = re.search(r"(?:公司代码|股票代码|证券代码)\s*[：: ]\s*(\d{6})", normalized_text)
        if code_match:
            stock_code = normalize_stock_code(code_match.group(1))

    abbr_match = re.search(r"(?:公司简称|股票简称|证券简称)\s*[：:]\s*([^\n\r ]+)", normalized_text)
    stock_abbr = normalize_text(abbr_match.group(1)) if abbr_match else ""
    if not stock_abbr:
        stock_abbr = extract_stock_abbr_from_filename(pdf_path.name)

    company_name = ""
    for line in normalized_text.splitlines():
        candidate = normalize_company_name_candidate(line)
        if not candidate:
            continue
        if any(keyword in candidate for keyword in ["年度报告", "半年度报告", "季度报告", "报告摘要", "公司代码", "公司简称"]):
            continue
        if candidate.endswith(("股份有限公司", "有限责任公司", "有限公司")):
            company_name = candidate
            break

    if not stock_code or not stock_abbr or not company_name:
        return None

    return {
        "stock_code": stock_code,
        "stock_abbr": stock_abbr,
        "company_name": company_name,
    }


def collect_company_rows_from_reports(existing_codes: set[str]) -> List[Dict[str, str]]:
    """从当前批次 PDF 中补齐公司维表缺失项。"""
    if not REPORT_ROOT.exists():
        return []

    found_rows: Dict[str, Dict[str, str]] = {}
    for pdf_path in sorted(REPORT_ROOT.rglob("*.pdf")):
        info = extract_company_info_from_pdf(pdf_path)
        if not info:
            continue
        stock_code = info["stock_code"]
        if not stock_code or stock_code in existing_codes or stock_code in found_rows:
            continue
        found_rows[stock_code] = info

    return list(found_rows.values())


def augment_company_dataframe(base_df: pd.DataFrame) -> pd.DataFrame:
    """用当前批次 PDF 补齐公司主数据。"""
    existing_codes = set(base_df["stock_code"].tolist())
    report_rows = collect_company_rows_from_reports(existing_codes)
    if not report_rows:
        print("未从 PDF 中补充到新的公司主数据。")
        return base_df

    report_df = pd.DataFrame(report_rows)
    merged_df = pd.concat([base_df, report_df], ignore_index=True)
    merged_df = merged_df.drop_duplicates(subset=["stock_code"], keep="first")
    print(f"从 PDF 中额外补充公司数量：{len(report_df)}")
    print(f"合并后公司数量：{len(merged_df)}")
    return merged_df


def build_alias_names(stock_code: str, stock_abbr: str, company_name: str) -> List[tuple[str, str, bool]]:
    """生成常用别名，提升扫描阶段命中率。"""
    alias_items: List[tuple[str, str, bool]] = []

    if stock_code:
        alias_items.append((stock_code, "stock_code", True))

    if stock_abbr:
        alias_items.append((stock_abbr, "stock_abbr", True))

    if company_name:
        alias_items.append((company_name, "company_name", True))

        # 常见公司全称后缀裁剪，便于文件名中只出现主体名称时命中。
        suffixes = [
            "股份有限公司",
            "集团股份有限公司",
            "有限责任公司",
            "股份公司",
            "有限公司",
        ]
        compact_name = company_name.replace("(", "（").replace(")", "）")
        for suffix in suffixes:
            if compact_name.endswith(suffix):
                short_name = compact_name[: -len(suffix)].strip()
                if short_name:
                    alias_items.append((short_name, "company_name_short", False))

    # 括号全半角互转，降低文件名格式差异影响。
    normalized_aliases = []
    for alias_name, alias_type, is_primary in alias_items:
        alias_name = normalize_text(alias_name)
        if not alias_name:
            continue
        normalized_aliases.append((alias_name, alias_type, is_primary))
        alt_alias = alias_name.replace("（", "(").replace("）", ")")
        if alt_alias != alias_name:
            normalized_aliases.append((alt_alias, alias_type, False))

    # 去重并保持稳定顺序。
    deduped: List[tuple[str, str, bool]] = []
    seen = set()
    for item in normalized_aliases:
        key = (item[0], item[1])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def import_company_data(df: pd.DataFrame) -> None:
    """将公司主数据与别名写入数据库。"""
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        company_rows = [
            (row.stock_code, row.stock_abbr, row.company_name)
            for row in df.itertuples(index=False)
        ]

        insert_company_sql = """
            INSERT INTO company_dim (stock_code, stock_abbr, company_name)
            VALUES %s
            ON CONFLICT (stock_code)
            DO UPDATE SET
                stock_abbr = EXCLUDED.stock_abbr,
                company_name = EXCLUDED.company_name,
                updated_at = CURRENT_TIMESTAMP
        """
        execute_values(cur, insert_company_sql, company_rows, page_size=200)

        cur.execute("SELECT company_id, stock_code FROM company_dim")
        code_to_company_id = {
            normalize_stock_code(stock_code): company_id
            for company_id, stock_code in cur.fetchall()
        }

        alias_rows = []
        for row in df.itertuples(index=False):
            company_id = code_to_company_id.get(row.stock_code)
            if not company_id:
                continue
            for alias_name, alias_type, is_primary in build_alias_names(
                row.stock_code,
                row.stock_abbr,
                row.company_name,
            ):
                alias_rows.append((company_id, alias_name, alias_type, is_primary))

        alias_rows = list(dict.fromkeys(alias_rows))
        insert_alias_sql = """
            INSERT INTO company_alias (company_id, alias_name, alias_type, is_primary)
            VALUES %s
            ON CONFLICT DO NOTHING
        """
        execute_values(cur, insert_alias_sql, alias_rows, page_size=500)

        conn.commit()
        print(f"成功导入 company_dim：{len(company_rows)} 条")
        print(f"成功导入 company_alias：{len(alias_rows)} 条")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="导入公司维表与别名表。")
    parser.add_argument(
        "--excel-path",
        help="公司主数据 Excel 路径；不传时自动在项目目录内查找。",
    )
    return parser.parse_args()


def main() -> None:
    """主入口。"""
    args = parse_args()
    excel_path = find_source_file(args.excel_path)
    print(f"使用公司主数据文件：{excel_path}")
    df = load_company_dataframe(excel_path)
    df = augment_company_dataframe(df)
    import_company_data(df)


if __name__ == "__main__":
    main()
