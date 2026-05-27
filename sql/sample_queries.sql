-- 查询单家公司单期利润表关键指标
SELECT
    company_name,
    stock_code,
    report_year,
    report_period,
    total_operating_revenue,
    net_profit
FROM income_sheet
WHERE stock_code = '000999'
  AND report_year = 2024
  AND report_period = 'FY';

-- 查询单家公司跨年营业收入趋势
SELECT
    report_year,
    report_period,
    total_operating_revenue
FROM income_sheet
WHERE stock_code = '000999'
  AND report_period = 'FY'
ORDER BY report_year;

-- 查询多家公司同一年度总资产对比
SELECT
    company_name,
    stock_code,
    asset_total_assets
FROM balance_sheet
WHERE report_year = 2024
  AND report_period = 'FY'
ORDER BY asset_total_assets DESC
LIMIT 10;
