from pathlib import Path
import os

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

# ========= 配置说明 =========
# 1. 修改 EXCEL_PATH：
#    指向“附件3：数据库-表名及字段说明.xlsx”的实际路径。
#    默认使用当前项目下的 input/attachment/附件3：数据库-表名及字段说明.xlsx。
#
# 2. 修改环境变量：
#    通过 PG_HOST、PG_PORT、PG_DBNAME、PG_USER、PG_PASSWORD 配置数据库连接。
#    本脚本只负责导入 attachment3_field_dict，不负责建表。
#
# 3. 运行方式：
#    python scripts/import_attachment3_dict.py

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXCEL_PATH = PROJECT_ROOT / "input" / "attachment" / "附件3：数据库-表名及字段说明.xlsx"

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DBNAME", "financial_reports"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

SHEET_TABLE_MAPPING = {
    "资产负债表": "balance_sheet",
    "利润表": "income",
    "现金流量表": "cash_flow",
}


def normalize_text(value):
    """将单元格值规范化为去空白后的字符串。"""
    if pd.isna(value):
        return None

    text = str(value).replace("\u00a0", " ").strip()
    if not text:
        return None
    return text


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """清洗列名中的空格和不可见字符。"""
    renamed = {}
    for col in df.columns:
        clean_col = str(col).replace("\u00a0", " ").strip()
        renamed[col] = clean_col
    return df.rename(columns=renamed)


def load_sheet_records(excel_path: Path, sheet_name: str, target_table: str):
    """读取单个 sheet 并转换为待写入数据库的记录。"""
    df = pd.read_excel(excel_path, sheet_name=sheet_name)
    df = normalize_columns(df)

    required_columns = ["字段名称", "中文名称", "字段类型", "字段说明"]
    missing_columns = [col for col in required_columns if col not in df.columns]
    if missing_columns:
        raise ValueError(
            f"sheet【{sheet_name}】缺少必要列：{', '.join(missing_columns)}；当前列为：{list(df.columns)}"
        )

    records = []
    for row in df.itertuples(index=False):
        row_dict = row._asdict()
        field_name = normalize_text(row_dict.get("字段名称"))
        field_name_cn = normalize_text(row_dict.get("中文名称"))
        data_type = normalize_text(row_dict.get("字段类型"))
        field_desc = normalize_text(row_dict.get("字段说明"))

        if not field_name or not field_name_cn:
            continue

        # 数据库中 field_code 是全局唯一，因此这里使用“目标表名.字段名”避免不同表之间相互覆盖。
        field_code = f"{target_table}.{field_name}"

        records.append(
            (
                target_table,
                field_code,
                field_name_cn,
                data_type,
                field_desc,
                len(records) + 1,
            )
        )

    return records


def load_all_records(excel_path: Path):
    """按映射读取四个 sheet 的全部字段记录。"""
    all_records = []
    sheet_counts = {}

    for sheet_name, target_table in SHEET_TABLE_MAPPING.items():
        records = load_sheet_records(excel_path, sheet_name, target_table)
        all_records.extend(records)
        sheet_counts[sheet_name] = len(records)

    return all_records, sheet_counts


def upsert_records(records):
    """将字段字典批量写入 attachment3_field_dict。"""
    if not records:
        print("没有可导入的数据。")
        return 0

    sql = """
        INSERT INTO attachment3_field_dict
        (
            target_table,
            field_code,
            field_name_cn,
            data_type,
            field_desc,
            sort_order
        )
        VALUES %s
        ON CONFLICT (field_code)
        DO UPDATE SET
            target_table = EXCLUDED.target_table,
            field_name_cn = EXCLUDED.field_name_cn,
            data_type = EXCLUDED.data_type,
            field_desc = EXCLUDED.field_desc,
            sort_order = EXCLUDED.sort_order
    """

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    try:
        execute_values(cur, sql, records, page_size=200)
        conn.commit()
        return len(records)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def main():
    excel_path = Path(EXCEL_PATH)
    if not excel_path.exists():
        raise FileNotFoundError(f"Excel 文件不存在：{excel_path}")

    print(f"开始读取 Excel：{excel_path}")
    records, sheet_counts = load_all_records(excel_path)

    print("各 sheet 读取结果：")
    for sheet_name, count in sheet_counts.items():
        print(f"- {sheet_name}：{count} 条")

    imported_count = upsert_records(records)
    print(f"导入完成，共写入或更新 {imported_count} 条字段字典记录。")


if __name__ == "__main__":
    main()
