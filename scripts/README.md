# 脚本说明

## 主流程

1. `scan_reports.py`：扫描 PDF 并建立财报索引。
2. `import_company.py`：导入公司基础信息。
3. `import_attachment3_dict.py`：导入目标字段字典。
4. `locate_financial_statements.py`：定位三大财务报表。
5. `extract_statement_blocks.py`：抽取并标准化报表块。
6. `extract_attachment3_rule_based.py`：规则化抽取目标字段。
7. `load_attachment3_results_to_sql.py`：写入 PostgreSQL 标准表。
