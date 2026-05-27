# 数据处理流程

## 流程概览

1. `scan_reports.py` 扫描财报 PDF，生成报告索引。
2. `import_company.py` 导入公司基础信息与别名。
3. `import_attachment3_dict.py` 导入目标字段字典。
4. `locate_financial_statements.py` 定位三大财务报表页码。
5. `extract_statement_blocks.py` 抽取并标准化报表块。
6. `extract_attachment3_rule_based.py` 按字段规则抽取目标值。
7. `load_attachment3_results_to_sql.py` 将结果聚合写入标准表。

## 数据目录约定

完整 PDF 放在 `input/reports/`，字段说明表放在 `input/attachment/`，这些目录不进入 Git。公开仓库只保留 `sample_data/` 下的格式样例。

## 公开版范围

公开版本只保留规则抽取和候选补全流程，不包含额外补缺链路或模型调用缓存。
