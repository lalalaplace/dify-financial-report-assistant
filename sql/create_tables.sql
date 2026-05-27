CREATE TABLE IF NOT EXISTS attachment3_field_dict (
    field_id BIGSERIAL PRIMARY KEY,
    target_table VARCHAR(50) NOT NULL,   -- balance_sheet / income / cash_flow
    field_code VARCHAR(100) NOT NULL UNIQUE,
    field_name_cn VARCHAR(200) NOT NULL,
    data_type VARCHAR(50),
    field_desc TEXT,
    sort_order INTEGER
);
-- 说明：
-- 1. 本脚本用于补齐当前代码实际依赖的基础表结构，可重复执行。
-- 2. 已存在的表不会重建；缺失字段、缺失约束、缺失索引会自动补齐。
-- 3. 本脚本同时创建中间表、基础主表与最终标准表。


BEGIN;


-- ========================================
-- 1. 公司主数据
-- ========================================

CREATE TABLE IF NOT EXISTS company_dim (
    company_id BIGSERIAL PRIMARY KEY,
    stock_code VARCHAR(20) NOT NULL,
    stock_abbr VARCHAR(100) NOT NULL,
    company_name VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE company_dim
    ADD COLUMN IF NOT EXISTS stock_code VARCHAR(20),
    ADD COLUMN IF NOT EXISTS stock_abbr VARCHAR(100),
    ADD COLUMN IF NOT EXISTS company_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE UNIQUE INDEX IF NOT EXISTS company_dim_stock_code_key
    ON company_dim (stock_code);

CREATE INDEX IF NOT EXISTS idx_company_dim_stock_abbr
    ON company_dim (stock_abbr);

CREATE INDEX IF NOT EXISTS idx_company_dim_company_name
    ON company_dim (company_name);


CREATE TABLE IF NOT EXISTS company_alias (
    alias_id BIGSERIAL PRIMARY KEY,
    company_id BIGINT NOT NULL,
    alias_name VARCHAR(255) NOT NULL,
    alias_type VARCHAR(64) NOT NULL,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE company_alias
    ADD COLUMN IF NOT EXISTS company_id BIGINT,
    ADD COLUMN IF NOT EXISTS alias_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS alias_type VARCHAR(64),
    ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'company_alias_company_id_fkey'
    ) THEN
        ALTER TABLE company_alias
            ADD CONSTRAINT company_alias_company_id_fkey
            FOREIGN KEY (company_id)
            REFERENCES company_dim(company_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_company_alias_company_id
    ON company_alias (company_id);

CREATE INDEX IF NOT EXISTS idx_company_alias_alias_name
    ON company_alias (alias_name);

CREATE UNIQUE INDEX IF NOT EXISTS uk_company_alias_company_alias_type
    ON company_alias (company_id, alias_name, alias_type);


-- ========================================
-- 2. 财报文件索引
-- ========================================

CREATE TABLE IF NOT EXISTS report_file_index (
    file_id BIGSERIAL PRIMARY KEY,
    company_id BIGINT,
    stock_code VARCHAR(20),
    stock_abbr VARCHAR(100),
    company_name VARCHAR(255),
    file_name VARCHAR(255) NOT NULL,
    file_path TEXT NOT NULL,
    report_year INTEGER,
    report_period VARCHAR(32),
    source_exchange VARCHAR(32),
    report_type_text VARCHAR(255),
    match_method VARCHAR(64),
    parse_status VARCHAR(32),
    is_summary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE report_file_index
    ADD COLUMN IF NOT EXISTS company_id BIGINT,
    ADD COLUMN IF NOT EXISTS stock_code VARCHAR(20),
    ADD COLUMN IF NOT EXISTS stock_abbr VARCHAR(100),
    ADD COLUMN IF NOT EXISTS company_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS file_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS file_path TEXT,
    ADD COLUMN IF NOT EXISTS report_year INTEGER,
    ADD COLUMN IF NOT EXISTS report_period VARCHAR(32),
    ADD COLUMN IF NOT EXISTS source_exchange VARCHAR(32),
    ADD COLUMN IF NOT EXISTS report_type_text VARCHAR(255),
    ADD COLUMN IF NOT EXISTS match_method VARCHAR(64),
    ADD COLUMN IF NOT EXISTS parse_status VARCHAR(32),
    ADD COLUMN IF NOT EXISTS is_summary BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'report_file_index_company_id_fkey'
    ) THEN
        ALTER TABLE report_file_index
            ADD CONSTRAINT report_file_index_company_id_fkey
            FOREIGN KEY (company_id)
            REFERENCES company_dim(company_id);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS report_file_index_file_path_key
    ON report_file_index (file_path);

CREATE INDEX IF NOT EXISTS idx_report_file_index_company_id
    ON report_file_index (company_id);

CREATE INDEX IF NOT EXISTS idx_report_file_index_stock_code
    ON report_file_index (stock_code);

CREATE INDEX IF NOT EXISTS idx_report_file_index_parse_status
    ON report_file_index (parse_status);

CREATE INDEX IF NOT EXISTS idx_report_file_index_report_year_period
    ON report_file_index (report_year, report_period);


-- ========================================
-- 3. 三大报表定位表
-- ========================================

CREATE TABLE IF NOT EXISTS report_statement_locator (
    id BIGSERIAL PRIMARY KEY,
    file_id BIGINT NOT NULL,
    statement_type VARCHAR(32) NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    locator_method VARCHAR(64) NOT NULL,
    locator_status VARCHAR(32) NOT NULL,
    source_text TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE report_statement_locator
    ADD COLUMN IF NOT EXISTS file_id BIGINT,
    ADD COLUMN IF NOT EXISTS statement_type VARCHAR(32),
    ADD COLUMN IF NOT EXISTS page_start INTEGER,
    ADD COLUMN IF NOT EXISTS page_end INTEGER,
    ADD COLUMN IF NOT EXISTS locator_method VARCHAR(64),
    ADD COLUMN IF NOT EXISTS locator_status VARCHAR(32),
    ADD COLUMN IF NOT EXISTS source_text TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'report_statement_locator_file_id_fkey'
    ) THEN
        ALTER TABLE report_statement_locator
            ADD CONSTRAINT report_statement_locator_file_id_fkey
            FOREIGN KEY (file_id)
            REFERENCES report_file_index(file_id)
            ON DELETE CASCADE;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uk_report_statement_locator_file_statement
    ON report_statement_locator (file_id, statement_type);

CREATE INDEX IF NOT EXISTS idx_report_statement_locator_status
    ON report_statement_locator (locator_status);


-- ========================================
-- 4. 对附件3中间表补索引
-- ========================================
CREATE TABLE IF NOT EXISTS attachment3_extract_result (
    result_id BIGSERIAL PRIMARY KEY,
    file_id BIGINT NOT NULL REFERENCES report_file_index(file_id),
    company_id BIGINT,
    stock_code VARCHAR(20),
    stock_abbr VARCHAR(100),
    report_year INTEGER,
    report_period VARCHAR(20),
    target_table VARCHAR(50) NOT NULL,
    field_code VARCHAR(100),
    field_name_cn VARCHAR(200),
    value_text TEXT,
    source_page_range VARCHAR(50),
    source_text TEXT,
    extract_method VARCHAR(50),   -- rule / rule_candidate_fill / manual_backfill
    llm_status VARCHAR(50),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS attachment3_validation_result (
    validation_id BIGSERIAL PRIMARY KEY,
    file_id BIGINT NOT NULL REFERENCES report_file_index(file_id),
    target_table VARCHAR(50),
    validation_rule VARCHAR(200),
    validation_status VARCHAR(50),
    validation_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_attachment3_field_dict_target_table
    ON attachment3_field_dict (target_table);

CREATE INDEX IF NOT EXISTS idx_attachment3_extract_result_file_id
    ON attachment3_extract_result (file_id);

CREATE INDEX IF NOT EXISTS idx_attachment3_extract_result_target_table
    ON attachment3_extract_result (target_table);

CREATE INDEX IF NOT EXISTS idx_attachment3_extract_result_file_target_field
    ON attachment3_extract_result (file_id, target_table, field_code);

CREATE INDEX IF NOT EXISTS idx_attachment3_extract_result_method
    ON attachment3_extract_result (extract_method);

CREATE INDEX IF NOT EXISTS idx_attachment3_validation_result_file_id
    ON attachment3_validation_result (file_id);


COMMIT;
-- 作用：
-- 1. 创建附件3标准最终表：
--    - balance_sheet
--    - income_sheet
--    - cash_flow_sheet
-- 2. 本脚本严格使用最终交付结构，不再使用工程中间宽表结构。
-- 3. 本脚本不会创建 *_raw / *_num / *_method 等工程字段。
--
-- 使用前提：
-- 1. 已完成基础库表初始化。
-- 2. attachment3_field_dict 已按当前附件3字典导入。
-- 3. 如需保留旧数据，请先自行备份；本脚本会 DROP 原表。
--
-- 执行方式：
-- psql -d financial_reports -f sql/create_tables.sql

DROP TABLE IF EXISTS balance_sheet;
CREATE TABLE balance_sheet (
    serial_number INTEGER NOT NULL,
    stock_code VARCHAR(32) NOT NULL,
    stock_abbr VARCHAR(128),
    company_name VARCHAR(255),
    report_year INTEGER NOT NULL,
    report_period VARCHAR(8) NOT NULL,
    asset_cash_and_cash_equivalents DECIMAL(20,2),
    asset_accounts_receivable DECIMAL(20,2),
    asset_inventory DECIMAL(20,2),
    asset_trading_financial_assets DECIMAL(20,2),
    asset_construction_in_progress DECIMAL(20,2),
    asset_total_assets DECIMAL(20,2),
    asset_total_assets_yoy_growth DECIMAL(10,4),
    liability_accounts_payable DECIMAL(20,2),
    liability_advance_from_customers DECIMAL(20,2),
    liability_total_liabilities DECIMAL(20,2),
    liability_total_liabilities_yoy_growth DECIMAL(10,4),
    liability_contract_liabilities DECIMAL(20,2),
    liability_short_term_loans DECIMAL(20,2),
    asset_liability_ratio DECIMAL(10,4),
    equity_unappropriated_profit DECIMAL(20,2),
    equity_total_equity DECIMAL(20,2),
    CONSTRAINT balance_sheet_pk PRIMARY KEY (stock_code, report_year, report_period)
);
CREATE UNIQUE INDEX balance_sheet_serial_number_uq ON balance_sheet (serial_number);
CREATE INDEX balance_sheet_year_period_idx ON balance_sheet (report_year, report_period);
ALTER TABLE balance_sheet ADD CONSTRAINT balance_sheet_report_period_chk CHECK (report_period IN ('Q1', 'HY', 'Q3', 'FY'));


DROP TABLE IF EXISTS income_sheet;
CREATE TABLE income_sheet (
    serial_number INTEGER NOT NULL,
    stock_code VARCHAR(32) NOT NULL,
    stock_abbr VARCHAR(128),
    company_name VARCHAR(255),
    report_year INTEGER NOT NULL,
    report_period VARCHAR(8) NOT NULL,
    net_profit DECIMAL(20,2),
    net_profit_yoy_growth DECIMAL(10,4),
    other_income DECIMAL(20,2),
    total_operating_revenue DECIMAL(20,2),
    operating_revenue_yoy_growth DECIMAL(10,4),
    operating_expense_cost_of_sales DECIMAL(20,2),
    operating_expense_selling_expenses DECIMAL(20,2),
    operating_expense_administrative_expenses DECIMAL(20,2),
    operating_expense_financial_expenses DECIMAL(20,2),
    operating_expense_rnd_expenses DECIMAL(20,2),
    operating_expense_taxes_and_surcharges DECIMAL(20,2),
    total_operating_expenses DECIMAL(20,2),
    operating_profit DECIMAL(20,2),
    total_profit DECIMAL(20,2),
    asset_impairment_loss DECIMAL(20,2),
    credit_impairment_loss DECIMAL(20,2),
    CONSTRAINT income_sheet_pk PRIMARY KEY (stock_code, report_year, report_period)
);
CREATE UNIQUE INDEX income_sheet_serial_number_uq ON income_sheet (serial_number);
CREATE INDEX income_sheet_year_period_idx ON income_sheet (report_year, report_period);
ALTER TABLE income_sheet ADD CONSTRAINT income_sheet_report_period_chk CHECK (report_period IN ('Q1', 'HY', 'Q3', 'FY'));


DROP TABLE IF EXISTS cash_flow_sheet;
CREATE TABLE cash_flow_sheet (
    serial_number INTEGER NOT NULL,
    stock_code VARCHAR(32) NOT NULL,
    stock_abbr VARCHAR(128),
    company_name VARCHAR(255),
    report_year INTEGER NOT NULL,
    report_period VARCHAR(8) NOT NULL,
    net_cash_flow DECIMAL(20,2),
    net_cash_flow_yoy_growth DECIMAL(10,4),
    operating_cf_net_amount DECIMAL(20,2),
    operating_cf_ratio_of_net_cf DECIMAL(10,4),
    operating_cf_cash_from_sales DECIMAL(20,2),
    investing_cf_net_amount DECIMAL(20,2),
    investing_cf_ratio_of_net_cf DECIMAL(10,4),
    investing_cf_cash_for_investments DECIMAL(20,2),
    investing_cf_cash_from_investment_recovery DECIMAL(20,2),
    financing_cf_cash_from_borrowing DECIMAL(20,2),
    financing_cf_cash_for_debt_repayment DECIMAL(20,2),
    financing_cf_net_amount DECIMAL(20,2),
    financing_cf_ratio_of_net_cf DECIMAL(10,4),
    CONSTRAINT cash_flow_sheet_pk PRIMARY KEY (stock_code, report_year, report_period)
);
CREATE UNIQUE INDEX cash_flow_sheet_serial_number_uq ON cash_flow_sheet (serial_number);
CREATE INDEX cash_flow_sheet_year_period_idx ON cash_flow_sheet (report_year, report_period);
ALTER TABLE cash_flow_sheet ADD CONSTRAINT cash_flow_sheet_report_period_chk CHECK (report_period IN ('Q1', 'HY', 'Q3', 'FY'));
BEGIN;

ALTER TABLE IF EXISTS public.balance_sheet
    ADD COLUMN IF NOT EXISTS company_name VARCHAR(255);

ALTER TABLE IF EXISTS public.income_sheet
    ADD COLUMN IF NOT EXISTS company_name VARCHAR(255);

ALTER TABLE IF EXISTS public.cash_flow_sheet
    ADD COLUMN IF NOT EXISTS company_name VARCHAR(255);

COMMIT;


