import argparse
import csv
import faulthandler
import json
import math
import os
import re
import shutil
import subprocess
from decimal import Decimal, InvalidOperation
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_values

from attachment3_targeted_field_rules import (
    get_targeted_fragment_blacklist,
    get_targeted_precise_aliases,
    LONGTAIL_CLEANUP_FIELD_CODES,
    TARGETED_FIELD_CODES,
    TARGETED_EMPTY_VALUE_FIELD_CODES,
    build_targeted_semantic_rule_overrides,
    get_targeted_field_min_score,
    merge_strict_candidate_fill_rules,
    should_prioritize_raw_line_numeric as should_prioritize_raw_line_numeric_for_field,
    should_reject_targeted_row,
)
from statement_table_schema import ColumnSchema, NormalizedTable, compact_text, load_normalized_table_json, normalize_item_name, normalize_text

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DBNAME", "financial_reports"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATEMENT_JSON_DIR = PROJECT_ROOT / "output" / "statement_json"
RUN_HISTORY_PATH = PROJECT_ROOT / "output" / "run_history.csv"
RUN_SUMMARY_DIR = PROJECT_ROOT / "output" / "logs"
TARGETED_FAILURE_DEBUG_DIR = RUN_SUMMARY_DIR / "targeted_failures"
RAW_BLOCK_JSON_DIR = PROJECT_ROOT / "output" / "json"
EXTRACT_WATCHDOG_ENV = "ATTACHMENT3_EXTRACT_WATCHDOG_SECONDS"
RULE_METHOD = "rule"
RULE_CANDIDATE_FILL_METHOD = "rule_candidate_fill"
DELETE_METHODS = [RULE_METHOD, RULE_CANDIDATE_FILL_METHOD]
AUTO_FILL_MIN_CONFIDENCE = 0.68
AUTO_FILL_MIN_GAP = 0.12
STRONG_RULE_MIN_CONFIDENCE = 0.82
STRONG_RULE_MIN_GAP = 0.06
MAX_ROW_MATCHES = 5
ROW_MATCH_MIN_SCORE = {"balance_sheet": 58.0, "income": 58.0, "cash_flow": 68.0}
EMPTY_VALUE_TOKENS = {"", "-", "--", "—", "——", "/", "不适用", "n/a", "na"}
FAILURE_REASON_PRIORITY = ["only_non_target_column_role", "empty_value", "invalid_after_validation", "weak_match", "not_found"]
ROW_PREFIX_NOISE_PATTERN = re.compile(r"^(?:第?\d+\s*[\\/|]\s*|[（(]?\d+[)）./、]\s*|[一二三四五六七八九十]+\s*[、.．]\s*)")
ROW_SUFFIX_NOTE_PATTERN = re.compile(
    r"(?:附注|注)\s*[一二三四五六七八九十\d]+(?:\s*[、,，.．-]\s*\d+)?$|[一二三四五六七八九十]+\s*[、,，.．]\s*\d+$"
)
ROW_PAGE_FRAGMENT_PATTERN = re.compile(r"^\d+\s*/\s*")
TITLE_GLUE_SPLIT_PATTERN = re.compile(r"(?:[一二三四五六七八九十]+[、.．])")
TRAILING_NUMERIC_NOISE_PATTERN = re.compile(r"(?:[-－—–]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?)+\s*$")
ROW_TRAILING_EMPTY_MARK_PATTERN = re.compile(r"[-－—–]+\s*$")

ROW_NOTE_TOKEN_PATTERN = re.compile(r"^(?:[一二三四五六七八九十]+[、.．]?\s*\d+|\d+)$")
ROW_NUMERIC_TOKEN_PATTERN = re.compile(r"[-－—–]?\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?%?")
ROW_TRAILING_NOTE_AND_VALUES_PATTERN = re.compile(
    r"(?:\s+[一二三四五六七八九十]+[、.．]?\s*\d+)?(?:\s+[-－—–]?\(?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?%?){1,2}\s*$"
)
TRAILING_NUMERIC_NOISE_END_CHARS = set("0123456789%")
ROW_CONTEXT_FRAGMENT_LABELS = {
    "收到的现金",
    "支付的现金",
    "的现金",
    "付的现金",
    "现金",
    "金",
    "收益",
    "额",
    "其他",
    "量净额",
    "净额",
    "填列",
    "号填列",
    "列",
    "净亏损以",
    "亏损总额以",
}


def strip_trailing_numeric_noise(text: str) -> str:
    """仅在尾部确实像数值时才执行数值尾噪清理，避免长数字串正则回溯卡死。"""
    stripped = text.rstrip()
    if not stripped or stripped[-1] not in TRAILING_NUMERIC_NOISE_END_CHARS:
        return text
    return TRAILING_NUMERIC_NOISE_PATTERN.sub("", text).strip()
ROW_MERGE_FRAGMENT_SUFFIXES = ("支", "收", "现", "流", "量", "额", "益", "其", "的", "债", "借", "填")

PRIMARY_STATEMENT_TITLE_MARKERS = {
    "balance_sheet": ["资产负债表", "合并资产负债表", "母公司资产负债表"],
    "income": ["利润表", "合并利润表", "母公司利润表"],
    "cash_flow": ["现金流量表", "合并现金流量表", "母公司现金流量表"],
}
NON_PRIMARY_STATEMENT_TITLE_TOKENS = ["分析表", "变动分析", "主要经营情况", "主营业务分析"]
NON_PRIMARY_STATEMENT_TITLE_TOKENS.extend(["项目大幅变动", "情况与原因", "原因说明", "股东信息", "现金流量表项目", "利润表项目"])

COLUMN_ROLE_POLICY = {
    "balance_sheet": {"primary": "current_period", "allow_previous_fallback": True},
    "income": {"primary": "current_period", "allow_previous_fallback": True},
    "cash_flow": {"primary": "current_period", "allow_previous_fallback": False},
}

NON_FINANCIAL_FIELD_CODES = {"report_year", "report_period", "stock_code", "stock_abbr", "file_id", "company_id", "serial_number"}
METADATA_FIELD_NAMES = {"stock_code", "stock_abbr", "serial_number", "report_year", "report_period", "file_id", "company_id"}
RAW_BLOCK_TEXT_CACHE: Dict[int, List[Dict]] = {}

CANDIDATE_FILL_WHITELIST = {
    "balance_sheet": {
        "balance_sheet.asset_cash_and_cash_equivalents",
        "balance_sheet.asset_accounts_receivable",
        "balance_sheet.asset_inventory",
        "balance_sheet.asset_trading_financial_assets",
        "balance_sheet.asset_construction_in_progress",
        "balance_sheet.asset_total_assets",
        "balance_sheet.liability_accounts_payable",
        "balance_sheet.liability_contract_liabilities",
        "balance_sheet.liability_short_term_loans",
        "balance_sheet.liability_total_liabilities",
        "balance_sheet.equity_unappropriated_profit",
        "balance_sheet.equity_total_equity",
    },
    "income": {
        "income.net_profit",
        "income.other_income",
        "income.total_operating_revenue",
        "income.operating_expense_cost_of_sales",
        "income.operating_expense_selling_expenses",
        "income.operating_expense_administrative_expenses",
        "income.operating_expense_financial_expenses",
        "income.operating_expense_rnd_expenses",
        "income.operating_expense_taxes_and_surcharges",
        "income.total_operating_expenses",
        "income.operating_profit",
        "income.total_profit",
        "income.asset_impairment_loss",
        "income.credit_impairment_loss",
    },
    "cash_flow": {
        "cash_flow.net_cash_flow",
        "cash_flow.operating_cf_net_amount",
        "cash_flow.operating_cf_cash_from_sales",
        "cash_flow.investing_cf_net_amount",
        "cash_flow.investing_cf_cash_for_investments",
        "cash_flow.investing_cf_cash_from_investment_recovery",
        "cash_flow.financing_cf_cash_from_borrowing",
        "cash_flow.financing_cf_cash_for_debt_repayment",
        "cash_flow.financing_cf_net_amount",
    },
}

STRICT_CANDIDATE_FILL_RULES = {
    "balance_sheet.asset_total_assets": {"allow_exact": ["资产总计", "资产合计", "资产总额", "负债和所有者权益总计", "负债及所有者权益总计", "负债和股东权益总计", "负债及股东权益总计"], "deny_contains": ["流动资产合计", "非流动资产合计"]},
    "balance_sheet.liability_total_liabilities": {"allow_exact": ["负债合计", "负债总计", "负债总额"], "deny_contains": ["流动负债合计", "非流动负债合计", "负债和所有者权益", "负债及所有者权益", "负债和股东权益", "负债及股东权益"]},
    "balance_sheet.liability_contract_liabilities": {"allow_exact": ["合同负债"], "deny_contains": ["负债合计", "流动负债合计"]},
    "balance_sheet.liability_accounts_payable": {"allow_exact": ["应付账款"], "deny_contains": ["其他应付款", "应付票据", "负债合计"]},
    "balance_sheet.equity_total_equity": {"allow_exact": ["所有者权益合计", "股东权益合计", "所有者权益总计", "股东权益总计", "所有者权益总额", "股东权益总额", "所有者权益或股东权益合计"], "deny_contains": ["归属于母公司", "少数股东权益"]},
    "income.total_operating_revenue": {"allow_exact": ["营业收入", "营业总收入"], "deny_contains": ["营业外收入", "其他收益", "分行业", "分产品", "分地区", "变动原因说明"]},
    "income.operating_expense_cost_of_sales": {"allow_exact": ["营业成本"], "deny_contains": ["营业总成本", "营业总支出"]},
    "income.total_operating_expenses": {"allow_exact": ["营业总成本", "营业总费用"], "deny_contains": ["营业成本", "营业外支出", "其他收益"]},
    "cash_flow.operating_cf_cash_from_sales": {"allow_exact": ["销售商品、提供劳务收到的现金", "销售商品提供劳务收到的现金", "销售商品收到的现金"], "deny_contains": ["收到其他与经营活动有关的现金", "经营活动现金流入小计"]},
    "cash_flow.net_cash_flow": {"allow_exact": ["现金及现金等价物净增加额"], "deny_contains": ["经营活动产生的现金流量净额", "投资活动产生的现金流量净额", "筹资活动产生的现金流量净额"]},
}

HIGH_FREQ_FIELD_ALIASES = {
    "balance_sheet.asset_trading_financial_assets": ["交易性金融资产净额"],
    "balance_sheet.asset_construction_in_progress": ["在建工程净额"],
    "balance_sheet.asset_total_assets": ["资产总额", "资产总额合计", "负债和所有者权益总计", "负债及所有者权益总计", "负债和股东权益总计", "负债及股东权益总计"],
    "balance_sheet.liability_short_term_loans": ["短期借款余额"],
    "balance_sheet.equity_unappropriated_profit": ["未分配利润累计亏损", "未分配利润余额"],
    "balance_sheet.liability_total_liabilities": ["负债总额"],
    "balance_sheet.equity_total_equity": ["所有者权益", "股东权益", "所有者权益总计", "股东权益总计", "所有者权益总额", "股东权益总额", "所有者权益或股东权益合计", "所有者权益或股东权益总计"],
    "income.total_operating_expenses": ["营业总成本", "营业总支出"],
    "income.total_operating_revenue": ["营业总收入", "营业收入净额", "营业总收入营业收入"],
    "income.net_profit": ["归属于母公司股东的净利润", "归属于上市公司股东的净利润", "归属于母公司股东净利润"],
    "income.operating_profit": ["营业利润亏损填列"],
    "income.total_profit": ["利润总额亏损总额填列"],
    "income.asset_impairment_loss": ["资产减值损失损失填列"],
    "income.credit_impairment_loss": ["信用减值损失损失填列"],
    "income.other_income": ["其他收益金额"],
    "cash_flow.investing_cf_cash_for_investments": ["投资支付现金"],
    "cash_flow.financing_cf_cash_from_borrowing": ["取得借款收到现金"],
    "cash_flow.financing_cf_cash_for_debt_repayment": ["偿还债务支付现金"],
    "cash_flow.operating_cf_net_amount": ["经营活动现金流量净额"],
    "cash_flow.operating_cf_cash_from_sales": ["销售商品、提供劳务收到的现金", "销售商品提供劳务收到的现金", "销售商品收到的现金"],
    "cash_flow.net_cash_flow": ["现金及现金等价物净增额"],
}

DIAGNOSTIC_PREVIEW_LIMIT = 3
SOURCE_ROW_ABSENT_REASON = "source_row_absent"
CASH_FLOW_SALES_ROW_ABSENT_MARKERS = [
    "收到的税费返还",
    "收到其他与经营活动有关的现金",
    "经营活动现金流入小计",
    "购买商品、接受劳务支付的现金",
]
CASH_FLOW_SALES_ROW_FINANCIAL_MARKERS = [
    "收到原保险合同保费取得的现金",
    "收到再保业务现金净额",
    "保户储金及投资款净增加额",
    "收取利息、手续费及佣金的现金",
    "拆入资金净增加额",
    "回购业务资金净增加额",
    "代理买卖证券收到的现金净额",
]
HIGH_RISK_FIELD_CODES = {
    "balance_sheet.asset_total_assets",
    "balance_sheet.liability_total_liabilities",
    "balance_sheet.liability_contract_liabilities",
    "balance_sheet.liability_accounts_payable",
    "income.total_operating_revenue",
    "income.total_operating_expenses",
    "cash_flow.net_cash_flow",
}
TARGETED_GUARDRAIL_FIELD_CODES = set(TARGETED_FIELD_CODES) | {"income.total_profit"}
CANDIDATE_FILL_STRICT_FIELD_CODES = HIGH_RISK_FIELD_CODES | TARGETED_GUARDRAIL_FIELD_CODES
FIELD_ROW_MATCH_MIN_SCORE = {
    "balance_sheet.liability_short_term_loans": 52.0,
    "balance_sheet.equity_total_equity": 50.0,
    "income.other_income": 50.0,
    "income.operating_expense_cost_of_sales": 52.0,
    "income.total_profit": 52.0,
    "cash_flow.operating_cf_cash_from_sales": 60.0,
    "cash_flow.financing_cf_cash_from_borrowing": 60.0,
    "cash_flow.financing_cf_cash_for_debt_repayment": 60.0,
}
KEY_FIELD_CODE_MAP = {
    "balance_sheet": {
        "balance_sheet.asset_cash_and_cash_equivalents",
        "balance_sheet.asset_accounts_receivable",
        "balance_sheet.asset_inventory",
        "balance_sheet.asset_total_assets",
        "balance_sheet.liability_total_liabilities",
        "balance_sheet.liability_short_term_loans",
        "balance_sheet.equity_unappropriated_profit",
        "balance_sheet.equity_total_equity",
    },
    "income": {
        "income.net_profit",
        "income.other_income",
        "income.total_operating_revenue",
        "income.total_operating_expenses",
        "income.operating_profit",
        "income.total_profit",
        "income.asset_impairment_loss",
        "income.credit_impairment_loss",
    },
    "cash_flow": {
        "cash_flow.net_cash_flow",
        "cash_flow.operating_cf_net_amount",
        "cash_flow.investing_cf_cash_for_investments",
        "cash_flow.financing_cf_cash_from_borrowing",
        "cash_flow.financing_cf_cash_for_debt_repayment",
    },
}
RUN_HISTORY_COLUMNS = [
    "run_id",
    "run_time",
    "git_commit",
    "branch",
    "prompt_name",
    "prompt_goal",
    "model_name",
    "test_files_count",
    "changed_files",
    "success_count",
    "failed_count",
    "empty_count",
    "inserted_rows",
    "total_target_fields",
    "non_empty_fields",
    "non_empty_rate",
    "key_field_total",
    "key_field_hit",
    "key_field_hit_rate",
    "high_risk_fill_count",
    "high_risk_fill_suspect_count",
    "not_found_count",
    "alias_missing_count",
    "entry_missing_count",
    "semantic_unstable_count",
    "unexpected_error_count",
    "final_judgement",
    "notes",
]

SEMANTIC_GUARDRAILS = {
    "income.other_income": {"allow": ["鍏朵粬鏀剁泭"], "deny": ["鍏朵粬缁煎悎鏀剁泭", "鎶曡祫鏀剁泭", "钀ヤ笟澶栨敹鍏?"]},
    "income.operating_expense_cost_of_sales": {"allow": ["钀ヤ笟鎴愭湰"], "deny": ["钀ヤ笟鎬绘垚鏈?", "钀ヤ笟鎬绘敮鍑?", "钀ヤ笟澶栨敮鍑?"]},
    "balance_sheet.liability_short_term_loans": {"allow": ["鐭湡鍊熸"], "deny": ["闀挎湡鍊熸", "涓€骞村唴鍒版湡鐨勯潪娴佸姩璐熷€?", "鍚戜腑澶摱琛屽€熸", "鎷嗗叆璧勯噾"]},
    "cash_flow.operating_cf_net_amount": {"allow": ["经营活动"], "deny": ["投资活动", "筹资活动"]},
    "cash_flow.operating_cf_cash_from_sales": {"allow": ["销售商品", "提供劳务", "经营活动"], "deny": ["投资活动", "筹资活动"]},
    "cash_flow.investing_cf_net_amount": {"allow": ["投资活动"], "deny": ["经营活动", "筹资活动"]},
    "cash_flow.investing_cf_cash_for_investments": {"allow": ["投资支付", "投资活动"], "deny": ["经营活动", "筹资活动"]},
    "cash_flow.investing_cf_cash_from_investment_recovery": {"allow": ["收回投资", "投资活动"], "deny": ["经营活动", "筹资活动"]},
    "cash_flow.financing_cf_cash_from_borrowing": {"allow": ["取得借款", "筹资活动"], "deny": ["经营活动", "投资活动"]},
    "cash_flow.financing_cf_cash_for_debt_repayment": {"allow": ["偿还债务", "筹资活动"], "deny": ["经营活动", "投资活动"]},
    "cash_flow.financing_cf_net_amount": {"allow": ["筹资活动"], "deny": ["经营活动", "投资活动"]},
    "income.total_operating_revenue": {"allow": ["营业总收入", "营业收入"], "deny": ["营业外收入", "营业外支出", "其他收益", "分行业", "分产品", "分地区", "变动原因说明"]},
    "income.total_operating_expenses": {"allow": ["营业总成本", "营业总支出"], "deny": ["营业外收入", "营业外支出", "其他收益"]},
    "income.operating_profit": {"allow": ["营业利润"], "deny": ["利润总额", "净利润"]},
    "income.total_profit": {"allow": ["利润总额"], "deny": ["营业利润", "净利润"]},
    "income.net_profit": {"allow": ["净利润"], "deny": ["营业利润", "利润总额"]},
    "balance_sheet.asset_total_assets": {"allow": ["资产总计", "资产总额", "负债和所有者权益", "负债及所有者权益", "负债和股东权益", "负债及股东权益"], "deny": ["流动资产合计", "非流动资产合计"]},
    "balance_sheet.liability_total_liabilities": {"allow": ["负债合计", "负债总计"], "deny": ["流动负债合计", "非流动负债合计", "负债和所有者权益", "负债及所有者权益", "负债和股东权益", "负债及股东权益"]},
    "balance_sheet.equity_total_equity": {"allow": ["所有者权益", "股东权益"], "deny": ["少数股东权益", "归属于母公司"]},
}

SEMANTIC_GUARDRAILS = build_targeted_semantic_rule_overrides(SEMANTIC_GUARDRAILS)

STATEMENT_TARGET_TABLE_MAP = {"balance_sheet": "balance_sheet", "income": "income", "cash_flow": "cash_flow"}
COLUMN_LABEL_ALIASES = {
    "balance_sheet_current": ["期末余额", "本期期末", "本报告期末", "年末余额", "本期期末"],
    "balance_sheet_previous": ["期初余额", "上年末", "年初余额", "上期末"],
    "income_current": ["本期发生额", "本报告期", "本期金额", "本年累计", "本期"],
    "income_previous": ["上期发生额", "上年同期", "上期金额", "上年金额", "同期"],
    "cash_flow_current": ["本期发生额", "本报告期", "本期金额", "本年累计", "本期"],
    "cash_flow_previous": ["上期发生额", "上年同期", "上期金额", "上年金额", "同期"],
}
FIELD_ALIAS_MAP = {
    "balance_sheet.asset_cash_and_cash_equivalents": ["货币资金", "货币资金余额", "现金及现金等价物", "现金及现金等价物余额"],
    "balance_sheet.asset_accounts_receivable": ["应收账款", "应收账款净额", "应收账款余额", "应收账款账面价值"],
    "balance_sheet.asset_inventory": ["存货", "存货净额", "存货账面价值"],
    "balance_sheet.asset_trading_financial_assets": ["交易性金融资产", "交易性金融资产净额"],
    "balance_sheet.asset_construction_in_progress": ["在建工程", "在建工程净额"],
    "balance_sheet.asset_total_assets": ["资产总计", "资产合计", "总资产", "资产总额", "负债和所有者权益总计", "负债及所有者权益总计", "负债和股东权益总计", "负债及股东权益总计"],
    "balance_sheet.liability_accounts_payable": ["应付账款"],
    "balance_sheet.liability_advance_from_customers": ["预收款项"],
    "balance_sheet.liability_total_liabilities": ["负债合计", "负债总计", "总负债"],
    "balance_sheet.liability_contract_liabilities": ["合同负债"],
    "balance_sheet.liability_short_term_loans": ["短期借款", "短期借款余额"],
    "balance_sheet.equity_unappropriated_profit": ["未分配利润", "未分配利润余额", "未分配利润（累计亏损以“-”号填列）"],
    "balance_sheet.equity_total_equity": ["所有者权益", "股东权益", "所有者权益合计", "股东权益合计", "所有者权益总计", "所有者权益（或股东权益）合计", "股东权益（或所有者权益）合计", "所有者权益或股东权益合计", "所有者权益或股东权益总计"],
    "income.net_profit": ["归属于母公司股东的净利润", "归属于母公司所有者的净利润", "归属于上市公司股东的净利润", "归属于母公司股东净利润", "净利润", "净利润（净亏损以“-”号填列）", "五、净利润", "六、净利润"],
    "income.other_income": ["其他收益", "加：其他收益", "其他收益金额"],
    "income.total_operating_revenue": ["营业总收入", "营业收入", "一、营业收入", "其中：营业收入", "营业总收入（营业收入）"],
    "income.operating_expense_cost_of_sales": ["营业成本", "二、营业成本"],
    "income.operating_expense_selling_expenses": ["销售费用"],
    "income.operating_expense_administrative_expenses": ["管理费用"],
    "income.operating_expense_financial_expenses": ["财务费用"],
    "income.operating_expense_rnd_expenses": ["研发费用"],
    "income.operating_expense_taxes_and_surcharges": ["税金及附加"],
    "income.total_operating_expenses": ["营业总成本", "营业总费用"],
    "income.operating_profit": ["营业利润", "三、营业利润", "营业利润（亏损以“-”号填列）", "营业利润（亏损以\"-\"号填列）"],
    "income.total_profit": ["利润总额", "四、利润总额", "利润总额（亏损总额以“-”号填列）", "利润总额（亏损总额以\"-\"号填列）"],
    "income.asset_impairment_loss": ["资产减值损失", "资产减值损失（损失以“-”号填列）", "加：资产减值损失"],
    "income.credit_impairment_loss": ["信用减值损失", "信用减值损失（损失以“-”号填列）", "加：信用减值损失"],
    "cash_flow.net_cash_flow": ["现金及现金等价物净增加额", "现金及现金等价物净增额", "现金及现金等价物净增加额（净亏损以“-”号填列）", "五、现金及现金等价物净增加额"],
    "cash_flow.operating_cf_net_amount": ["经营活动产生的现金流量净额", "经营活动现金流量净额", "经营活动产生的现金流量净额（净亏损以“-”号填列）"],
    "cash_flow.operating_cf_cash_from_sales": ["销售商品提供劳务收到的现金", "销售商品、提供劳务收到的现金", "销售商品收到的现金"],
    "cash_flow.investing_cf_net_amount": ["投资活动产生的现金流量净额"],
    "cash_flow.investing_cf_cash_for_investments": ["投资支付的现金", "投资所支付的现金", "投资支付现金"],
    "cash_flow.investing_cf_cash_from_investment_recovery": ["收回投资收到的现金", "收回投资所收到的现金"],
    "cash_flow.financing_cf_cash_from_borrowing": ["取得借款收到的现金", "取得借款收到现金", "借款收到的现金"],
    "cash_flow.financing_cf_cash_for_debt_repayment": ["偿还债务支付的现金", "偿还债务支付现金", "偿还债务所支付的现金"],
    "cash_flow.financing_cf_net_amount": ["筹资活动产生的现金流量净额"],
}

ADDITIONAL_FIELD_ALIASES = {
    "balance_sheet.liability_short_term_loans": ["短期借款余额", "其中：短期借款"],
    "balance_sheet.equity_total_equity": ["所有者权益合计", "所有者权益（或股东权益）合计", "股东权益合计", "所有者权益或股东权益合计", "所有者权益或股东权益总计"],
    "balance_sheet.equity_unappropriated_profit": ["未分配利润余额", "未分配利润（累计亏损）"],
    "balance_sheet.asset_trading_financial_assets": ["交易性金融资产净额", "其中：交易性金融资产"],
    "balance_sheet.asset_construction_in_progress": ["在建工程净额", "其中：在建工程"],
    "balance_sheet.asset_total_assets": ["资产总额", "资产总额合计", "负债和所有者权益总计", "负债及所有者权益总计", "负债和股东权益总计", "负债及股东权益总计"],
    "income.net_profit": ["归属于母公司股东的净利润", "归属于母公司所有者的净利润", "归母净利润"],
    "income.operating_profit": ["营业利润（亏损以“-”号填列）", "营业利润（亏损"],
    "income.total_profit": ["利润总额（亏损总额以“-”号填列）", "利润总额（亏损总额"],
    "income.asset_impairment_loss": ["资产减值损失（损失以“-”号填列）", "资产减值损失（损失"],
    "income.credit_impairment_loss": ["信用减值损失（损失以“-”号填列）", "信用减值损失（损失"],
    "income.other_income": ["加：其他收益", "其他收益"],
    "income.total_operating_revenue": ["营业收入净额", "其中：营业总收入", "营业总收入"],
    "cash_flow.investing_cf_cash_for_investments": ["投资支付现金", "投资支付"],
    "cash_flow.investing_cf_cash_from_investment_recovery": ["收回投资收到现金", "收回投资收到的现金", "收回投资收到"],
    "cash_flow.financing_cf_cash_from_borrowing": ["取得借款收到现金", "取得借款收到"],
    "cash_flow.financing_cf_cash_for_debt_repayment": ["偿还债务支付现金", "偿还债务支付"],
    "cash_flow.operating_cf_net_amount": ["经营活动现金流量净额", "经营活动产生的现金流量", "经营活动现金流量", "经营活动产生的现金流"],
    "cash_flow.operating_cf_cash_from_sales": ["销售商品、提供劳务收到的现金", "销售商品提供劳务收到的现金", "销售商品收到的现金"],
    "cash_flow.net_cash_flow": ["现金及现金等价物净增加额", "现金及现金等价物净增加", "现金及现金等价物净增"],
}

ADDITIONAL_HIGH_FREQ_FIELD_ALIASES = {
    "balance_sheet.asset_trading_financial_assets": ["交易性金融资产净额"],
    "balance_sheet.asset_construction_in_progress": ["在建工程净额"],
    "balance_sheet.asset_total_assets": ["资产总额", "资产总额合计", "负债和所有者权益总计", "负债及所有者权益总计", "负债和股东权益总计", "负债及股东权益总计"],
    "balance_sheet.liability_short_term_loans": ["短期借款余额"],
    "balance_sheet.equity_unappropriated_profit": ["未分配利润余额"],
    "balance_sheet.equity_total_equity": ["所有者权益合计", "所有者权益（或股东权益）合计", "所有者权益或股东权益合计"],
    "income.total_operating_revenue": ["营业收入净额", "其中：营业总收入"],
    "income.net_profit": ["归属于母公司股东的净利润"],
    "income.operating_profit": ["营业利润（亏损以“-”号填列）"],
    "income.total_profit": ["利润总额（亏损总额以“-”号填列）"],
    "income.asset_impairment_loss": ["资产减值损失（损失以“-”号填列）"],
    "income.credit_impairment_loss": ["信用减值损失（损失以“-”号填列）"],
    "income.other_income": ["加：其他收益"],
    "cash_flow.investing_cf_cash_for_investments": ["投资支付现金"],
    "cash_flow.investing_cf_cash_from_investment_recovery": ["收回投资收到现金"],
    "cash_flow.financing_cf_cash_from_borrowing": ["取得借款收到现金"],
    "cash_flow.financing_cf_cash_for_debt_repayment": ["偿还债务支付现金"],
    "cash_flow.operating_cf_net_amount": ["经营活动现金流量净额"],
    "cash_flow.operating_cf_cash_from_sales": ["销售商品、提供劳务收到的现金", "销售商品提供劳务收到的现金"],
    "cash_flow.net_cash_flow": ["现金及现金等价物净增加额"],
}

for field_code, aliases in ADDITIONAL_FIELD_ALIASES.items():
    FIELD_ALIAS_MAP.setdefault(field_code, [])
    FIELD_ALIAS_MAP[field_code].extend(aliases)

for field_code, aliases in ADDITIONAL_HIGH_FREQ_FIELD_ALIASES.items():
    HIGH_FREQ_FIELD_ALIASES.setdefault(field_code, [])
    HIGH_FREQ_FIELD_ALIASES[field_code].extend(aliases)

for field_code, aliases in {
    "balance_sheet.liability_short_term_loans": ["流动负债短期借款", "负债-短期借款"],
    "cash_flow.financing_cf_cash_from_borrowing": [
        "融资性现金流-取得借款收到的现金",
        "融资活动产生的现金流量-取得借款收到的现金",
        "取得借款所收到的现金",
    ],
    "cash_flow.financing_cf_cash_for_debt_repayment": [
        "融资性现金流-偿还债务支付的现金",
        "融资活动产生的现金流量-偿还债务支付的现金",
        "偿还债务所支付的现金",
    ],
}.items():
    FIELD_ALIAS_MAP.setdefault(field_code, [])
    FIELD_ALIAS_MAP[field_code].extend(aliases)
    HIGH_FREQ_FIELD_ALIASES.setdefault(field_code, [])
    HIGH_FREQ_FIELD_ALIASES[field_code].extend(aliases)

STRICT_CANDIDATE_FILL_RULES = merge_strict_candidate_fill_rules(STRICT_CANDIDATE_FILL_RULES)
OPTIONAL_RESULT_COLUMNS = [
    ("raw_line_name", "TEXT"),
    ("normalized_line_name", "TEXT"),
    ("source_page", "INTEGER"),
    ("source_column_role", "VARCHAR(64)"),
    ("unit", "VARCHAR(32)"),
    ("confidence", "DOUBLE PRECISION"),
    ("extra_info_json", "TEXT"),
]


def normalize_stock_code(value) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    text = re.sub(r"\s+", "", text)
    return text.zfill(6) if text.isdigit() and len(text) <= 6 else text


def normalize_signed_number_text(value_text: str) -> str:
    text = normalize_text(value_text).replace("（", "(").replace("）", ")")
    text = text.replace("－", "-").replace("—", "-").replace("–", "-").replace("﹣", "-")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    return text.replace(",", "").strip()


def parse_decimal(value_text: str) -> Optional[Decimal]:
    text = normalize_signed_number_text(value_text)
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def get_unit_multiplier(unit: str) -> Decimal:
    unit_text = normalize_text(unit)
    if unit_text == "千元":
        return Decimal("1000")
    if unit_text == "万元":
        return Decimal("10000")
    if unit_text == "亿元":
        return Decimal("100000000")
    return Decimal("1")


def is_ratio_field(field_code: str) -> bool:
    return field_code.endswith("_yoy_growth") or "_ratio_" in field_code or field_code.endswith("asset_liability_ratio")


def should_skip_statement_field(field_code: str) -> bool:
    final_name = normalize_text(field_code.split(".", 1)[1] if "." in field_code else field_code)
    return is_ratio_field(field_code) or final_name in METADATA_FIELD_NAMES or final_name in NON_FINANCIAL_FIELD_CODES


def scale_value_text(value_text: str, unit_multiplier: Decimal, is_ratio: bool) -> str:
    normalized_value = normalize_text(value_text)
    if is_ratio:
        return normalized_value
    numeric_value = parse_decimal(normalized_value)
    if numeric_value is None:
        return normalized_value
    return format((numeric_value * unit_multiplier).quantize(Decimal("0.01")), "f")


def normalize_row_label(value: str) -> str:
    """统一清洗行名，降低附注号、页码残片和异常标点的干扰。"""
    text = normalize_text(value)
    if not text:
        return ""
    text = text.replace("（", "(").replace("）", ")").replace("【", "[").replace("】", "]")
    text = text.replace("：", ":").replace("／", "/").replace("\\", "/")
    text = text.replace("－", "-").replace("—", "-").replace("–", "-")
    text = re.sub(r"\s+", " ", text).strip()
    text = ROW_PAGE_FRAGMENT_PATTERN.sub("", text)
    text = ROW_PREFIX_NOISE_PATTERN.sub("", text)
    text = re.sub(r"^(?:页码?|page)\s*\d+\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\d+\s+", "", text)
    text = ROW_SUFFIX_NOTE_PATTERN.sub("", text).strip()
    text = re.sub(r"\s*(?:附注|注)\s*[一二三四五六七八九十\d]+(?:\s*[、,，.．-]\s*\d+)?\s*$", "", text)
    text = re.sub(r"\([^)]*\)?", "", text)
    text = re.sub(r"\[[^\]]*\]?", "", text)
    text = re.sub(r"[（(][^）)]*[）)]?", "", text)
    if len(TITLE_GLUE_SPLIT_PATTERN.findall(text)) >= 1:
        segments = [segment.strip() for segment in TITLE_GLUE_SPLIT_PATTERN.split(text) if segment.strip()]
        if segments:
            text = segments[-1]
    text = re.sub(r"^(?:其中|加|减)[:：]", "", text)
    text = re.sub(r"(?:项目|本期|上期|本年|上年同期)$", "", text)
    text = strip_trailing_numeric_noise(text)
    text = ROW_TRAILING_EMPTY_MARK_PATTERN.sub("", text).strip()
    text = re.sub(r"\s+", "", text)
    text = text.strip("()[]{}:：,，;；、./")
    return text


def normalize_row_label(value: str) -> str:
    """统一清洗行名，优先保留完整科目名，去掉附注号和尾部数值。"""
    text = normalize_text(value)
    if not text:
        return ""
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = text.replace("\\", "/")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^\d+\s*/\s*\d+\s*", "", text)
    text = re.sub(r"^(?:page|页码)\s*\d+\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:[（(]?\d+[)）./、]\s*|[一二三四五六七八九十]+[、.．]\s*)", "", text)
    text = re.sub(r"\s*(?:附注|注)\s*[一二三四五六七八九十0-9]+(?:\s*[、.．]\s*\d+)?\s*$", "", text)
    text = re.sub(r"\s+[一二三四五六七八九十]+[、.．]?\s*\d+\s*$", "", text).strip()
    text = ROW_TRAILING_NOTE_AND_VALUES_PATTERN.sub("", text).strip()
    text = re.sub(r"(?:项目|本期|上期|本年|上年同期)$", "", text)
    text = re.sub(r"^(?:其中|加|减)[:：]\s*", "", text)
    text = re.sub(r"\([^)]*号填列[^)]*\)", "", text)
    text = re.sub(r"（[^）]*号填列[^）]*）", "", text)
    text = strip_trailing_numeric_noise(text)
    text = ROW_TRAILING_EMPTY_MARK_PATTERN.sub("", text).strip()
    text = re.sub(r"\s+", "", text)
    text = text.strip("()（）[]{}:：；。、/")
    return text


def is_low_information_row_label(text: str) -> bool:
    compact_value = normalize_item_name(text)
    if not compact_value:
        return True
    if compact_value in ROW_CONTEXT_FRAGMENT_LABELS:
        return True
    if len(compact_value) <= 1:
        return True
    if len(compact_value) <= 2 and compact_value.endswith(("额", "金", "益")):
        return True
    return False


def is_structural_row_label(text: str) -> bool:
    normalized = normalize_text(text)
    compact_value = normalize_row_label(normalized)
    if not compact_value:
        return True
    if normalized.rstrip().endswith(("：", ":")):
        return True
    if compact_value.endswith(("活动产生的现金流量", "所有者权益", "股东权益", "流动资产", "流动负债", "非流动资产", "非流动负债")):
        return True
    return False


def merge_fragment_row_labels(*parts: str) -> str:
    merged_parts = []
    for part in parts:
        normalized = normalize_row_label(part)
        if normalized:
            merged_parts.append(normalized)
    return normalize_row_label("".join(merged_parts))


def should_merge_with_next_fragment(current_label: str, next_label: str) -> bool:
    current_norm = normalize_row_label(current_label)
    next_norm = normalize_row_label(next_label)
    if not current_norm or not next_norm:
        return False
    if is_low_information_row_label(current_norm) or is_low_information_row_label(next_norm):
        return True
    if len(current_norm) <= 12 and current_norm.endswith(ROW_MERGE_FRAGMENT_SUFFIXES):
        return True
    return False


def extract_numeric_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    for token in ROW_NUMERIC_TOKEN_PATTERN.findall(normalize_text(text)):
        token = normalize_text(token)
        if not token:
            continue
        compact_value = compact_text(token)
        if not compact_value:
            continue
        if not any(marker in compact_value for marker in [",", ".", "%"]) and len(compact_value.lstrip("-()")) <= 3:
            continue
        tokens.append(token)
    return tokens


def extract_numeric_value_from_text(text: str) -> Optional[str]:
    tokens = extract_numeric_tokens(text)
    if not tokens:
        return None
    return tokens[0]


def collect_numeric_cells(row: Dict, table: Dict) -> List[Dict]:
    numeric_cells: List[Dict] = []
    for cell in row.get("cells", []):
        value_text = normalize_text(cell.get("value_text"))
        if is_empty_cell_value(value_text):
            continue
        numeric_text = extract_numeric_value_from_text(value_text)
        if numeric_text is None:
            continue
        header_item = get_header_item(table, cell.get("column_index"))
        numeric_cells.append(
            {
                **cell,
                "value_text": numeric_text,
                "column_role": normalize_text(cell.get("column_role") or (header_item or {}).get("column_role")),
            }
        )
    return numeric_cells


def resolve_numeric_cell_fallback(
    row: Dict,
    candidate_columns: List[int],
    table: Dict,
    primary_role: str,
) -> Tuple[Optional[Dict], str]:
    numeric_cells = collect_numeric_cells(row, table)
    if not numeric_cells:
        return None, ""

    def sort_key(item: Dict) -> Tuple[int, int, int]:
        role_penalty = 0 if normalize_text(item.get("column_role")) == normalize_text(primary_role) else 1
        if candidate_columns:
            distance = min(abs(int(item.get("column_index") or 0) - column_index) for column_index in candidate_columns)
        else:
            distance = 999
        return role_penalty, distance, int(item.get("column_index") or 0)

    chosen = sorted(numeric_cells, key=sort_key)[0]
    resolution = "same_row_numeric_role_fallback" if normalize_text(chosen.get("column_role")) == normalize_text(primary_role) else "same_row_numeric_adjacent_fallback"
    return chosen, resolution


def resolve_targeted_primary_role_cell(
    row: Dict,
    candidate_columns: List[int],
    table: Dict,
    primary_role: str,
) -> Tuple[Optional[Dict], str]:
    """仅对目标字段做列角色纠偏，优先回收与主角色一致的数值单元格。"""
    numeric_cells = collect_numeric_cells(row, table)
    normalized_primary_role = normalize_text(primary_role)
    primary_role_cells = [
        item for item in numeric_cells if normalize_text(item.get("column_role")) == normalized_primary_role
    ]
    if not primary_role_cells:
        return None, ""

    def sort_key(item: Dict) -> Tuple[int, int, int]:
        direct_match = 0 if item.get("column_index") in candidate_columns else 1
        if candidate_columns:
            distance = min(abs(int(item.get("column_index") or 0) - column_index) for column_index in candidate_columns)
        else:
            distance = 999
        return direct_match, distance, int(item.get("column_index") or 0)

    return sorted(primary_role_cells, key=sort_key)[0], "targeted_primary_role_cell"


def is_numeric_only_row_label(text: str) -> bool:
    normalized = normalize_text(text)
    compact_value = compact_text(normalized)
    if not compact_value:
        return True
    if parse_decimal(normalized) is not None:
        return True
    tokens = extract_numeric_tokens(normalized)
    if not tokens:
        return False
    token_lengths = sum(len(compact_text(token)) for token in tokens)
    return token_lengths >= max(1, len(compact_value) - 2)


def resolve_following_numeric_row_fallback(
    row: Dict,
    table: Dict,
    candidate_columns: List[int],
    primary_role: str,
    max_lookahead: int = 3,
) -> Tuple[Optional[Dict], Optional[Dict], str]:
    rows = table.get("rows") or []
    row_index = int(row.get("row_index") or 0)
    for offset in range(1, max_lookahead + 1):
        next_index = row_index + offset
        if next_index >= len(rows):
            break
        next_row = rows[next_index]
        next_label = normalize_text(next_row.get("row_label") or next_row.get("normalized_label") or "")
        if not next_row.get("cells"):
            if is_context_note_line(next_label) or is_low_information_row_label(next_label):
                continue
            if is_structural_row_label(next_label):
                break
            continue
        if not is_numeric_only_row_label(next_label):
            break
        next_cell, next_resolution = resolve_numeric_cell_fallback(
            row=next_row,
            candidate_columns=candidate_columns,
            table=table,
            primary_role=primary_role,
        )
        if next_cell is not None and not is_empty_cell_value(next_cell.get("value_text", "")):
            return next_row, next_cell, f"following_numeric_row_{offset}_{next_resolution}"
    return None, None, ""


def resolve_targeted_subtotal_fallback(
    field_code: str,
    row: Dict,
    table: Dict,
    candidate_columns: List[int],
    primary_role: str,
    max_lookahead: int = 6,
) -> Tuple[Optional[Dict], Optional[Dict], str]:
    """仅对高确定性目标字段使用小计兜底，避免把长尾空值继续留空。"""
    subtotal_label_map = {
        "cash_flow.financing_cf_cash_from_borrowing": "筹资活动现金流入小计",
    }
    subtotal_label = subtotal_label_map.get(field_code)
    if not subtotal_label:
        return None, None, ""
    rows = table.get("rows") or []
    row_index = int(row.get("row_index") or 0)
    saw_non_target_non_empty = False
    for offset in range(1, max_lookahead + 1):
        next_index = row_index + offset
        if next_index >= len(rows):
            break
        next_row = rows[next_index]
        next_label = normalize_row_label(next_row.get("row_label") or next_row.get("normalized_label") or "")
        if compact_row_label(next_label) == compact_row_label(subtotal_label):
            if saw_non_target_non_empty:
                return None, None, ""
            subtotal_cell, subtotal_reason = resolve_numeric_cell_fallback(
                row=next_row,
                candidate_columns=candidate_columns,
                table=table,
                primary_role=primary_role,
            )
            if subtotal_cell is not None and not is_empty_cell_value(subtotal_cell.get("value_text", "")):
                return next_row, subtotal_cell, f"targeted_subtotal_{subtotal_reason}"
            return None, None, ""
        if is_structural_row_label(next_label):
            break
        if not next_row.get("cells"):
            continue
        if any(not is_empty_cell_value(cell.get("value_text", "")) for cell in next_row.get("cells", [])):
            saw_non_target_non_empty = True
    return None, None, ""


def is_context_note_line(text: str) -> bool:
    normalized = normalize_text(text)
    compact_value = compact_text(normalized)
    if not compact_value:
        return True
    if ROW_NOTE_TOKEN_PATTERN.fullmatch(compact_value):
        return True
    if "填列" in normalized and len(compact_value) <= 12:
        return True
    if compact_value in {"填列", "号填列", "列"}:
        return True
    return False


def split_page_text_lines(page_text: str) -> List[str]:
    return [normalize_text(line) for line in normalize_text(page_text).splitlines() if normalize_text(line)]


def is_primary_statement_payload(payload: NormalizedTable) -> bool:
    expected_markers = PRIMARY_STATEMENT_TITLE_MARKERS.get(normalize_text(payload.statement_type), [])
    title_text = normalize_text(payload.title_text or "")
    header_text = normalize_text(payload.header_text or "")
    text_prefix = normalize_text(payload.text or "")[:240]
    if any(token in title_text for token in NON_PRIMARY_STATEMENT_TITLE_TOKENS):
        return False
    for marker in expected_markers:
        if marker and (marker in title_text or marker in header_text or marker in text_prefix):
            return True
    return False


def find_alias_line_window(lines: List[str], alias: str, max_window_lines: int = 4) -> Optional[Tuple[int, int]]:
    alias_compact = compact_row_label(alias)
    if not alias_compact or len(alias_compact) < 4:
        return None
    for start_index in range(len(lines)):
        merged_parts: List[str] = []
        for end_index in range(start_index, min(len(lines), start_index + max_window_lines)):
            merged_parts.append(lines[end_index])
            merged_compact = compact_row_label("".join(merged_parts))
            if not merged_compact:
                continue
            if merged_compact == alias_compact:
                return start_index, end_index
            if merged_compact.startswith(alias_compact) and len(merged_compact) - len(alias_compact) <= 8:
                return start_index, end_index
            if alias_compact in merged_compact and len(merged_compact) - len(alias_compact) <= 24:
                return start_index, end_index
            if alias_compact.startswith(merged_compact) and len(alias_compact) - len(merged_compact) <= 8:
                continue
            if len(merged_compact) > len(alias_compact) + 24:
                break
    return None


def collect_context_numbers_from_lines(
    lines: List[str],
    window_end_index: int,
    primary_role: str,
) -> Tuple[Optional[str], str]:
    collected: List[str] = []
    for line in lines[window_end_index + 1 : window_end_index + 8]:
        if is_context_note_line(line):
            continue
        numeric_tokens = extract_numeric_tokens(line)
        if numeric_tokens:
            collected.extend(numeric_tokens)
            if len(collected) >= 2:
                break
            continue
        if is_structural_row_label(line):
            break
        if compact_row_label(line):
            break
    if not collected:
        return None, ""
    if normalize_text(primary_role) == "previous_period" and len(collected) >= 2:
        return collected[1], "previous"
    return collected[0], "current"


def collect_context_numbers_from_lines_loose(
    lines: List[str],
    window_end_index: int,
    primary_role: str,
    max_scan_lines: int = 12,
) -> Tuple[Optional[str], str]:
    collected: List[str] = []
    hard_break_count = 0
    for line in lines[window_end_index + 1 : window_end_index + 1 + max_scan_lines]:
        normalized = normalize_text(line)
        if not normalized:
            continue
        numeric_tokens = extract_numeric_tokens(normalized)
        if numeric_tokens:
            collected.extend(numeric_tokens)
            if len(collected) >= 2:
                break
            continue
        if is_context_note_line(normalized) or is_low_information_row_label(normalized):
            continue
        if is_structural_row_label(normalized):
            hard_break_count += 1
            if hard_break_count >= 2:
                break
            continue
        hard_break_count += 1
        if hard_break_count >= 2:
            break
    if not collected:
        return None, ""
    if normalize_text(primary_role) == "previous_period" and len(collected) >= 2:
        return collected[1], "previous"
    return collected[0], "current"


def collect_targeted_window_numbers(
    lines: List[str],
    window_start_index: int,
    window_end_index: int,
    primary_role: str,
    max_scan_lines: int = 14,
) -> Tuple[Optional[str], str]:
    collected: List[str] = []
    merged_window = " ".join(lines[window_start_index : window_end_index + 1])
    merged_tokens = extract_numeric_tokens(merged_window)
    if merged_tokens:
        collected.extend(merged_tokens)
    hard_break_count = 0
    for line in lines[window_end_index + 1 : window_end_index + 1 + max_scan_lines]:
        normalized = normalize_text(line)
        if not normalized:
            continue
        numeric_tokens = extract_numeric_tokens(normalized)
        if numeric_tokens:
            collected.extend(numeric_tokens)
            if len(collected) >= 2:
                break
            continue
        if is_context_note_line(normalized) or is_low_information_row_label(normalized):
            continue
        if is_structural_row_label(normalized):
            hard_break_count += 1
            if hard_break_count >= 2:
                break
            continue
        if len(compact_row_label(normalized)) <= 6:
            continue
        hard_break_count += 1
        if hard_break_count >= 2:
            break
    if not collected:
        return None, ""
    if normalize_text(primary_role) == "previous_period" and len(collected) >= 2:
        return collected[1], "previous"
    return collected[0], "current"


def load_raw_block_texts(file_id: int) -> List[Dict]:
    cached = RAW_BLOCK_TEXT_CACHE.get(file_id)
    if cached is not None:
        return cached
    path = RAW_BLOCK_JSON_DIR / f"file_{file_id}_blocks.json"
    if not path.exists():
        RAW_BLOCK_TEXT_CACHE[file_id] = []
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        RAW_BLOCK_TEXT_CACHE[file_id] = []
        return []
    blocks = payload.get("blocks") or []
    normalized_blocks: List[Dict] = []
    for block in blocks:
        text = normalize_text(block.get("text") or "")
        if not text:
            continue
        normalized_blocks.append(
            {
                "block_id": block.get("block_id"),
                "page_start": block.get("page_start"),
                "page_end": block.get("page_end"),
                "text": text,
            }
        )
    RAW_BLOCK_TEXT_CACHE[file_id] = normalized_blocks
    return normalized_blocks


def is_targeted_fragment_match(field_code: str, merged_text: str) -> bool:
    compact_text_value = compact_row_label(merged_text)
    if not compact_text_value:
        return False
    blacklist = get_targeted_fragment_blacklist(field_code)
    if any(compact_row_label(token) in compact_text_value for token in blacklist if compact_row_label(token)):
        return False
    if field_code != "cash_flow.operating_cf_cash_from_sales":
        return False
    required_groups = [
        ["销售商品", "提供劳务", "收到的现金"],
        ["销售商品", "收到的现金"],
    ]
    for group in required_groups:
        if all(compact_row_label(token) in compact_text_value for token in group):
            return True
    return False


def is_targeted_fragment_match_v2(field_code: str, merged_text: str) -> bool:
    compact_text_value = compact_row_label(merged_text)
    if not compact_text_value:
        return False
    blacklist = get_targeted_fragment_blacklist(field_code)
    if any(compact_row_label(token) in compact_text_value for token in blacklist if compact_row_label(token)):
        return False
    alias_compacts = [
        compact_row_label(alias)
        for alias in get_targeted_precise_aliases(field_code)
        if compact_row_label(alias)
    ]
    for alias_compact in alias_compacts:
        if compact_text_value == alias_compact:
            return True
        if compact_text_value.startswith(alias_compact) and len(compact_text_value) - len(alias_compact) <= 2:
            return True
        if alias_compact.startswith(compact_text_value) and len(alias_compact) - len(compact_text_value) <= 2:
            return True
    return False


def extract_targeted_block_text_numbers(
    file_id: int,
    field_code: str,
    primary_role: str,
) -> Tuple[Optional[str], Optional[int], str, str]:
    if field_code not in {
        "cash_flow.operating_cf_cash_from_sales",
        "cash_flow.financing_cf_cash_from_borrowing",
        "cash_flow.financing_cf_cash_for_debt_repayment",
    }:
        return None, None, "", ""
    for block in load_raw_block_texts(file_id):
        lines = split_page_text_lines(block.get("text") or "")
        if not lines:
            continue
        for start_index in range(len(lines)):
            merged_parts: List[str] = []
            for end_index in range(start_index, min(len(lines), start_index + 8)):
                merged_parts.append(lines[end_index])
                merged_text = merge_fragment_row_labels(*merged_parts)
                if not is_targeted_fragment_match_v2(field_code, merged_text):
                    continue
                value_text, value_role = collect_targeted_window_numbers(
                    lines=lines,
                    window_start_index=start_index,
                    window_end_index=end_index,
                    primary_role=primary_role,
                    max_scan_lines=16,
                )
                if value_text:
                    page_no = block.get("page_start") if block.get("page_start") == block.get("page_end") else block.get("page_start")
                    return value_text, page_no, merged_text, f"targeted_block_text_{value_role}"
    return None, None, "", ""


def is_specific_alias_for_context(field_code: str, alias: str) -> bool:
    normalized = normalize_text(alias)
    if not normalized:
        return False
    specific_tokens = {
        "balance_sheet.equity_total_equity": ["鍚堣", "鎬昏", "鎬婚"],
        "balance_sheet.liability_short_term_loans": ["鐭湡鍊熸"],
        "income.other_income": ["鍏朵粬鏀剁泭"],
        "income.operating_expense_cost_of_sales": ["钀ヤ笟鎴愭湰"],
        "income.total_profit": ["鍒╂鼎鎬婚"],
        "cash_flow.operating_cf_cash_from_sales": ["閿€鍞晢鍝?"],
        "cash_flow.financing_cf_cash_from_borrowing": ["鍊熸"],
        "cash_flow.financing_cf_cash_for_debt_repayment": ["鍋胯繕鍊哄姟"],
    }
    tokens = specific_tokens.get(field_code)
    if not tokens:
        return len(compact_row_label(normalized)) >= 6
    return any(token and token in normalized for token in tokens)


def extract_row_context_numbers(
    row: Dict,
    payload: NormalizedTable,
    matched_variant: str,
    primary_role: str,
) -> Tuple[Optional[str], str]:
    if matched_variant and compact_row_label(matched_variant) in {
        compact_row_label("所有者权益合计"),
        compact_row_label("股东权益合计"),
        compact_row_label("所有者权益总计"),
        compact_row_label("股东权益总计"),
        compact_row_label("所有者权益或股东权益合计"),
        compact_row_label("所有者权益或股东权益总计"),
    }:
        return None, ""
    raw_line_text = normalize_text(row.get("source_text") or row.get("raw_line_text") or "")
    row_label = normalize_text(row.get("row_label") or "")
    raw_label = normalize_text(row.get("raw_label") or row_label)

    tokens = extract_numeric_tokens(raw_line_text)
    if tokens:
        if normalize_text(primary_role) == "previous_period" and len(tokens) >= 2:
            return tokens[1], "raw_line_numeric_tail_previous"
        return tokens[0], "raw_line_numeric_tail_current"

    page_text = normalize_text((payload.page_text_map or {}).get(str(row.get("page_no") or ""), ""))
    if not page_text:
        return None, ""
    lines = split_page_text_lines(page_text)
    if not lines:
        return None, ""

    for anchor in [raw_line_text, raw_label, row_label, matched_variant]:
        anchor = normalize_text(anchor)
        if not anchor:
            continue
        alias_window = find_alias_line_window(lines, anchor)
        if alias_window is None:
            continue
        value_text, value_role = collect_context_numbers_from_lines(lines, alias_window[1], primary_role)
        if value_text:
            return value_text, f"page_text_following_lines_{value_role}"
    return None, ""


def extract_raw_line_numeric_by_role(row: Dict, primary_role: str) -> Tuple[Optional[str], str]:
    raw_line_text = normalize_text(row.get("source_text") or row.get("raw_line_text") or "")
    tokens = extract_numeric_tokens(raw_line_text)
    if not tokens:
        return None, ""
    if normalize_text(primary_role) == "previous_period":
        if len(tokens) >= 2:
            return tokens[-1], "raw_line_numeric_tail_previous"
        return tokens[0], "raw_line_numeric_tail_previous"
    if len(tokens) >= 2:
        return tokens[-2], "raw_line_numeric_tail_current"
    return tokens[-1], "raw_line_numeric_tail_current"


def should_prioritize_raw_line_numeric(
    field_code: str,
    cell: Optional[Dict],
    cell_resolution: str,
    primary_role: str,
) -> bool:
    return should_prioritize_raw_line_numeric_for_field(
        field_code=field_code,
        cell_value_text=(cell or {}).get("value_text", ""),
        cell_role=(cell or {}).get("column_role", ""),
        cell_resolution=cell_resolution,
        primary_role=primary_role,
        parse_decimal_fn=parse_decimal,
        is_empty_cell_value_fn=is_empty_cell_value,
        normalize_text_fn=normalize_text,
    )


def extract_alias_context_numbers(
    field_code: str,
    aliases: List[str],
    payload: NormalizedTable,
    primary_role: str,
) -> Tuple[Optional[str], Optional[int], str, str]:
    if not is_primary_statement_payload(payload):
        return None, None, "", ""
    if field_code == "balance_sheet.equity_total_equity":
        aliases = [
            alias
            for alias in aliases
            if compact_row_label(alias) in {
                compact_row_label("所有者权益合计"),
                compact_row_label("股东权益合计"),
                compact_row_label("所有者权益总计"),
                compact_row_label("股东权益总计"),
                compact_row_label("所有者权益总额"),
                compact_row_label("股东权益总额"),
                compact_row_label("所有者权益或股东权益合计"),
                compact_row_label("所有者权益或股东权益总计"),
                compact_row_label("负债和所有者权益总计"),
                compact_row_label("负债及所有者权益总计"),
                compact_row_label("负债和股东权益总计"),
                compact_row_label("负债及股东权益总计"),
            }
        ]
    for page_no_text, page_text_raw in (payload.page_text_map or {}).items():
        lines = split_page_text_lines(page_text_raw)
        if not lines:
            continue
        for alias in aliases:
            alias = normalize_text(alias)
            if len(alias) < 4:
                continue
            alias_window = find_alias_line_window(lines, alias)
            if alias_window is None:
                continue
            value_text, value_role = collect_context_numbers_from_lines(lines, alias_window[1], primary_role)
            if value_text:
                page_no = int(page_no_text) if str(page_no_text).isdigit() else None
                return value_text, page_no, alias, f"page_text_alias_{value_role}"
    if field_code in TARGETED_GUARDRAIL_FIELD_CODES:
        merged_lines = split_page_text_lines(payload.text or "")
        for alias in aliases:
            alias = normalize_text(alias)
            if len(alias) < 4:
                continue
            if not is_specific_alias_for_context(field_code, alias):
                continue
            alias_window = find_alias_line_window(merged_lines, alias, max_window_lines=6)
            if alias_window is None:
                continue
            value_text, value_role = collect_context_numbers_from_lines_loose(merged_lines, alias_window[1], primary_role)
            if value_text:
                return value_text, None, alias, f"merged_text_alias_{value_role}"
    return None, None, "", ""


def build_candidate_from_alias_context(field: Dict, table: Dict, payload: NormalizedTable) -> Optional[Dict]:
    value_text, source_page, matched_alias, resolution = extract_alias_context_numbers(
        field_code=field["field_code"],
        aliases=field.get("aliases", []),
        payload=payload,
        primary_role=field.get("preferred_role") or "current_period",
    )
    if not value_text:
        return None
    scaled_value = scale_value_text(
        value_text=value_text,
        unit_multiplier=table["unit_multiplier"],
        is_ratio=is_ratio_field(field["field_code"]),
    )
    numeric_value = parse_decimal(scaled_value)
    if numeric_value is None and not is_ratio_field(field["field_code"]):
        return None
    confidence = 0.86
    return {
        "target_table": field["target_table"],
        "field_code": field["field_code"],
        "field_name_cn": field["field_name_cn"],
        "value_text": scaled_value,
        "numeric_value": numeric_value,
        "row_id": -1,
        "raw_line_name": matched_alias,
        "normalized_line_name": normalize_row_label(matched_alias),
        "source_page": source_page,
        "source_column_role": field.get("preferred_role") or "current_period",
        "column_label": f"context:{resolution}",
        "unit": payload.unit or "元",
        "confidence": confidence,
        "source_page_range": str(source_page) if source_page is not None else "",
        "source_text": f"matched_alias={matched_alias}\nvalue_text={value_text}\nresolution={resolution}",
        "candidate_rank": 0,
        "candidate_score": confidence,
        "extract_method": RULE_METHOD,
        "extra_info_json": {
            "statement_type": payload.statement_type,
            "unit": payload.unit,
            "currency": payload.currency,
            "locator_confidence": payload.locator_confidence,
            "parse_confidence": payload.parser_meta.get("parse_confidence"),
            "matched_alias": matched_alias,
            "matched_variant": normalize_row_label(matched_alias),
            "row_match_score": 86.0,
            "cell_resolution": resolution,
            "column_role_policy": {
                "primary_role": field.get("preferred_role") or "current_period",
                "allow_previous_fallback": False,
                "fallback_requires_opt_in": False,
                "primary_column": None,
                "fallback_column": None,
            },
            "used_fallback_role": False,
            "column_role_warning": "",
            "allow_previous_period_fallback": False,
            "fill_stage": "rule_alias_context",
        },
    }


def extract_targeted_page_text_numbers(
    field_code: str,
    payload: NormalizedTable,
    primary_role: str,
) -> Tuple[Optional[str], Optional[int], str, str]:
    if field_code == "balance_sheet.liability_short_term_loans":
        return None, None, "", ""
    aliases = get_targeted_precise_aliases(field_code)
    if not aliases:
        return None, None, "", ""
    for page_no_text, page_text_raw in (payload.page_text_map or {}).items():
        lines = split_page_text_lines(page_text_raw)
        if not lines:
            continue
        for alias in aliases:
            alias = normalize_text(alias)
            if not alias:
                continue
            alias_window = find_alias_line_window(lines, alias, max_window_lines=6)
            if alias_window is None:
                continue
            merged_text = merge_fragment_row_labels(*lines[alias_window[0] : alias_window[1] + 1])
            blocked, _ = should_reject_targeted_row(
                field_code=field_code,
                texts=[merged_text, alias],
                compact_row_label_fn=compact_row_label,
            )
            if blocked:
                continue
            value_text, value_role = collect_targeted_window_numbers(
                lines=lines,
                window_start_index=alias_window[0],
                window_end_index=alias_window[1],
                primary_role=primary_role,
            )
            if value_text:
                page_no = int(page_no_text) if str(page_no_text).isdigit() else None
                return value_text, page_no, merged_text or alias, f"targeted_page_text_{value_role}"
    return None, None, "", ""


def build_candidate_from_targeted_page_text(field: Dict, table: Dict, payload: NormalizedTable) -> Optional[Dict]:
    value_text, source_page, matched_alias, resolution = extract_targeted_page_text_numbers(
        field_code=field["field_code"],
        payload=payload,
        primary_role=field.get("preferred_role") or "current_period",
    )
    if not value_text:
        return None
    scaled_value = scale_value_text(
        value_text=value_text,
        unit_multiplier=table["unit_multiplier"],
        is_ratio=is_ratio_field(field["field_code"]),
    )
    numeric_value = parse_decimal(scaled_value)
    if numeric_value is None and not is_ratio_field(field["field_code"]):
        return None
    confidence = 0.88
    return {
        "target_table": field["target_table"],
        "field_code": field["field_code"],
        "field_name_cn": field["field_name_cn"],
        "value_text": scaled_value,
        "numeric_value": numeric_value,
        "row_id": -1,
        "raw_line_name": matched_alias,
        "normalized_line_name": normalize_row_label(matched_alias),
        "source_page": source_page,
        "source_column_role": field.get("preferred_role") or "current_period",
        "column_label": f"context:{resolution}",
        "unit": payload.unit or "鍏?",
        "confidence": confidence,
        "source_page_range": str(source_page) if source_page is not None else "",
        "source_text": f"matched_alias={matched_alias}\nvalue_text={value_text}\nresolution={resolution}",
        "candidate_rank": 0,
        "candidate_score": confidence,
        "extract_method": RULE_METHOD,
        "extra_info_json": {
            "statement_type": payload.statement_type,
            "unit": payload.unit,
            "currency": payload.currency,
            "locator_confidence": payload.locator_confidence,
            "parse_confidence": payload.parser_meta.get("parse_confidence"),
            "matched_alias": matched_alias,
            "matched_variant": normalize_row_label(matched_alias),
            "row_match_score": 88.0,
            "cell_resolution": resolution,
            "column_role_policy": {
                "primary_role": field.get("preferred_role") or "current_period",
                "allow_previous_fallback": False,
                "fallback_requires_opt_in": False,
                "primary_column": None,
                "fallback_column": None,
            },
            "used_fallback_role": False,
            "column_role_warning": "",
            "allow_previous_period_fallback": False,
            "fill_stage": "rule_targeted_page_text",
        },
    }


def build_candidate_from_targeted_block_text(field: Dict, table: Dict, payload: NormalizedTable) -> Optional[Dict]:
    value_text, source_page, matched_alias, resolution = extract_targeted_block_text_numbers(
        file_id=int(payload.file_id or 0),
        field_code=field["field_code"],
        primary_role=field.get("preferred_role") or "current_period",
    )
    if not value_text:
        return None
    scaled_value = scale_value_text(
        value_text=value_text,
        unit_multiplier=table["unit_multiplier"],
        is_ratio=is_ratio_field(field["field_code"]),
    )
    numeric_value = parse_decimal(scaled_value)
    if numeric_value is None and not is_ratio_field(field["field_code"]):
        return None
    confidence = 0.84
    return {
        "target_table": field["target_table"],
        "field_code": field["field_code"],
        "field_name_cn": field["field_name_cn"],
        "value_text": scaled_value,
        "numeric_value": numeric_value,
        "row_id": -1,
        "raw_line_name": matched_alias,
        "normalized_line_name": normalize_row_label(matched_alias),
        "source_page": source_page,
        "source_column_role": field.get("preferred_role") or "current_period",
        "column_label": f"context:{resolution}",
        "unit": table.get("unit") or "元",
        "confidence": confidence,
        "source_page_range": str(source_page) if source_page is not None else "",
        "source_text": f"matched_alias={matched_alias}\nvalue_text={value_text}\nresolution={resolution}",
        "candidate_rank": 0,
        "candidate_score": confidence,
        "extract_method": RULE_METHOD,
        "extra_info_json": {
            "statement_type": table["statement_type"],
            "unit": table.get("unit"),
            "currency": table.get("currency"),
            "matched_alias": matched_alias,
            "matched_variant": normalize_row_label(matched_alias),
            "row_match_score": 84.0,
            "column_role_policy": {
                "primary_role": field.get("preferred_role") or "current_period",
                "allow_previous_fallback": False,
                "fallback_requires_opt_in": False,
                "primary_column": None,
                "fallback_column": None,
            },
            "used_fallback_role": False,
            "column_role_warning": "",
            "allow_previous_period_fallback": False,
            "fill_stage": "rule_targeted_block_text",
        },
    }


def is_empty_cell_value(value_text: str) -> bool:
    compact_value = compact_text(value_text).lower()
    return compact_value in EMPTY_VALUE_TOKENS


def compact_row_label(value: str) -> str:
    return compact_text(normalize_row_label(value))


def split_row_text_variants(*values: str) -> List[str]:
    """从原始行名和 source_text 中拆出更干净的候选片段。"""
    variants = set()
    for value in values:
        text = normalize_text(value)
        if not text:
            continue
        normalized = normalize_row_label(text)
        if normalized:
            variants.add(normalized)
        pieces = re.split(r"[\n|｜]", text)
        for piece in pieces:
            piece = normalize_text(piece)
            if not piece:
                continue
            piece = re.sub(r"^\d+\s*/\s*", "", piece)
            sub_parts = TITLE_GLUE_SPLIT_PATTERN.split(piece)
            for sub_part in sub_parts:
                normalized_part = normalize_row_label(sub_part)
                if normalized_part:
                    variants.add(normalized_part)
    filtered = [item for item in variants if not is_low_information_row_label(item)]
    if filtered:
        return sorted(filtered, key=len)
    return sorted(variants, key=len)


def is_polluted_row_variant(text: str, alias: str) -> bool:
    compact_value = compact_row_label(text)
    alias_compact = compact_row_label(alias)
    if not compact_value:
        return True
    if alias_compact and alias_compact in compact_value:
        return False
    heading_hits = len(TITLE_GLUE_SPLIT_PATTERN.findall(text))
    return len(compact_value) >= 20 or heading_hits >= 2


def is_candidate_fill_allowed(field: Dict) -> bool:
    field_code = normalize_text(field.get("field_code"))
    if not field_code:
        return False
    final_name = field_code.split(".", 1)[1] if "." in field_code else field_code
    if final_name in NON_FINANCIAL_FIELD_CODES:
        return False
    whitelist = CANDIDATE_FILL_WHITELIST.get(normalize_text(field.get("target_table")), set())
    return field_code in whitelist


def is_strict_candidate_fill_allowed(field: Dict, candidate: Dict) -> bool:
    rule = STRICT_CANDIDATE_FILL_RULES.get(normalize_text(field.get("field_code")))
    if not rule:
        return True
    normalized_line = normalize_row_label(candidate.get("normalized_line_name") or candidate.get("raw_line_name") or "")
    compact_line = compact_row_label(normalized_line)
    for token in rule.get("deny_contains", []):
        if compact_row_label(token) and compact_row_label(token) in compact_line:
            return False
    allow_exact = rule.get("allow_exact") or []
    if allow_exact and compact_line not in {compact_row_label(item) for item in allow_exact if compact_row_label(item)}:
        return False
    return True


def build_field_aliases(field_code: str, field_name_cn: str) -> List[str]:
    aliases = set()
    field_name = normalize_text(field_name_cn)
    if field_name:
        aliases.add(field_name)
        aliases.add(normalize_item_name(field_name))
        aliases.add(re.sub(r"[（(].*?[)）]", "", field_name).strip())
    for alias in FIELD_ALIAS_MAP.get(field_code, []):
        aliases.add(alias)
        aliases.add(normalize_item_name(alias))
    result = []
    for alias in aliases:
        alias = normalize_item_name(alias)
        if alias:
            result.append(alias)
    return sorted(set(result), key=len, reverse=True)


def build_field_aliases_v2(field_code: str, field_name_cn: str) -> List[str]:
    """????????????????????????????"""
    aliases = set(build_field_aliases(field_code, field_name_cn))
    field_name = normalize_text(field_name_cn)
    if field_name:
        aliases.add(normalize_item_name(normalize_row_label(field_name)))
        aliases.add(normalize_item_name(re.sub(r"[?(].*?[)?]", "", field_name).strip()))
    for alias in FIELD_ALIAS_MAP.get(field_code, []) + HIGH_FREQ_FIELD_ALIASES.get(field_code, []):
        aliases.add(normalize_item_name(normalize_row_label(alias)))
    for alias in get_targeted_precise_aliases(field_code):
        aliases.add(normalize_item_name(normalize_row_label(alias)))
    cleaned_aliases = {alias for alias in aliases if alias}
    if field_code == "income.other_income":
        field_name_compact = compact_row_label(field_name_cn)
        exact_aliases = {
            normalize_item_name(normalize_row_label(alias))
            for alias in get_targeted_precise_aliases(field_code)
        }
        cleaned_aliases = {
            alias
            for alias in cleaned_aliases
            if alias in exact_aliases
            or (field_name_compact and compact_row_label(alias) == field_name_compact)
        }
        cleaned_aliases.update({alias for alias in exact_aliases if alias})
    if field_code == "cash_flow.operating_cf_cash_from_sales":
        cleaned_aliases.update(
            {
                normalize_item_name(normalize_row_label(alias))
                for alias in get_targeted_precise_aliases(field_code)
            }
        )
    if field_code == "balance_sheet.equity_total_equity":
        allowed_compacts = {
            compact_row_label("所有者权益合计"),
            compact_row_label("股东权益合计"),
            compact_row_label("所有者权益总计"),
            compact_row_label("股东权益总计"),
            compact_row_label("所有者权益总额"),
            compact_row_label("股东权益总额"),
            compact_row_label("所有者权益或股东权益合计"),
            compact_row_label("所有者权益或股东权益总计"),
            compact_row_label("负债和所有者权益总计"),
            compact_row_label("负债及所有者权益总计"),
            compact_row_label("负债和股东权益总计"),
            compact_row_label("负债及股东权益总计"),
        }
        cleaned_aliases = {
            alias
            for alias in cleaned_aliases
            if compact_row_label(alias) in allowed_compacts
        }
    return sorted(cleaned_aliases, key=len, reverse=True)


def parse_json_name(file_name: str) -> Optional[Dict]:
    match = re.match(r"file_(\d+)_(balance_sheet|income|cash_flow)\.json$", file_name)
    if not match:
        return None
    return {"file_id": int(match.group(1)), "statement_type": match.group(2)}


def list_statement_json_files(file_ids: Optional[List[int]] = None, statement_types: Optional[List[str]] = None, limit: Optional[int] = None) -> List[Path]:
    if not STATEMENT_JSON_DIR.exists():
        raise FileNotFoundError(f"statement_json 目录不存在：{STATEMENT_JSON_DIR}")
    file_id_filter = set(file_ids or [])
    statement_type_filter = set(statement_types or [])
    files = []
    for path in STATEMENT_JSON_DIR.glob("file_*_*.json"):
        parsed = parse_json_name(path.name)
        if parsed is None:
            continue
        if file_id_filter and parsed["file_id"] not in file_id_filter:
            continue
        if statement_type_filter and parsed["statement_type"] not in statement_type_filter:
            continue
        files.append((parsed["file_id"], parsed["statement_type"], path))
    files.sort(key=lambda item: (item[0], item[1]))
    if limit is not None and not file_id_filter:
        # --limit 需要与入库脚本保持一致，按前 N 个 file_id 限定范围，而不是前 N 个 JSON 文件。
        allowed_file_ids = []
        seen_file_ids = set()
        for file_id, _statement_type, _path in files:
            if file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)
            allowed_file_ids.append(file_id)
            if len(allowed_file_ids) >= limit:
                break
        allowed_file_id_set = set(allowed_file_ids)
        files = [item for item in files if item[0] in allowed_file_id_set]
    return [path for _file_id, _statement_type, path in files]


def get_extract_watchdog_seconds() -> int:
    """读取单个 JSON 抽取 watchdog 秒数；默认关闭，不影响正常逻辑。"""
    raw_value = normalize_text(os.getenv(EXTRACT_WATCHDOG_ENV, ""))
    if not raw_value:
        return 0
    try:
        seconds = int(raw_value)
    except ValueError:
        return 0
    return max(seconds, 0)


def build_watchdog_path(run_id: str) -> Path:
    """生成 watchdog 栈日志路径。"""
    RUN_SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    safe_run_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", run_id or "manual")
    return RUN_SUMMARY_DIR / f"watchdog_stack_{safe_run_id}.log"


def get_table_columns(conn, table_name: str) -> set:
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


def ensure_extract_result_schema(conn) -> set:
    cur = conn.cursor()
    try:
        for column_name, column_type in OPTIONAL_RESULT_COLUMNS:
            cur.execute(f"ALTER TABLE attachment3_extract_result ADD COLUMN IF NOT EXISTS {column_name} {column_type}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
    return get_table_columns(conn, "attachment3_extract_result")


def fetch_field_dict(conn, target_table: str) -> List[Dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT target_table, field_code, field_name_cn, data_type, sort_order
        FROM attachment3_field_dict
        WHERE target_table = %s
        ORDER BY sort_order, field_code
        """,
        (target_table,),
    )
    rows = cur.fetchall()
    cur.close()
    result = []
    for target_table_value, field_code, field_name_cn, data_type, sort_order in rows:
        result.append(
            {
                "target_table": target_table_value,
                "field_code": field_code,
                "field_name_cn": field_name_cn,
                "data_type": normalize_text(data_type).lower(),
                "sort_order": sort_order,
                "aliases": build_field_aliases_v2(field_code, field_name_cn),
                "preferred_role": "current_period",
            }
        )
    return result


def parse_table_structure(payload: Dict | NormalizedTable) -> Dict:
    table = payload if isinstance(payload, NormalizedTable) else load_normalized_table_json(Path(payload))
    column_map = {column.col_id: column for column in table.column_schema}
    header = [{"column_index": column.col_id, "column_label": column.raw_name, "column_role": column.role} for column in table.column_schema]
    rows = []
    raw_rows = list(table.rows)
    for row_index, row in enumerate(raw_rows):
        cells = []
        for cell_key, value_text in row.cells.items():
            try:
                column_index = int(cell_key)
            except (TypeError, ValueError):
                continue
            column = column_map.get(column_index)
            if column is None:
                continue
            cells.append({"column_index": column_index, "column_label": column.raw_name, "column_role": column.role, "value_text": normalize_text(value_text)})
        row_label_raw = normalize_text(row.raw_item_name)
        row_label_clean = normalize_row_label(row_label_raw or row.normalized_item_name)
        normalized_label = normalize_item_name(row_label_clean or row.normalized_item_name or row_label_raw)
        if not cells:
            cells = synthesize_cells_from_raw_line(
                statement_type=table.statement_type,
                row_label_raw=row_label_raw,
                normalized_label=normalized_label,
                raw_line_text=normalize_text(row.raw_line_text),
                column_map=column_map,
            )
        prev_row = raw_rows[row_index - 1] if row_index > 0 else None
        next_row = raw_rows[row_index + 1] if row_index + 1 < len(raw_rows) else None
        contextual_variants = []
        if prev_row is not None and is_low_information_row_label(row_label_raw):
            merged_prev = merge_fragment_row_labels(prev_row.raw_item_name, row.raw_item_name)
            if merged_prev:
                contextual_variants.append(merged_prev)
        if next_row is not None and should_merge_with_next_fragment(row.raw_item_name, next_row.raw_item_name):
            merged_next = merge_fragment_row_labels(row.raw_item_name, next_row.raw_item_name)
            if merged_next and merged_next != row_label_clean and not is_low_information_row_label(merged_next):
                contextual_variants.append(merged_next)
        rows.append(
            {
                "row_index": row_index,
                "row_id": row.row_id,
                "raw_label": row.raw_item_name,
                "row_label": row_label_raw,
                "normalized_label": normalized_label,
                "row_variants": split_row_text_variants(
                    row_label_raw,
                    row.raw_line_text,
                    row.normalized_item_name,
                    *contextual_variants,
                ),
                "page_no": row.source_page,
                "cells": cells,
                "source_text": row.raw_line_text,
            }
        )
    return {"statement_type": table.statement_type, "unit": table.unit, "currency": table.currency, "unit_multiplier": get_unit_multiplier(table.unit), "header": header, "rows": rows, "parser_meta": table.parser_meta, "table": table}


def synthesize_cells_from_raw_line(
    statement_type: str,
    row_label_raw: str,
    normalized_label: str,
    raw_line_text: str,
    column_map: Dict[int, ColumnSchema],
) -> List[Dict]:
    """当几何分列失手时，从原始行文本中保守回填当前/上期数值列。"""
    if not raw_line_text:
        return []
    if is_low_information_row_label(row_label_raw or normalized_label):
        return []
    if is_structural_row_label(row_label_raw or normalized_label):
        return []

    tokens = extract_numeric_tokens(raw_line_text)
    if not (1 <= len(tokens) <= 2):
        return []

    amount_columns = [
        column
        for column in sorted(column_map.values(), key=lambda item: item.col_id)
        if normalize_text(column.role) in {"current_period", "previous_period"}
    ]
    if not amount_columns:
        return []

    synthesized: List[Dict] = []
    if len(tokens) == 1:
        target_column = next((column for column in amount_columns if normalize_text(column.role) == "current_period"), amount_columns[0])
        synthesized.append(
            {
                "column_index": target_column.col_id,
                "column_label": target_column.raw_name,
                "column_role": target_column.role,
                "value_text": normalize_text(tokens[0]),
            }
        )
        return synthesized

    role_order = ["current_period", "previous_period"]
    ordered_columns = []
    for role in role_order:
        ordered_columns.extend([column for column in amount_columns if normalize_text(column.role) == role])
    if not ordered_columns:
        ordered_columns = amount_columns

    for token, column in zip(tokens, ordered_columns):
        synthesized.append(
            {
                "column_index": column.col_id,
                "column_label": column.raw_name,
                "column_role": column.role,
                "value_text": normalize_text(token),
            }
        )
    return synthesized


def row_match_score(alias: str, row_label: str) -> float:
    alias_compact = normalize_item_name(normalize_row_label(alias))
    row_compact = normalize_item_name(normalize_row_label(row_label))
    return row_match_score_compact(alias_compact, row_compact)


def row_match_score_compact(alias_compact: str, row_compact: str) -> float:
    """使用已归一化文本打分，避免在同一张表内重复清洗相同别名和行变体。"""
    if not alias_compact or not row_compact:
        return 0.0
    if alias_compact == row_compact:
        return 100.0
    if row_compact.startswith(alias_compact) or alias_compact.startswith(row_compact):
        return 92.0 - abs(len(alias_compact) - len(row_compact))
    if alias_compact in row_compact or row_compact in alias_compact:
        return 86.0 - abs(len(alias_compact) - len(row_compact))
    return SequenceMatcher(None, alias_compact, row_compact).ratio() * 80.0


def find_row(field: Dict, table: Dict) -> List[Dict]:
    candidates = []
    field_code = normalize_text(field.get("field_code"))
    label_aliases = field.get("aliases", [])
    normalized_aliases = []
    seen_aliases = set()
    for alias in label_aliases:
        alias_compact = normalize_item_name(normalize_row_label(alias))
        if not alias_compact or alias_compact in seen_aliases:
            continue
        seen_aliases.add(alias_compact)
        normalized_aliases.append((alias, alias_compact))
    min_score = get_targeted_field_min_score(field_code)
    if min_score is None:
        min_score = FIELD_ROW_MATCH_MIN_SCORE.get(
            field_code,
            ROW_MATCH_MIN_SCORE.get(table.get("statement_type"), 68.0),
        )
    for row in table["rows"]:
        if not row["cells"] and not row.get("row_variants"):
            continue
        if not row["cells"] and is_low_information_row_label(row.get("row_label") or row.get("normalized_label") or ""):
            continue
        if not row["cells"] and is_structural_row_label(row.get("row_label") or row.get("normalized_label") or ""):
            continue
        best_score = 0.0
        best_alias = ""
        best_variant = row["normalized_label"] or row["row_label"]
        variants = row.get("row_variants") or [row["normalized_label"], row["row_label"]]
        normalized_variants = []
        seen_variants = set()
        for variant in variants:
            variant_compact = normalize_item_name(normalize_row_label(variant))
            if not variant_compact or variant_compact in seen_variants:
                continue
            seen_variants.add(variant_compact)
            normalized_variants.append((variant, variant_compact))
        for alias, alias_compact in normalized_aliases:
            for variant, variant_compact in normalized_variants:
                score = row_match_score_compact(alias_compact, variant_compact)
                if score > best_score:
                    best_score = score
                    best_alias = alias
                    best_variant = variant
        if best_score >= min_score:
            candidates.append({"row": row, "alias": best_alias, "score": best_score, "matched_variant": best_variant})
    return sorted(candidates, key=lambda item: item["score"], reverse=True)


def collect_field_diagnostics(field: Dict, table: Dict) -> List[Dict]:
    previews: List[Dict] = []
    preview_scores: List[Tuple[float, str, str]] = []
    aliases = field.get("aliases", [])[:5]
    for row in table["rows"]:
        variants = row.get("row_variants") or [row.get("normalized_label") or row.get("row_label") or ""]
        best_score = 0.0
        best_variant = ""
        for alias in aliases:
            for variant in variants:
                score = row_match_score(alias, variant)
                if score > best_score:
                    best_score = score
                    best_variant = normalize_row_label(variant)
        if best_score > 0:
            preview_scores.append((best_score, normalize_text(row.get("row_label")), best_variant))
    for score, raw_line, variant in sorted(preview_scores, reverse=True)[:DIAGNOSTIC_PREVIEW_LIMIT]:
        previews.append({"score": round(score, 4), "raw_line": raw_line, "normalized": variant})
    return previews


def table_has_compact_label(table: Dict, label: str) -> bool:
    target = compact_row_label(label)
    if not target:
        return False
    for row in table.get("rows", []):
        for text in row.get("row_variants") or [row.get("normalized_label"), row.get("row_label")]:
            compact_value = compact_row_label(text or "")
            if compact_value and (compact_value == target or target in compact_value):
                return True
    return False


def detect_source_row_absent_reason(field: Dict, table: Dict, previews: List[Dict]) -> str:
    """识别源表本身缺目标行的场景，仅用于诊断，不改变抽取结果。"""
    field_code = normalize_text(field.get("field_code"))
    if field_code != "cash_flow.operating_cf_cash_from_sales":
        return ""
    has_target_row = any(table_has_compact_label(table, alias) for alias in get_targeted_precise_aliases(field_code))
    if has_target_row:
        return ""
    marker_hits = sum(1 for marker in CASH_FLOW_SALES_ROW_ABSENT_MARKERS if table_has_compact_label(table, marker))
    financial_marker_hits = sum(1 for marker in CASH_FLOW_SALES_ROW_FINANCIAL_MARKERS if table_has_compact_label(table, marker))
    preview_text = " ".join(normalize_text(item.get("normalized") or item.get("raw_line")) for item in previews)
    preview_compact = compact_row_label(preview_text)
    has_purchase_or_subtotal_preview = any(
        compact_row_label(marker) in preview_compact
        for marker in ["购买商品、接受劳务支付的现金", "经营活动现金流入小计", "收到其他与经营活动有关的现金"]
    )
    if marker_hits >= 3 and (financial_marker_hits >= 2 or has_purchase_or_subtotal_preview):
        return SOURCE_ROW_ABSENT_REASON
    return ""


def build_not_found_diagnostic(field: Dict, table: Dict, row_matches: Optional[List[Dict]] = None, rejected_by_guardrail: bool = False) -> Dict:
    previews = collect_field_diagnostics(field, table)
    top_labels = [item["normalized"] or item["raw_line"] for item in previews[:DIAGNOSTIC_PREVIEW_LIMIT]]
    top_scores = [item["score"] for item in previews[:DIAGNOSTIC_PREVIEW_LIMIT]]
    candidate_stage_reached = bool(row_matches)
    field_entry_reached = bool(previews)
    alias_match_count = len(row_matches or [])
    source_row_absent_reason = detect_source_row_absent_reason(field, table, previews)
    if rejected_by_guardrail:
        not_found_reason = "guardrail_rejected"
    elif source_row_absent_reason:
        not_found_reason = source_row_absent_reason
    elif not field_entry_reached:
        not_found_reason = "entry_not_reached"
    else:
        not_found_reason = "alias_missing"
    return {
        "candidate_stage_reached": candidate_stage_reached,
        "field_entry_reached": field_entry_reached,
        "alias_match_count": alias_match_count,
        "top_candidate_labels": top_labels,
        "top_candidate_scores": top_scores,
        "rejected_by_guardrail": rejected_by_guardrail,
        "not_found_reason": not_found_reason,
        "source_row_absent_reason": source_row_absent_reason,
        "alias_sample": field.get("aliases", [])[:5],
    }


def format_not_found_preview(decision_logs: List[Dict], limit: int = 3) -> str:
    parts: List[str] = []
    for item in decision_logs:
        if item.get("failure_reason") != "not_found":
            continue
        labels = item.get("top_candidate_labels") or []
        scores = item.get("top_candidate_scores") or []
        if not labels:
            continue
        preview = ",".join(f"{label}:{scores[index]}" for index, label in enumerate(labels[: min(len(labels), len(scores), 2)]))
        if preview:
            reason = normalize_text(item.get("not_found_reason"))
            parts.append(f"{item.get('field_code')}<{reason}>[{preview}]")
        if len(parts) >= limit:
            break
    return " ; ".join(parts)


def find_column(table: Dict, statement_type: str, requested_label: Optional[str] = None, prefer_current: bool = True) -> Optional[int]:
    preferred_role = "current_period" if prefer_current else "previous_period"
    if requested_label:
        requested_compact = compact_text(requested_label)
        for header_item in table.get("header", []):
            header_label = compact_text(header_item.get("column_label"))
            if requested_compact and (requested_compact == header_label or requested_compact in header_label):
                return header_item["column_index"]
    for header_item in table.get("header", []):
        if normalize_text(header_item.get("column_role")) == preferred_role:
            return header_item["column_index"]
    alias_key = f"{statement_type}_{'current' if prefer_current else 'previous'}"
    for alias in COLUMN_LABEL_ALIASES.get(alias_key, []):
        alias_compact = compact_text(alias)
        for header_item in table.get("header", []):
            header_label = compact_text(header_item.get("column_label"))
            if alias_compact and (alias_compact == header_label or alias_compact in header_label or header_label in alias_compact):
                return header_item["column_index"]
    return None


def resolve_candidate_columns(
    table: Dict,
    statement_type: str,
    preferred_role: str,
    allow_previous_period_fallback: bool = False,
) -> Tuple[List[int], Dict]:
    """显式列角色策略：默认 current_period，按报表策略决定是否允许 previous_period。"""
    policy = COLUMN_ROLE_POLICY.get(statement_type, {"primary": "current_period", "allow_previous_fallback": False})
    primary_role = normalize_text(preferred_role or policy.get("primary")) or "current_period"
    fallback_allowed = bool(policy.get("allow_previous_fallback")) or bool(allow_previous_period_fallback)
    ordered_columns: List[int] = []
    primary_column = find_column(table=table, statement_type=statement_type, prefer_current=primary_role != "previous_period")
    fallback_column = None
    if fallback_allowed:
        fallback_column = find_column(table=table, statement_type=statement_type, prefer_current=primary_role == "previous_period")
    for column_index in [primary_column, fallback_column]:
        if column_index is not None and column_index not in ordered_columns:
            ordered_columns.append(column_index)
    return ordered_columns, {
        "primary_role": primary_role,
        "allow_previous_fallback": fallback_allowed,
        "fallback_requires_opt_in": False,
        "primary_column": primary_column,
        "fallback_column": fallback_column,
    }


def extract_cell(row: Dict, column_index: int) -> Optional[Dict]:
    for cell in row["cells"]:
        if cell["column_index"] == column_index:
            return cell
    return None


def get_header_item(table: Dict, column_index: int) -> Optional[Dict]:
    for header_item in table.get("header", []):
        if header_item.get("column_index") == column_index:
            return header_item
    return None


def has_non_target_role_value(row: Dict, table: Dict, primary_role: str) -> bool:
    """判断该行是否在非目标列角色上存在可用值。"""
    normalized_primary_role = normalize_text(primary_role)
    for cell in row.get("cells", []):
        if is_empty_cell_value(cell.get("value_text", "")):
            continue
        header_item = get_header_item(table, cell.get("column_index"))
        cell_role = normalize_text(cell.get("column_role") or (header_item or {}).get("column_role"))
        if cell_role and cell_role != normalized_primary_role:
            return True
    return False


def build_column_role_warning(
    row: Dict,
    table: Dict,
    cell: Optional[Dict],
    column_policy: Dict,
    used_fallback_role: bool,
) -> str:
    """仅记录列角色告警，不在默认链路中直接阻断写出。"""
    primary_role = column_policy.get("primary_role") or "current_period"
    if cell is not None:
        cell_role = normalize_text(cell.get("column_role"))
        if cell_role and cell_role != normalize_text(primary_role):
            return "only_non_target_column_role"
    if used_fallback_role:
        return "only_non_target_column_role"
    if has_non_target_role_value(row, table, primary_role):
        return "only_non_target_column_role"
    return ""


def resolve_row_cell(row: Dict, column_index: int, table: Dict) -> Tuple[Optional[Dict], str]:
    """优先取目标列；仅在表头标签重复且该行只有一个非空值时做极窄回退。"""
    cell = extract_cell(row, column_index)
    if cell is not None and not is_empty_cell_value(cell.get("value_text", "")):
        return cell, "preferred_column"

    non_empty_cells = [item for item in row["cells"] if not is_empty_cell_value(item.get("value_text", ""))]
    if len(non_empty_cells) != 1:
        return cell, "preferred_empty"

    fallback_cell = non_empty_cells[0]
    preferred_header = get_header_item(table, column_index)
    fallback_header = get_header_item(table, fallback_cell["column_index"])
    if preferred_header is None or fallback_header is None:
        return cell, "preferred_empty"
    if compact_text(preferred_header.get("column_label")) != compact_text(fallback_header.get("column_label")):
        return cell, "preferred_empty"
    return fallback_cell, "duplicate_header_single_value_fallback"


def json_dumps(data: Dict) -> str:
    return json.dumps(data, ensure_ascii=False)


def sanitize_json_debug_value(value):
    if isinstance(value, dict):
        return {normalize_text(key): sanitize_json_debug_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json_debug_value(item) for item in value]
    if isinstance(value, str):
        normalized = normalize_text(value)
        cleaned_chars = []
        for char in normalized:
            if char in {"\n", "\r", "\t"} or ord(char) >= 32:
                cleaned_chars.append(char)
        return "".join(cleaned_chars)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def normalize_csv_cell(value) -> str:
    return normalize_text(value) if value is not None else ""


def build_cell_debug_snapshot(cell: Optional[Dict]) -> Dict:
    if not cell:
        return {}
    return {
        "column_index": cell.get("column_index"),
        "column_label": normalize_text(cell.get("column_label")),
        "column_role": normalize_text(cell.get("column_role")),
        "value_text": normalize_text(cell.get("value_text")),
    }


def build_targeted_failure_record(payload: NormalizedTable, decision_item: Dict) -> Dict:
    record = {
        "file_id": payload.file_id,
        "statement_type": payload.statement_type,
        "field_code": normalize_text(decision_item.get("field_code")),
        "decision": normalize_text(decision_item.get("decision")),
        "failure_reason": normalize_text(decision_item.get("failure_reason")),
        "not_found_reason": normalize_text(decision_item.get("not_found_reason")),
        "source_row_absent_reason": normalize_text(decision_item.get("source_row_absent_reason")),
        "preview": build_issue_preview(decision_item),
        "best_row_score": decision_item.get("best_row_score"),
        "row_match_count": decision_item.get("row_match_count"),
        "candidate_stage_reached": decision_item.get("candidate_stage_reached"),
        "field_entry_reached": decision_item.get("field_entry_reached"),
        "alias_match_count": decision_item.get("alias_match_count"),
        "top_candidate_labels": decision_item.get("top_candidate_labels") or [],
        "top_candidate_scores": decision_item.get("top_candidate_scores") or [],
        "matched_alias": normalize_text(decision_item.get("matched_alias")),
        "matched_variant": normalize_text(decision_item.get("matched_variant")),
        "row_label": normalize_text(decision_item.get("row_label")),
        "normalized_row_label": normalize_text(decision_item.get("normalized_row_label")),
        "row_page": decision_item.get("row_page"),
        "row_source_text": normalize_text(decision_item.get("row_source_text")),
        "candidate_columns": decision_item.get("candidate_columns") or [],
        "primary_role": normalize_text(decision_item.get("primary_role")),
        "column_policy": decision_item.get("column_policy") or {},
        "cell_resolution": normalize_text(decision_item.get("cell_resolution")),
        "selected_cell": decision_item.get("selected_cell") or {},
        "preferred_cell": decision_item.get("preferred_cell") or {},
        "targeted_role_cell": decision_item.get("targeted_role_cell") or {},
        "numeric_fallback_cell": decision_item.get("numeric_fallback_cell") or {},
        "following_row_label": normalize_text(decision_item.get("following_row_label")),
        "following_row_page": decision_item.get("following_row_page"),
        "following_cell": decision_item.get("following_cell") or {},
        "value_override_text": normalize_text(decision_item.get("value_override_text")),
        "value_override_reason": normalize_text(decision_item.get("value_override_reason")),
        "raw_line_override_text": normalize_text(decision_item.get("raw_line_override_text")),
        "raw_line_override_reason": normalize_text(decision_item.get("raw_line_override_reason")),
        "raw_line_numeric_tokens": decision_item.get("raw_line_numeric_tokens") or [],
        "raw_value_text": normalize_text(decision_item.get("raw_value_text")),
        "scaled_value_text": normalize_text(decision_item.get("scaled_value_text")),
        "value_parse_status": normalize_text(decision_item.get("value_parse_status")),
        "alias_sample": decision_item.get("alias_sample") or [],
    }
    return sanitize_json_debug_value(record)


def build_candidate_failure_meta(candidate: Optional[Dict]) -> Dict:
    if not candidate:
        return {}
    extra_info = candidate.get("extra_info_json") or {}
    return {
        "matched_alias": normalize_text(extra_info.get("matched_alias")),
        "matched_variant": normalize_text(extra_info.get("matched_variant")),
        "row_label": normalize_text(candidate.get("raw_line_name")),
        "normalized_row_label": normalize_text(candidate.get("normalized_line_name")),
        "row_page": candidate.get("source_page"),
        "row_source_text": normalize_text(candidate.get("source_text")),
        "cell_resolution": normalize_text(extra_info.get("cell_resolution")),
        "selected_cell": {
            "column_label": normalize_text(candidate.get("column_label")),
            "column_role": normalize_text(candidate.get("source_column_role")),
            "value_text": normalize_text(candidate.get("value_text")),
        },
        "raw_value_text": normalize_text(candidate.get("value_text")),
        "scaled_value_text": normalize_text(candidate.get("value_text")),
        "value_parse_status": "candidate_available",
    }


def write_targeted_failure_debug_jsonl(summary_dir: Path, run_id: str, records: List[Dict]) -> Path:
    summary_dir.mkdir(parents=True, exist_ok=True)
    output_path = summary_dir / f"targeted_failures_{run_id}.jsonl"
    with output_path.open("w", encoding="utf-8") as file:
        for record in records:
            sanitized_record = sanitize_json_debug_value(record)
            file.write(json.dumps(sanitized_record, ensure_ascii=False, allow_nan=False) + "\n")
    return output_path


def run_git_command(args: List[str]) -> str:
    git_path = shutil.which("git")
    if not git_path:
        return ""
    try:
        completed = subprocess.run(
            [git_path, *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return normalize_text(completed.stdout)


def get_git_metadata() -> Dict[str, str]:
    return {
        "git_commit": run_git_command(["rev-parse", "HEAD"]),
        "branch": run_git_command(["rev-parse", "--abbrev-ref", "HEAD"]),
    }


def resolve_changed_files(changed_files_arg: str) -> str:
    changed_files = normalize_text(changed_files_arg)
    if changed_files:
        return changed_files
    return " ; ".join(
        item for item in run_git_command(["diff", "--name-only"]).splitlines() if normalize_text(item)
    )


def ensure_run_history_header(csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if csv_path.exists() and csv_path.stat().st_size > 0:
        return
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=RUN_HISTORY_COLUMNS)
        writer.writeheader()


def append_run_history(csv_path: Path, row: Dict) -> Path:
    normalized_row = {column: normalize_csv_cell(row.get(column, "")) for column in RUN_HISTORY_COLUMNS}
    try:
        ensure_run_history_header(csv_path)
        with csv_path.open("a", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=RUN_HISTORY_COLUMNS)
            writer.writerow(normalized_row)
        return csv_path
    except PermissionError:
        pending_dir = csv_path.parent / "logs"
        pending_dir.mkdir(parents=True, exist_ok=True)
        pending_path = pending_dir / f"run_history_pending_{normalize_text(row.get('run_id')) or 'unknown'}.csv"
        with pending_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=RUN_HISTORY_COLUMNS)
            writer.writeheader()
            writer.writerow(normalized_row)
        print(f"[运行记录待合并] path={pending_path} | reason=run_history.csv 被占用")
        return pending_path


def format_ratio_text(value: float) -> str:
    return f"{float(value or 0.0) * 100:.2f}%"


def format_counter_lines(counter_map: Dict[str, int], empty_text: str = "- 无") -> List[str]:
    lines: List[str] = []
    for key, value in sorted(counter_map.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key}: {value}")
    return lines or [empty_text]


def build_issue_preview(item: Dict) -> str:
    labels = item.get("top_candidate_labels") or []
    scores = item.get("top_candidate_scores") or []
    preview_parts = []
    for index, label in enumerate(labels[: min(len(labels), len(scores), 3)]):
        preview_parts.append(f"{label}:{scores[index]}")
    if preview_parts:
        return " | ".join(preview_parts)
    for key in ("semantic_guardrail_reason", "column_role_warning"):
        text = normalize_text(item.get(key))
        if text:
            return text
    return ""


def collect_issue_samples(file_id: int, statement_type: str, decision_logs: List[Dict], limit: int = 2) -> List[Dict]:
    samples: List[Dict] = []
    for item in decision_logs:
        reason = normalize_text(item.get("failure_reason"))
        if not reason:
            continue
        detail_reason = normalize_text(item.get("not_found_reason")) if reason == "not_found" else ""
        sample_reason = f"{reason}/{detail_reason}" if detail_reason else reason
        samples.append(
            {
                "report_id": str(file_id),
                "table": statement_type,
                "field": normalize_text(item.get("field_code")),
                "reason": sample_reason,
                "preview": build_issue_preview(item),
            }
        )
        if len(samples) >= limit:
            break
    return samples


def write_run_metrics_json(summary_dir: Path, run_id: str, payload: Dict) -> Path:
    summary_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = summary_dir / f"run_metrics_{run_id}.json"
    with metrics_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    return metrics_path


def build_run_summary_markdown(run_history_row: Dict, metrics_payload: Dict) -> str:
    changed_files = [item.strip() for item in normalize_text(run_history_row.get("changed_files")).split(";") if item.strip()]
    if not changed_files:
        changed_files = ["-"]

    issue_samples = metrics_payload.get("issue_samples") or []
    if issue_samples:
        sample_lines: List[str] = []
        for sample in issue_samples[:8]:
            sample_lines.extend(
                [
                    f"report_id={sample.get('report_id', '')}",
                    f"- table={sample.get('table', '')}",
                    f"- field={sample.get('field', '')}",
                    f"- reason={sample.get('reason', '')}",
                    f"- preview={sample.get('preview', '')}",
                    "",
                ]
            )
        if sample_lines and not sample_lines[-1]:
            sample_lines.pop()
    else:
        sample_lines = ["- 无"]

    statement_lines = format_counter_lines(metrics_payload.get("statement_type_summary", {}))
    zero_reason_lines = format_counter_lines(metrics_payload.get("zero_reason_summary", {}))
    upstream_issue_lines = format_counter_lines(metrics_payload.get("upstream_issue_stage_summary", {}))
    decision_lines = format_counter_lines(metrics_payload.get("decision_summary", {}))
    targeted_failure_debug_path = normalize_text(metrics_payload.get("targeted_failure_debug_path"))
    targeted_failure_debug_count = metrics_payload.get("targeted_failure_debug_count", 0)

    lines = [
        f"# Run Summary: {run_history_row.get('run_id', '')}",
        "",
        "## 1. 基本信息",
        f"- git_commit: {run_history_row.get('git_commit', '')}",
        f"- branch: {run_history_row.get('branch', '')}",
        f"- prompt_name: {run_history_row.get('prompt_name', '')}",
        f"- prompt_goal: {run_history_row.get('prompt_goal', '')}",
        f"- model_name: {run_history_row.get('model_name', '')}",
        f"- test_scope: {metrics_payload.get('test_scope', '')}",
        f"- test_files_count: {run_history_row.get('test_files_count', '')}",
        f"- run_time: {run_history_row.get('run_time', '')}",
        "",
        "## 2. 本轮修改文件",
        *[f"- {item}" for item in changed_files],
        "",
        "## 3. Pipeline 级统计",
        f"- success_count: {run_history_row.get('success_count', 0)}",
        f"- failed_count: {run_history_row.get('failed_count', 0)}",
        f"- empty_count: {run_history_row.get('empty_count', 0)}",
        f"- inserted_rows: {run_history_row.get('inserted_rows', 0)}",
        "",
        "## 4. 字段覆盖统计",
        f"- total_target_fields: {run_history_row.get('total_target_fields', 0)}",
        f"- non_empty_fields: {run_history_row.get('non_empty_fields', 0)}",
        f"- non_empty_rate: {format_ratio_text(float(run_history_row.get('non_empty_rate', 0) or 0))}",
        "",
        "## 5. 关键字段统计",
        f"- key_field_total: {run_history_row.get('key_field_total', 0)}",
        f"- key_field_hit: {run_history_row.get('key_field_hit', 0)}",
        f"- key_field_hit_rate: {format_ratio_text(float(run_history_row.get('key_field_hit_rate', 0) or 0))}",
        "",
        "## 6. 高风险统计",
        f"- high_risk_fill_count: {run_history_row.get('high_risk_fill_count', 0)}",
        f"- high_risk_fill_suspect_count: {run_history_row.get('high_risk_fill_suspect_count', 0)}",
        "",
        "## 7. 诊断拆分",
        f"- not_found_count: {run_history_row.get('not_found_count', 0)}",
        f"- alias_missing_count: {run_history_row.get('alias_missing_count', 0)}",
        f"- entry_missing_count: {run_history_row.get('entry_missing_count', 0)}",
        f"- semantic_unstable_count: {run_history_row.get('semantic_unstable_count', 0)}",
        f"- unexpected_error_count: {run_history_row.get('unexpected_error_count', 0)}",
        "",
        "## 8. 结构化结果",
        "- statement_type_summary:",
        *statement_lines,
        "- zero_reason_summary:",
        *zero_reason_lines,
        "- upstream_issue_stage_summary:",
        *upstream_issue_lines,
        "- decision_summary:",
        *decision_lines,
        f"- targeted_failure_debug_count: {targeted_failure_debug_count}",
        f"- targeted_failure_debug_path: {targeted_failure_debug_path}",
        "",
        "## 9. 典型错误样本",
        *sample_lines,
        "",
        "## 10. 本轮结论",
        f"- final_judgement: {run_history_row.get('final_judgement', '')}",
        f"- notes: {run_history_row.get('notes', '')}",
    ]
    return "\n".join(lines) + "\n"


def format_counter_lines(counter_map: Dict[str, int], empty_text: str = "- 无") -> List[str]:
    lines: List[str] = []
    for key, value in sorted(counter_map.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"- {key}: {value}")
    return lines or [empty_text]


def summarize_targeted_failure_hotspots(debug_path: str, limit: int = 8) -> List[Dict]:
    path = Path(normalize_text(debug_path))
    if not path.exists():
        return []

    field_pattern = re.compile(r'"field_code"\s*:\s*"([^"]+)"')
    reason_pattern = re.compile(r'"failure_reason"\s*:\s*"([^"]+)"')
    not_found_reason_pattern = re.compile(r'"not_found_reason"\s*:\s*"([^"]+)"')
    preview_pattern = re.compile(r'"preview"\s*:\s*"([^"]*)"')
    field_summary: Dict[str, Dict] = {}

    with path.open("r", encoding="utf-8") as file:
        for raw_line in file:
            line = normalize_text(raw_line)
            if not line:
                continue
            field_match = field_pattern.search(line)
            reason_match = reason_pattern.search(line)
            if not field_match:
                continue
            field_code = normalize_text(field_match.group(1))
            failure_reason = normalize_text(reason_match.group(1) if reason_match else "") or "unknown"
            not_found_reason_match = not_found_reason_pattern.search(line)
            detail_reason = normalize_text(not_found_reason_match.group(1) if not_found_reason_match else "")
            summary_reason = f"{failure_reason}/{detail_reason}" if detail_reason else failure_reason
            preview_match = preview_pattern.search(line)
            preview = normalize_text(preview_match.group(1) if preview_match else "")

            bucket = field_summary.setdefault(
                field_code,
                {"field_code": field_code, "count": 0, "reason_counter": {}, "sample_preview": ""},
            )
            bucket["count"] += 1
            bucket["reason_counter"][summary_reason] = bucket["reason_counter"].get(summary_reason, 0) + 1
            if preview and not bucket["sample_preview"]:
                bucket["sample_preview"] = preview

    hotspots: List[Dict] = []
    for item in field_summary.values():
        reason_items = sorted(item["reason_counter"].items(), key=lambda pair: (-pair[1], pair[0]))
        top_reason, top_reason_count = reason_items[0] if reason_items else ("unknown", 0)
        hotspots.append(
            {
                "field_code": item["field_code"],
                "count": item["count"],
                "top_reason": top_reason,
                "top_reason_count": top_reason_count,
                "sample_preview": item["sample_preview"],
            }
        )
    hotspots.sort(key=lambda item: (-item["count"], item["field_code"]))
    return hotspots[:limit]


def build_hotspot_lines(hotspots: List[Dict]) -> List[str]:
    if not hotspots:
        return ["- 无"]
    lines: List[str] = []
    for item in hotspots:
        lines.append(
            f"- {item['field_code']}: {item['count']} 次"
            f" | 主因={item['top_reason']}({item['top_reason_count']})"
        )
        if item.get("sample_preview"):
            lines.append(f"- 预览: {item['sample_preview']}")
    return lines


def build_priority_suggestion_lines(hotspots: List[Dict]) -> List[str]:
    if not hotspots:
        return ["- 当前无可用建议"]
    top_item = hotspots[0]
    field_code = top_item["field_code"]
    top_reason = top_item["top_reason"]
    if top_reason == "empty_value":
        action = "优先检查同名行取值链，重点看主列为空时的原行数字兜底和上下文取值。"
    elif SOURCE_ROW_ABSENT_REASON in top_reason:
        action = "优先确认源表是否属于金融、保险或非商品销售表式；若源表没有目标行，不应放宽别名去匹配小计或支付类行。"
    elif top_reason == "not_found":
        action = "优先检查别名召回与跨行拼接，重点看标题拼接、碎片行合并和黑名单是否过严。"
    elif top_reason == "weak_match":
        action = "优先检查候选过滤阈值与语义护栏，确认是否把正确候选误拒。"
    else:
        action = "优先检查该字段的失败样本，确认是召回、取值还是校验阶段丢失。"
    return [
        f"- 第一优先字段: {field_code}",
        f"- 依据: 最新日志中出现 {top_item['count']} 次，主因是 {top_reason}",
        f"- 修改建议: {action}",
    ]


def build_run_summary_markdown(run_history_row: Dict, metrics_payload: Dict) -> str:
    changed_files = [item.strip() for item in normalize_text(run_history_row.get("changed_files")).split(";") if item.strip()]
    if not changed_files:
        changed_files = ["-"]

    issue_samples = metrics_payload.get("issue_samples") or []
    if issue_samples:
        sample_lines: List[str] = []
        for sample in issue_samples[:8]:
            sample_lines.extend(
                [
                    f"report_id={sample.get('report_id', '')}",
                    f"- table={sample.get('table', '')}",
                    f"- field={sample.get('field', '')}",
                    f"- reason={sample.get('reason', '')}",
                    f"- preview={sample.get('preview', '')}",
                    "",
                ]
            )
        if sample_lines and not sample_lines[-1]:
            sample_lines.pop()
    else:
        sample_lines = ["- 无"]

    statement_lines = format_counter_lines(metrics_payload.get("statement_type_summary", {}))
    zero_reason_lines = format_counter_lines(metrics_payload.get("zero_reason_summary", {}))
    upstream_issue_lines = format_counter_lines(metrics_payload.get("upstream_issue_stage_summary", {}))
    decision_lines = format_counter_lines(metrics_payload.get("decision_summary", {}))
    targeted_failure_debug_path = normalize_text(metrics_payload.get("targeted_failure_debug_path"))
    targeted_failure_debug_count = metrics_payload.get("targeted_failure_debug_count", 0)
    hotspots = summarize_targeted_failure_hotspots(targeted_failure_debug_path)
    hotspot_lines = build_hotspot_lines(hotspots)
    priority_suggestion_lines = build_priority_suggestion_lines(hotspots)

    lines = [
        f"# Run Summary: {run_history_row.get('run_id', '')}",
        "",
        "## 1. 基本信息",
        f"- git_commit: {run_history_row.get('git_commit', '')}",
        f"- branch: {run_history_row.get('branch', '')}",
        f"- prompt_name: {run_history_row.get('prompt_name', '')}",
        f"- prompt_goal: {run_history_row.get('prompt_goal', '')}",
        f"- model_name: {run_history_row.get('model_name', '')}",
        f"- test_scope: {metrics_payload.get('test_scope', '')}",
        f"- test_files_count: {run_history_row.get('test_files_count', '')}",
        f"- run_time: {run_history_row.get('run_time', '')}",
        "",
        "## 2. 本轮修改文件",
        *[f"- {item}" for item in changed_files],
        "",
        "## 3. Pipeline 统计",
        f"- success_count: {run_history_row.get('success_count', 0)}",
        f"- failed_count: {run_history_row.get('failed_count', 0)}",
        f"- empty_count: {run_history_row.get('empty_count', 0)}",
        f"- inserted_rows: {run_history_row.get('inserted_rows', 0)}",
        "",
        "## 4. 字段覆盖统计",
        f"- total_target_fields: {run_history_row.get('total_target_fields', 0)}",
        f"- non_empty_fields: {run_history_row.get('non_empty_fields', 0)}",
        f"- non_empty_rate: {format_ratio_text(float(run_history_row.get('non_empty_rate', 0) or 0))}",
        "",
        "## 5. 关键字段统计",
        f"- key_field_total: {run_history_row.get('key_field_total', 0)}",
        f"- key_field_hit: {run_history_row.get('key_field_hit', 0)}",
        f"- key_field_hit_rate: {format_ratio_text(float(run_history_row.get('key_field_hit_rate', 0) or 0))}",
        "",
        "## 6. 高风险统计",
        f"- high_risk_fill_count: {run_history_row.get('high_risk_fill_count', 0)}",
        f"- high_risk_fill_suspect_count: {run_history_row.get('high_risk_fill_suspect_count', 0)}",
        "",
        "## 7. 诊断拆分",
        f"- not_found_count: {run_history_row.get('not_found_count', 0)}",
        f"- alias_missing_count: {run_history_row.get('alias_missing_count', 0)}",
        f"- entry_missing_count: {run_history_row.get('entry_missing_count', 0)}",
        f"- semantic_unstable_count: {run_history_row.get('semantic_unstable_count', 0)}",
        f"- unexpected_error_count: {run_history_row.get('unexpected_error_count', 0)}",
        "",
        "## 8. 结构化结果",
        "- statement_type_summary:",
        *statement_lines,
        "- zero_reason_summary:",
        *zero_reason_lines,
        "- upstream_issue_stage_summary:",
        *upstream_issue_lines,
        "- decision_summary:",
        *decision_lines,
        f"- targeted_failure_debug_count: {targeted_failure_debug_count}",
        f"- targeted_failure_debug_path: {targeted_failure_debug_path}",
        "",
        "## 9. 高频失败字段",
        *hotspot_lines,
        "",
        "## 10. 修改优先建议",
        *priority_suggestion_lines,
        "",
        "## 11. 典型错误样本",
        *sample_lines,
        "",
        "## 12. 本轮结论",
        f"- final_judgement: {run_history_row.get('final_judgement', '')}",
        f"- notes: {run_history_row.get('notes', '')}",
    ]
    return "\n".join(lines) + "\n"


def write_run_summary_markdown(summary_dir: Path, run_id: str, run_history_row: Dict, metrics_payload: Dict) -> Path:
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / f"run_summary_{run_id}.md"
    summary_text = build_run_summary_markdown(run_history_row, metrics_payload)
    with summary_path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(summary_text)
    return summary_path


def build_upstream_diagnostic(payload: NormalizedTable, table: Dict) -> Dict:
    """汇总 no_statement_rows 场景需要的上游定位诊断。"""
    pages = sorted(int(page) for page in (payload.pages or []) if page is not None)
    page_range = ""
    if pages:
        page_range = f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0])
    parser_meta = payload.parser_meta or {}
    locator_status = normalize_text(parser_meta.get("locator_status"))
    parser_failure_reason = normalize_text(parser_meta.get("failure_reason"))
    if parser_failure_reason == "no_rows_parsed":
        upstream_issue_stage = "parser_no_rows"
    elif locator_status and locator_status != "success":
        upstream_issue_stage = "locator_issue"
    else:
        upstream_issue_stage = "upstream_unknown"
    return {
        "statement_json_exists": True,
        "statement_type_in_json": normalize_text(payload.statement_type) == normalize_text(table.get("statement_type")),
        "locator_status": locator_status,
        "locator_method": normalize_text(parser_meta.get("locator_method")),
        "locator_confidence": float(payload.locator_confidence or 0.0),
        "locator_pages": page_range,
        "locator_page_count": len(pages),
        "parser_failure_reason": parser_failure_reason,
        "upstream_issue_stage": upstream_issue_stage,
    }


def format_upstream_diagnostic(summary: Dict) -> str:
    """压缩上游诊断，便于零结果日志直接判断问题层级。"""
    parts = [
        f"statement_json={1 if summary.get('statement_json_exists') else 0}",
        f"statement_type_in_json={1 if summary.get('statement_type_in_json') else 0}",
        f"upstream_stage={summary.get('upstream_issue_stage') or 'unknown'}",
        f"locator_status={summary.get('locator_status') or 'unknown'}",
        f"locator_confidence={round(float(summary.get('locator_confidence') or 0.0), 4)}",
        f"locator_pages={summary.get('locator_pages') or 'none'}",
        f"page_count={summary.get('locator_page_count') or 0}",
    ]
    parser_failure_reason = normalize_text(summary.get("parser_failure_reason"))
    if parser_failure_reason:
        parts.append(f"parser_failure={parser_failure_reason}")
    return ",".join(parts)


def choose_failure_reason(reasons: List[str], default_reason: str = "weak_match") -> str:
    for reason in FAILURE_REASON_PRIORITY:
        if reason in reasons:
            return reason
    return default_reason


def contains_semantic_token(texts: List[str], tokens: List[str]) -> bool:
    haystacks = [compact_row_label(text) for text in texts if compact_row_label(text)]
    for token in tokens:
        token_compact = compact_row_label(token)
        if not token_compact:
            continue
        for haystack in haystacks:
            if token_compact in haystack:
                return True
    return False


def semantic_alias_hit(field: Dict, candidate: Dict) -> bool:
    texts = [
        candidate.get("raw_line_name", ""),
        candidate.get("normalized_line_name", ""),
    ]
    if field["field_code"] not in TARGETED_FIELD_CODES:
        texts.append(candidate.get("source_text", ""))
    for alias in field.get("aliases", []):
        alias_compact = compact_row_label(alias)
        if not alias_compact:
            continue
        for text in texts:
            if alias_compact and alias_compact in compact_row_label(text):
                return True
    return False


def evaluate_semantic_guardrail(field: Dict, candidate: Dict) -> Tuple[bool, str]:
    rule = SEMANTIC_GUARDRAILS.get(field["field_code"])
    if not rule:
        return True, "not_configured"
    texts = [
        candidate.get("raw_line_name", ""),
        candidate.get("normalized_line_name", ""),
    ]
    if field["field_code"] not in TARGETED_FIELD_CODES:
        texts.append(candidate.get("source_text", ""))
    alias_hit = semantic_alias_hit(field, candidate)
    if rule.get("deny") and contains_semantic_token(texts, rule["deny"]):
        return False, "semantic_deny_token"
    if rule.get("allow") and not (contains_semantic_token(texts, rule["allow"]) or alias_hit):
        return False, "semantic_allow_token_missing"
    return True, "ok"


def filter_candidates_by_semantics(field: Dict, candidates: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    valid_candidates: List[Dict] = []
    blocked_candidates: List[Dict] = []
    for candidate in candidates:
        allowed, guardrail_reason = evaluate_semantic_guardrail(field, candidate)
        if allowed and candidate.get("extract_method") == RULE_CANDIDATE_FILL_METHOD and not is_strict_candidate_fill_allowed(field, candidate):
            allowed = False
            guardrail_reason = "candidate_fill_strict_guard"
        candidate["extra_info_json"]["semantic_guardrail"] = {"allowed": allowed, "reason": guardrail_reason}
        if allowed:
            valid_candidates.append(candidate)
        else:
            blocked_candidates.append(candidate)
    return valid_candidates, blocked_candidates


def violates_targeted_row_guardrail(field_code: str, matched_variant: str, row: Dict) -> bool:
    texts = [
        normalize_row_label(matched_variant),
        normalize_row_label(row.get("row_label") or ""),
        normalize_row_label(row.get("source_text") or ""),
    ]
    blocked, _ = should_reject_targeted_row(
        field_code=field_code,
        texts=texts,
        compact_row_label_fn=compact_row_label,
    )
    return blocked


def build_candidate_from_row(
    field: Dict,
    row_match: Dict,
    table: Dict,
    payload: NormalizedTable,
    allow_previous_period_fallback: bool = False,
) -> Tuple[Optional[Dict], str, Dict]:
    candidate_columns, column_policy = resolve_candidate_columns(
        table=table,
        statement_type=payload.statement_type,
        preferred_role=field.get("preferred_role") or "current_period",
        allow_previous_period_fallback=allow_previous_period_fallback,
    )
    row = row_match["row"]
    matched_variant = normalize_row_label(row_match.get("matched_variant") or row["normalized_label"] or row["row_label"])
    debug_info = {
        "matched_alias": row_match.get("alias"),
        "matched_variant": matched_variant,
        "row_match_score": round(float(row_match.get("score") or 0.0), 4),
        "row_label": row.get("row_label"),
        "normalized_row_label": row.get("normalized_label"),
        "row_page": row.get("page_no"),
        "row_source_text": row.get("source_text"),
        "candidate_columns": list(candidate_columns),
        "primary_role": column_policy.get("primary_role"),
        "column_policy": column_policy,
        "raw_line_numeric_tokens": extract_numeric_tokens(normalize_text(row.get("source_text") or row.get("raw_line_text") or "")),
    }
    if not candidate_columns:
        debug_info["value_parse_status"] = "no_candidate_columns"
        return None, "invalid_after_validation", debug_info
    value_row = row
    if violates_targeted_row_guardrail(field["field_code"], matched_variant, row):
        debug_info["value_parse_status"] = "targeted_blacklist_rejected"
        return None, "weak_match", debug_info
    if is_polluted_row_variant(matched_variant, row_match["alias"]):
        debug_info["value_parse_status"] = "polluted_row_variant"
        return None, "weak_match", debug_info
    cell = None
    cell_resolution = "preferred_empty"
    used_fallback_role = False
    value_override_text = ""
    value_override_reason = ""
    targeted_role_cell = None
    targeted_role_reason = ""
    numeric_fallback_cell = None
    numeric_fallback_reason = ""
    following_row = None
    following_cell = None
    following_reason = ""
    subtotal_row = None
    subtotal_cell = None
    subtotal_reason = ""
    for column_index in candidate_columns:
        current_cell, current_resolution = resolve_row_cell(row, column_index, table)
        if debug_info.get("preferred_cell") is None and current_cell is not None:
            debug_info["preferred_cell"] = build_cell_debug_snapshot(current_cell)
        if current_cell is not None and not is_empty_cell_value(current_cell.get("value_text", "")):
            cell = current_cell
            cell_resolution = current_resolution
            used_fallback_role = current_cell.get("column_role") != column_policy["primary_role"]
            break
        if cell is None and current_cell is not None:
            cell = current_cell
            cell_resolution = current_resolution
    if field["field_code"] in TARGETED_FIELD_CODES:
        targeted_role_cell, targeted_role_reason = resolve_targeted_primary_role_cell(
            row=row,
            candidate_columns=candidate_columns,
            table=table,
            primary_role=column_policy["primary_role"],
        )
        debug_info["targeted_role_cell"] = build_cell_debug_snapshot(targeted_role_cell)
        if targeted_role_cell is not None and (
            cell is None
            or is_empty_cell_value(cell.get("value_text", ""))
            or normalize_text(cell.get("column_role")) != normalize_text(column_policy["primary_role"])
        ):
            cell = dict(targeted_role_cell)
            cell_resolution = targeted_role_reason
            used_fallback_role = False
    if cell is None or is_empty_cell_value(cell.get("value_text", "")) or (
        parse_decimal(cell.get("value_text", "")) is None and not is_ratio_field(field["field_code"])
    ):
        numeric_fallback_cell, numeric_fallback_reason = resolve_numeric_cell_fallback(
            row=row,
            candidate_columns=candidate_columns,
            table=table,
            primary_role=column_policy["primary_role"],
        )
        debug_info["numeric_fallback_cell"] = build_cell_debug_snapshot(numeric_fallback_cell)
        if numeric_fallback_cell is not None:
            cell = dict(numeric_fallback_cell)
            cell_resolution = numeric_fallback_reason
            used_fallback_role = normalize_text(cell.get("column_role")) != normalize_text(column_policy["primary_role"])
    if cell is None or is_empty_cell_value(cell.get("value_text", "")) or (
        parse_decimal(cell.get("value_text", "")) is None and not is_ratio_field(field["field_code"])
    ):
        following_row, following_cell, following_reason = resolve_following_numeric_row_fallback(
            row=row,
            table=table,
            candidate_columns=candidate_columns,
            primary_role=column_policy["primary_role"],
        )
        debug_info["following_row_label"] = normalize_text((following_row or {}).get("row_label"))
        debug_info["following_row_page"] = (following_row or {}).get("page_no")
        debug_info["following_cell"] = build_cell_debug_snapshot(following_cell)
        if following_cell is not None:
            cell = dict(following_cell)
            value_row = following_row or row
            cell_resolution = following_reason
            used_fallback_role = normalize_text(cell.get("column_role")) != normalize_text(column_policy["primary_role"])
    if field["field_code"] in TARGETED_EMPTY_VALUE_FIELD_CODES and (
        cell is None
        or is_empty_cell_value(cell.get("value_text", ""))
        or (parse_decimal(cell.get("value_text", "")) is None and not is_ratio_field(field["field_code"]))
    ):
        subtotal_row, subtotal_cell, subtotal_reason = resolve_targeted_subtotal_fallback(
            field_code=field["field_code"],
            row=row,
            table=table,
            candidate_columns=candidate_columns,
            primary_role=column_policy["primary_role"],
        )
        debug_info["subtotal_row_label"] = normalize_text((subtotal_row or {}).get("row_label"))
        debug_info["subtotal_row_page"] = (subtotal_row or {}).get("page_no")
        debug_info["subtotal_cell"] = build_cell_debug_snapshot(subtotal_cell)
        if subtotal_cell is not None:
            cell = dict(subtotal_cell)
            value_row = subtotal_row or row
            cell_resolution = subtotal_reason
            used_fallback_role = normalize_text(cell.get("column_role")) != normalize_text(column_policy["primary_role"])
    if cell is None or is_empty_cell_value(cell.get("value_text", "")) or (
        parse_decimal(cell.get("value_text", "")) is None and not is_ratio_field(field["field_code"])
    ):
        value_override_text, value_override_reason = extract_row_context_numbers(
            row=row,
            payload=payload,
            matched_variant=matched_variant,
            primary_role=column_policy["primary_role"],
        )
        if (
            not value_override_text
            and field["field_code"] == "balance_sheet.liability_short_term_loans"
        ):
            value_override_text, _value_override_page, _matched_alias, value_override_reason = extract_targeted_page_text_numbers(
                field_code=field["field_code"],
                payload=payload,
                primary_role=column_policy["primary_role"],
            )
    debug_info["value_override_text"] = value_override_text
    debug_info["value_override_reason"] = value_override_reason
    raw_line_override_text = None
    raw_line_override_reason = ""
    if should_prioritize_raw_line_numeric(
        field_code=field["field_code"],
        cell=cell,
        cell_resolution=cell_resolution,
        primary_role=column_policy["primary_role"],
    ):
        raw_line_override_text, raw_line_override_reason = extract_raw_line_numeric_by_role(
            row=row,
            primary_role=column_policy["primary_role"],
        )
    debug_info["raw_line_override_text"] = raw_line_override_text
    debug_info["raw_line_override_reason"] = raw_line_override_reason
    if raw_line_override_text:
        if cell is None:
            cell = {
                "column_index": -1,
                "column_label": f"context:{raw_line_override_reason}",
                "column_role": column_policy["primary_role"],
                "value_text": raw_line_override_text,
            }
            cell_resolution = raw_line_override_reason
        else:
            cell["value_text"] = raw_line_override_text
            cell["column_label"] = f"context:{raw_line_override_reason}"
            cell["column_role"] = column_policy["primary_role"]
            cell_resolution = raw_line_override_reason
            used_fallback_role = False
    if cell is None:
        debug_info["cell_resolution"] = cell_resolution
        debug_info["selected_cell"] = build_cell_debug_snapshot(cell)
        debug_info["raw_value_text"] = ""
        debug_info["scaled_value_text"] = ""
        if value_override_text:
            cell = {
                "column_index": -1,
                "column_label": f"context:{value_override_reason}",
                "column_role": column_policy["primary_role"],
                "value_text": value_override_text,
            }
            cell_resolution = value_override_reason
        else:
            if has_non_target_role_value(row, table, column_policy["primary_role"]):
                debug_info["value_parse_status"] = "only_non_target_column_role"
                return None, "only_non_target_column_role", debug_info
            debug_info["value_parse_status"] = "empty_value"
            return None, "empty_value", debug_info
    if is_empty_cell_value(cell["value_text"]):
        debug_info["cell_resolution"] = cell_resolution
        debug_info["selected_cell"] = build_cell_debug_snapshot(cell)
        debug_info["raw_value_text"] = normalize_text(cell.get("value_text"))
        debug_info["scaled_value_text"] = ""
        if value_override_text:
            cell["value_text"] = value_override_text
            cell["column_label"] = f"context:{value_override_reason}"
            cell["column_role"] = column_policy["primary_role"]
            cell_resolution = value_override_reason
        else:
            if has_non_target_role_value(row, table, column_policy["primary_role"]):
                debug_info["value_parse_status"] = "only_non_target_column_role"
                return None, "only_non_target_column_role", debug_info
            debug_info["value_parse_status"] = "empty_value"
            return None, "empty_value", debug_info
    if parse_decimal(cell["value_text"]) is None and not is_ratio_field(field["field_code"]):
        debug_info["cell_resolution"] = cell_resolution
        debug_info["selected_cell"] = build_cell_debug_snapshot(cell)
        debug_info["raw_value_text"] = normalize_text(cell.get("value_text"))
        if value_override_text:
            cell["value_text"] = value_override_text
            cell["column_label"] = f"context:{value_override_reason}"
            cell["column_role"] = column_policy["primary_role"]
            cell_resolution = value_override_reason
        else:
            debug_info["scaled_value_text"] = ""
            debug_info["value_parse_status"] = "parse_decimal_none"
            return None, "invalid_after_validation", debug_info
    column_role_warning = build_column_role_warning(
        row=row,
        table=table,
        cell=cell,
        column_policy=column_policy,
        used_fallback_role=used_fallback_role,
    )
    scaled_value = scale_value_text(value_text=cell["value_text"], unit_multiplier=table["unit_multiplier"], is_ratio=is_ratio_field(field["field_code"]))
    numeric_value = parse_decimal(scaled_value)
    debug_info["cell_resolution"] = cell_resolution
    debug_info["selected_cell"] = build_cell_debug_snapshot(cell)
    debug_info["raw_value_text"] = normalize_text(cell.get("value_text"))
    debug_info["scaled_value_text"] = scaled_value
    debug_info["value_parse_status"] = "ok" if (numeric_value is not None or is_ratio_field(field["field_code"])) else "scaled_parse_decimal_none"
    if numeric_value is None and not is_ratio_field(field["field_code"]):
        return None, "invalid_after_validation", debug_info
    confidence = min(0.99, max(0.50, row_match["score"] / 100.0))
    return {
        "target_table": field["target_table"],
        "field_code": field["field_code"],
        "field_name_cn": field["field_name_cn"],
        "value_text": scaled_value,
        "numeric_value": numeric_value,
        "row_id": row["row_id"],
        "raw_line_name": row["row_label"],
        "normalized_line_name": matched_variant or row["normalized_label"],
        "source_page": value_row["page_no"],
        "source_column_role": cell["column_role"],
        "column_label": cell["column_label"],
        "unit": payload.unit or "元",
        "confidence": round(confidence, 4),
        "source_page_range": str(row["page_no"]) if row["page_no"] is not None else "",
        "source_text": (
            f"row_label={row['row_label']}\n"
            f"normalized_line_name={row['normalized_label']}\n"
            f"column_label={cell['column_label']}\n"
            f"column_role={cell['column_role']}\n"
            f"cell_value={cell['value_text']}\n"
            f"value_row_label={value_row['row_label']}\n"
            f"value_row_page={value_row['page_no']}\n"
            f"{value_row.get('source_text') or row.get('source_text') or ''}"
        ).strip(),
        "candidate_rank": 0,
        "candidate_score": round(confidence, 4),
        "extract_method": RULE_METHOD,
        "extra_info_json": {
            "statement_type": payload.statement_type,
            "unit": payload.unit,
            "currency": payload.currency,
            "locator_confidence": payload.locator_confidence,
            "parse_confidence": payload.parser_meta.get("parse_confidence"),
            "matched_alias": row_match["alias"],
            "matched_variant": matched_variant,
            "row_match_score": round(row_match["score"], 4),
            "cell_resolution": cell_resolution,
            "column_role_policy": column_policy,
            "used_fallback_role": used_fallback_role,
            "column_role_warning": column_role_warning,
            "allow_previous_period_fallback": bool(allow_previous_period_fallback),
            "value_source_row_id": value_row.get("row_id"),
            "value_source_row_label": value_row.get("row_label"),
            "fill_stage": "rule",
        },
    }, "", debug_info


def annotate_candidates(candidates: List[Dict], payload: NormalizedTable) -> List[Dict]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            -float(item.get("confidence") or 0.0),
            item.get("source_column_role") != "current_period",
            -float(payload.locator_confidence or 0.0),
        ),
    )
    for index, item in enumerate(ordered, start=1):
        item["candidate_rank"] = index
        item["candidate_score"] = round(float(item.get("confidence") or 0.0), 4)
    return ordered


def choose_rule_stage(candidates: List[Dict]) -> Tuple[str, Optional[Dict], Dict]:
    if not candidates:
        return "not_found", None, {}
    top1 = candidates[0]
    top2 = candidates[1] if len(candidates) >= 2 else None
    top1_confidence = float(top1.get("confidence") or 0.0)
    top2_confidence = float(top2.get("confidence") or 0.0) if top2 else 0.0
    gap = round(top1_confidence - top2_confidence, 4)
    meta = {
        "top1_confidence": round(top1_confidence, 4),
        "top2_confidence": round(top2_confidence, 4),
        "candidate_gap": gap,
        "candidate_count": len(candidates),
        "column_role": normalize_text(top1.get("source_column_role")),
        "column_role_warning": normalize_text((top1.get("extra_info_json") or {}).get("column_role_warning")),
    }
    if top1_confidence >= STRONG_RULE_MIN_CONFIDENCE and gap >= STRONG_RULE_MIN_GAP:
        return "rule", top1, meta
    if top1_confidence >= AUTO_FILL_MIN_CONFIDENCE and gap >= AUTO_FILL_MIN_GAP:
        return "candidate_fill", top1, meta
    return "weak_match", None, meta


def get_candidate_source_row_id(candidate: Dict) -> Optional[int]:
    """读取候选值来源行号，用于避免同页混入多张同类报表时跨段取值。"""
    extra_info = candidate.get("extra_info_json") or {}
    row_id = extra_info.get("value_source_row_id")
    if row_id is None:
        return None
    try:
        return int(row_id)
    except (TypeError, ValueError):
        return None


def align_balance_sheet_total_candidates(candidates_by_field: Dict[str, List[Dict]]) -> None:
    """让资产负债表合计类字段尽量来自同一段表格。"""
    asset_candidates = candidates_by_field.get("balance_sheet.asset_total_assets") or []
    if not asset_candidates:
        return
    ordered_asset_candidates = annotate_candidates(asset_candidates, NormalizedTable.empty())
    asset_row_id = get_candidate_source_row_id(ordered_asset_candidates[0])
    if asset_row_id is None:
        return
    for field_code in [
        "balance_sheet.liability_total_liabilities",
        "balance_sheet.equity_total_equity",
    ]:
        candidates = candidates_by_field.get(field_code) or []
        same_section_candidates = [
            candidate
            for candidate in candidates
            if (get_candidate_source_row_id(candidate) is None or get_candidate_source_row_id(candidate) >= asset_row_id)
        ]
        if same_section_candidates:
            candidates_by_field[field_code] = same_section_candidates


def is_exactish_alias_match(candidate: Dict) -> bool:
    extra_info = candidate.get("extra_info_json") or {}
    matched_alias = compact_row_label(extra_info.get("matched_alias") or "")
    field_code = normalize_text(candidate.get("field_code"))
    strict_rule = STRICT_CANDIDATE_FILL_RULES.get(field_code) or {}
    allowed_compacts = {
        compact_row_label(item)
        for item in strict_rule.get("allow_exact", [])
        if compact_row_label(item)
    }
    if not matched_alias:
        for text in [
            candidate.get("normalized_line_name", ""),
            extra_info.get("matched_variant", ""),
            candidate.get("raw_line_name", ""),
        ]:
            compact_value = compact_row_label(text)
            if compact_value and compact_value in allowed_compacts:
                return True
        return False
    for text in [
        candidate.get("normalized_line_name", ""),
        extra_info.get("matched_variant", ""),
        candidate.get("raw_line_name", ""),
    ]:
        compact_value = compact_row_label(text)
        if not compact_value:
            continue
        if compact_value == matched_alias:
            return True
        if compact_value in allowed_compacts:
            return True
        if compact_value.startswith(matched_alias) and len(compact_value) - len(matched_alias) <= 8:
            return True
    return False


def get_candidate_fill_rejection_reason(field: Dict, candidate: Dict) -> str:
    if not is_candidate_fill_allowed(field):
        return "candidate_fill_not_whitelisted"
    field_code = normalize_text(field.get("field_code"))
    if field_code in CANDIDATE_FILL_STRICT_FIELD_CODES and not is_exactish_alias_match(candidate):
        return "candidate_fill_alias_not_exact"
    extra_info = candidate.get("extra_info_json") or {}
    cell_resolution = normalize_text(extra_info.get("cell_resolution"))
    if field_code in CANDIDATE_FILL_STRICT_FIELD_CODES and cell_resolution in {"page_text_alias_current", "page_text_alias_previous"}:
        return "candidate_fill_alias_context_unstable"
    if field_code in CANDIDATE_FILL_STRICT_FIELD_CODES and cell_resolution in {"same_row_numeric_adjacent_fallback"}:
        return "candidate_fill_adjacent_column_unstable"
    if field_code in CANDIDATE_FILL_STRICT_FIELD_CODES and normalize_text(extra_info.get("column_role_warning")):
        return "candidate_fill_column_role_unstable"
    if float(candidate.get("confidence") or 0.0) < AUTO_FILL_MIN_CONFIDENCE:
        return "candidate_fill_low_confidence"
    if field_code in CANDIDATE_FILL_STRICT_FIELD_CODES and not is_strict_candidate_fill_allowed(field, candidate):
        return "candidate_fill_strict_guard"
    return ""


def fallback_to_rule_candidate(field: Dict, ordered: List[Dict], stage_meta: Dict) -> Tuple[Optional[Dict], str]:
    if not ordered:
        return None, "weak_match"
    top1 = ordered[0]
    top1_confidence = float(top1.get("confidence") or 0.0)
    if top1_confidence < AUTO_FILL_MIN_CONFIDENCE:
        return None, "weak_match"
    candidate_fill_rejection_reason = get_candidate_fill_rejection_reason(field, top1)
    if not candidate_fill_rejection_reason:
        return apply_selection_method(top1, RULE_CANDIDATE_FILL_METHOD, "candidate_fill_fallback", stage_meta), ""
    return apply_selection_method(top1, RULE_METHOD, "rule_fallback", stage_meta), ""


def apply_selection_method(candidate: Dict, method: str, fill_stage: str, extra_meta: Dict) -> Dict:
    row = dict(candidate)
    extra_info = dict(row.get("extra_info_json") or {})
    extra_info.update(extra_meta)
    extra_info["fill_stage"] = fill_stage
    row["extract_method"] = method
    row["extra_info_json"] = extra_info
    return row


def choose_best_candidates(
    payload: NormalizedTable,
    fields: List[Dict],
    candidates_by_field: Dict[str, List[Dict]],
    field_state_map: Dict[str, Dict],
) -> Tuple[List[Dict], List[Dict]]:
    final_rows: List[Dict] = []
    decision_logs: List[Dict] = []
    field_map = {field["field_code"]: field for field in fields}
    for field_code, candidates in candidates_by_field.items():
        field = field_map[field_code]
        state = field_state_map.setdefault(field_code, {})
        semantic_candidates, blocked_candidates = filter_candidates_by_semantics(field, candidates)
        state["blocked_candidate_count"] = len(blocked_candidates)
        if blocked_candidates:
            state.setdefault("failure_reasons", []).append("weak_match")
            state["semantic_guardrail_reason"] = blocked_candidates[0]["extra_info_json"].get("semantic_guardrail", {}).get("reason")
        ordered = annotate_candidates(semantic_candidates, payload)
        stage, selected_row, stage_meta = choose_rule_stage(ordered)
        if stage == "rule" and selected_row is not None:
            final_rows.append(apply_selection_method(selected_row, RULE_METHOD, "rule", stage_meta))
            decision_logs.append({"field_code": field_code, "decision": "rule", **stage_meta})
            continue
        if stage == "candidate_fill" and selected_row is not None:
            candidate_fill_rejection_reason = get_candidate_fill_rejection_reason(field, selected_row)
            if not candidate_fill_rejection_reason:
                final_rows.append(apply_selection_method(selected_row, RULE_CANDIDATE_FILL_METHOD, "candidate_fill", stage_meta))
                decision_logs.append({"field_code": field_code, "decision": "candidate_fill", **stage_meta})
            else:
                fallback_meta = dict(stage_meta)
                fallback_meta["candidate_fill_rejection_reason"] = candidate_fill_rejection_reason
                final_rows.append(apply_selection_method(selected_row, RULE_METHOD, "candidate_fill_rule_fallback", fallback_meta))
                decision_logs.append(
                    {
                        "field_code": field_code,
                        "decision": "candidate_fill_rejected",
                        "failure_reason": candidate_fill_rejection_reason,
                        "cell_resolution": normalize_text((selected_row.get("extra_info_json") or {}).get("cell_resolution")),
                        **stage_meta,
                    }
                )
            continue
        top_candidate_meta = build_candidate_failure_meta(ordered[0] if ordered else None)
        fallback_row, failure_reason = fallback_to_rule_candidate(field, ordered, stage_meta)
        if fallback_row is not None:
            final_rows.append(fallback_row)
            decision_logs.append({"field_code": field_code, "decision": "rule_candidate_fallback", **stage_meta})
        else:
            decision_logs.append({"field_code": field_code, "decision": "weak_match", "failure_reason": failure_reason, **stage_meta, **top_candidate_meta})
    return final_rows, decision_logs


def replace_results_for_file_and_table(conn, payload: NormalizedTable, rows: List[Dict], table_columns: set) -> None:
    file_id = payload.file_id
    target_table = STATEMENT_TARGET_TABLE_MAP[payload.statement_type]
    cur = conn.cursor()
    try:
        cur.execute(
            """
            DELETE FROM attachment3_extract_result
            WHERE file_id = %s
              AND target_table = %s
              AND extract_method = ANY(%s)
            """,
            (file_id, target_table, DELETE_METHODS),
        )
        if rows:
            insert_columns = ["file_id", "company_id", "stock_code", "stock_abbr", "report_year", "report_period", "target_table", "field_code", "field_name_cn", "value_text", "source_page_range", "source_text", "extract_method", "llm_status"]
            for column_name, _column_type in OPTIONAL_RESULT_COLUMNS:
                if column_name in table_columns:
                    insert_columns.append(column_name)
            insert_rows = []
            for row in rows:
                base = {
                    "file_id": payload.file_id,
                    "company_id": payload.company_id,
                    "stock_code": normalize_stock_code(payload.stock_code),
                    "stock_abbr": payload.stock_abbr,
                    "report_year": payload.report_year,
                    "report_period": payload.report_period,
                    "target_table": row["target_table"],
                    "field_code": row["field_code"],
                    "field_name_cn": row["field_name_cn"],
                    "value_text": row["value_text"],
                    "source_page_range": row["source_page_range"],
                    "source_text": row["source_text"],
                    "extract_method": row["extract_method"],
                    "llm_status": "pending",
                    "raw_line_name": row["raw_line_name"],
                    "normalized_line_name": row["normalized_line_name"],
                    "source_page": row["source_page"],
                    "source_column_role": row["source_column_role"],
                    "unit": row["unit"],
                    "confidence": row["confidence"],
                    "extra_info_json": json_dumps(row["extra_info_json"]),
                }
                insert_rows.append(tuple(base[column] for column in insert_columns))
            execute_values(cur, f"INSERT INTO attachment3_extract_result ({', '.join(insert_columns)}) VALUES %s", insert_rows, page_size=200)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


def process_single_json(
    conn,
    table_columns: set,
    json_path: Path,
    allow_previous_period_fallback: bool,
) -> Tuple[int, List[Dict], Dict]:
    payload = load_normalized_table_json(json_path)
    statement_type = payload.statement_type
    if statement_type not in STATEMENT_TARGET_TABLE_MAP:
        raise ValueError(f"不支持的 statement_type：{statement_type}")
    target_table = STATEMENT_TARGET_TABLE_MAP[statement_type]
    fields = fetch_field_dict(conn, target_table)
    table = parse_table_structure(payload)
    candidates_by_field: Dict[str, List[Dict]] = {}
    field_state_map: Dict[str, Dict] = {}
    early_decision_logs: List[Dict] = []
    non_empty_statement_rows = sum(1 for row in table["rows"] if any(not is_empty_cell_value(cell.get("value_text", "")) for cell in row["cells"]))
    upstream_diagnostic = build_upstream_diagnostic(payload, table)
    if non_empty_statement_rows == 0:
        replace_results_for_file_and_table(conn, payload, [], table_columns)
        summary = {
            "zero_reason": "no_statement_rows",
            "non_empty_statement_rows": 0,
            "field_candidate_count": 0,
            "not_found_fields": 0,
            "only_non_target_role_fields": 0,
            **upstream_diagnostic,
        }
        return 0, [], summary
    for field in fields:
        if should_skip_statement_field(field["field_code"]):
            continue
        field_state = {"field_code": field["field_code"], "failure_reasons": []}
        row_matches = find_row(field, table)
        if not row_matches:
            alias_context_candidate = build_candidate_from_alias_context(field, table, payload)
            if alias_context_candidate is not None:
                candidates_by_field[field["field_code"]] = [alias_context_candidate]
                field_state_map[field["field_code"]] = field_state
                continue
            targeted_page_text_candidate = build_candidate_from_targeted_page_text(field, table, payload)
            if targeted_page_text_candidate is not None:
                candidates_by_field[field["field_code"]] = [targeted_page_text_candidate]
                field_state_map[field["field_code"]] = field_state
                continue
            targeted_block_text_candidate = build_candidate_from_targeted_block_text(field, table, payload)
            if targeted_block_text_candidate is not None:
                candidates_by_field[field["field_code"]] = [targeted_block_text_candidate]
                field_state_map[field["field_code"]] = field_state
                continue
            diagnostic = build_not_found_diagnostic(field, table, row_matches=None, rejected_by_guardrail=False)
            early_decision_logs.append({"field_code": field["field_code"], "decision": "not_found", "failure_reason": "not_found", **diagnostic})
            continue
        field_state["row_match_count"] = len(row_matches)
        field_state["best_row_score"] = round(float(row_matches[0]["score"]), 4)
        field_candidates = []
        for row_match in row_matches[:MAX_ROW_MATCHES]:
            candidate, reject_reason, reject_debug = build_candidate_from_row(
                field,
                row_match,
                table,
                payload,
                allow_previous_period_fallback=allow_previous_period_fallback,
            )
            if candidate is not None:
                field_candidates.append(candidate)
            elif reject_reason:
                field_state["failure_reasons"].append(reject_reason)
                if reject_debug:
                    field_state.setdefault("reject_debug", []).append(reject_debug)
        if field_candidates:
            candidates_by_field[field["field_code"]] = field_candidates
            field_state_map[field["field_code"]] = field_state
        else:
            alias_context_candidate = build_candidate_from_alias_context(field, table, payload)
            if alias_context_candidate is not None:
                candidates_by_field[field["field_code"]] = [alias_context_candidate]
                field_state_map[field["field_code"]] = field_state
                continue
            targeted_page_text_candidate = build_candidate_from_targeted_page_text(field, table, payload)
            if targeted_page_text_candidate is not None:
                candidates_by_field[field["field_code"]] = [targeted_page_text_candidate]
                field_state_map[field["field_code"]] = field_state
                continue
            targeted_block_text_candidate = build_candidate_from_targeted_block_text(field, table, payload)
            if targeted_block_text_candidate is not None:
                candidates_by_field[field["field_code"]] = [targeted_block_text_candidate]
                field_state_map[field["field_code"]] = field_state
                continue
            failure_reason = choose_failure_reason(field_state["failure_reasons"], "invalid_after_validation")
            diagnostic = build_not_found_diagnostic(
                field,
                table,
                row_matches=row_matches,
                rejected_by_guardrail=bool(field_state.get("semantic_guardrail_reason")),
            )
            early_decision_logs.append(
                {
                    "field_code": field["field_code"],
                    "decision": failure_reason,
                    "failure_reason": failure_reason,
                    "best_row_score": field_state.get("best_row_score", 0.0),
                    "row_match_count": field_state.get("row_match_count", 0),
                    **(
                        sorted(
                            field_state.get("reject_debug") or [],
                            key=lambda item: float(item.get("row_match_score") or 0.0),
                            reverse=True,
                        )[0]
                        if field_state.get("reject_debug")
                        else {}
                    ),
                    **diagnostic,
                }
            )
    selected_rows, decision_logs = choose_best_candidates(
        payload=payload,
        fields=fields,
        candidates_by_field=candidates_by_field,
        field_state_map=field_state_map,
    )
    decision_logs = early_decision_logs + decision_logs
    replace_results_for_file_and_table(conn, payload, selected_rows, table_columns)
    for row in selected_rows:
        print(
            f"[抽取] file_id={payload.file_id} | statement_type={payload.statement_type} | "
            f"field_code={row['field_code']} | method={row['extract_method']} | "
            f"raw_line={row['raw_line_name']} | normalized_line={row['normalized_line_name']} | column_role={row['source_column_role']} | "
            f"value={row['value_text']} | page={row['source_page']} | confidence={row['confidence']}"
        )
    if selected_rows:
        zero_reason = ""
    elif non_empty_statement_rows == 0:
        zero_reason = "no_statement_rows"
    elif not candidates_by_field:
        zero_reason = "no_field_candidates"
    elif decision_logs and all(item.get("failure_reason") == "only_non_target_column_role" for item in decision_logs if item.get("failure_reason")):
        zero_reason = "only_non_target_column_role"
    elif any(item.get("failure_reason") == "invalid_after_validation" for item in decision_logs):
        zero_reason = "validation_rejected"
    else:
        zero_reason = "all_candidates_rejected"
    summary = {
        "zero_reason": zero_reason,
        "non_empty_statement_rows": non_empty_statement_rows,
        "field_candidate_count": len(candidates_by_field),
        "not_found_fields": sum(1 for item in decision_logs if item.get("failure_reason") == "not_found"),
        "total_target_fields": sum(1 for field in fields if not should_skip_statement_field(field["field_code"])),
        "non_empty_fields": len(selected_rows),
        "key_field_total": sum(
            1
            for field in fields
            if not should_skip_statement_field(field["field_code"])
            and field["field_code"] in KEY_FIELD_CODE_MAP.get(statement_type, set())
        ),
        "key_field_hit": sum(1 for row in selected_rows if row.get("field_code") in KEY_FIELD_CODE_MAP.get(statement_type, set())),
        "high_risk_fill_count": sum(
            1
            for row in selected_rows
            if row.get("field_code") in HIGH_RISK_FIELD_CODES
            and row.get("extract_method") == RULE_CANDIDATE_FILL_METHOD
        ),
        "high_risk_fill_suspect_count": sum(
            1
            for row in selected_rows
            if row.get("field_code") in HIGH_RISK_FIELD_CODES
            and row.get("extract_method") == RULE_CANDIDATE_FILL_METHOD
            and (
                float(row.get("confidence") or 0.0) < STRONG_RULE_MIN_CONFIDENCE
                or normalize_text((row.get("extra_info_json") or {}).get("column_role_warning"))
            )
        ),
        "alias_missing_count": sum(
            1
            for item in decision_logs
            if item.get("failure_reason") == "not_found" and item.get("not_found_reason") == "alias_missing"
        ),
        "entry_missing_count": sum(
            1
            for item in decision_logs
            if item.get("failure_reason") == "not_found" and item.get("not_found_reason") == "entry_not_reached"
        ),
        "semantic_unstable_count": sum(
            1
            for item in decision_logs
            if normalize_text(item.get("semantic_guardrail_reason")) or normalize_text(item.get("not_found_reason")) == "guardrail_rejected"
        ),
        "only_non_target_role_fields": sum(
            1
            for item in decision_logs
            if item.get("failure_reason") == "only_non_target_column_role"
            or item.get("column_role_warning") == "only_non_target_column_role"
        ),
        **upstream_diagnostic,
    }
    return len(selected_rows), decision_logs, summary


def parse_args():
    parser = argparse.ArgumentParser(description="基于 normalized table json 执行规则抽取和候选补全。")
    parser.add_argument("--file-id", type=int, nargs="*", help="仅处理指定 file_id，可传多个。")
    parser.add_argument("--statement-type", choices=list(STATEMENT_TARGET_TABLE_MAP.keys()), nargs="*", help="仅处理指定报表类型。")
    parser.add_argument("--limit", type=int, help="最多处理前 N 个 JSON 文件。")
    parser.add_argument("--allow-previous-period-fallback", action="store_true", help="显式允许在 current_period 缺失时回退 previous_period。默认关闭。")
    parser.add_argument("--run-id", help="本轮运行唯一编号，例如 20260416_r07。")
    parser.add_argument("--prompt-name", default="", help="本轮 prompt 名称。")
    parser.add_argument("--prompt-goal", default="", help="本轮 prompt 目标。")
    parser.add_argument("--model-name", default="", help="本轮使用模型名称。")
    parser.add_argument("--changed-files", default="", help="本轮实际修改文件，多个可用分号分隔。")
    parser.add_argument("--final-judgement", choices=["better", "same", "worse"], default="", help="本轮结论。")
    parser.add_argument("--notes", default="", help="本轮一句话结论。")
    parser.add_argument("--run-history-path", default=str(RUN_HISTORY_PATH), help="run_history.csv 输出路径。")
    return parser.parse_args()


def main():
    args = parse_args()
    json_files = list_statement_json_files(file_ids=args.file_id, statement_types=args.statement_type, limit=args.limit)
    print(f"待处理标准化报表 JSON 数：{len(json_files)}")
    run_started_at = datetime.now()
    run_id = normalize_text(args.run_id) or run_started_at.strftime("%Y%m%d_r%H%M%S")
    watchdog_seconds = get_extract_watchdog_seconds()
    watchdog_path = build_watchdog_path(run_id)
    if watchdog_seconds > 0:
        print(f"[watchdog] enabled=True | seconds={watchdog_seconds} | stack_path={watchdog_path}", flush=True)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        table_columns = ensure_extract_result_schema(conn)
        success_count = 0
        fail_count = 0
        total_rows = 0
        empty_count = 0
        decision_summary: Dict[str, int] = {}
        failure_summary: Dict[str, int] = {}
        zero_reason_summary: Dict[str, int] = {}
        upstream_issue_stage_summary: Dict[str, int] = {}
        statement_type_summary: Dict[str, int] = {}
        issue_samples: List[Dict] = []
        targeted_failure_records: List[Dict] = []
        run_aggregate = {
            "total_target_fields": 0,
            "non_empty_fields": 0,
            "key_field_total": 0,
            "key_field_hit": 0,
            "high_risk_fill_count": 0,
            "high_risk_fill_suspect_count": 0,
            "not_found_count": 0,
            "alias_missing_count": 0,
            "entry_missing_count": 0,
            "semantic_unstable_count": 0,
        }
        for json_index, json_path in enumerate(json_files, start=1):
            parsed_for_progress = parse_json_name(json_path.name) or {}
            print(
                f"[开始抽取JSON] {json_index}/{len(json_files)} | "
                f"file_id={parsed_for_progress.get('file_id')} | "
                f"statement_type={parsed_for_progress.get('statement_type')} | json={json_path.name}",
                flush=True,
            )
            try:
                if watchdog_seconds > 0:
                    with watchdog_path.open("a", encoding="utf-8", newline="\n") as watchdog_file:
                        watchdog_file.write(
                            f"\n[watchdog-arm] run_id={run_id} | index={json_index}/{len(json_files)} | "
                            f"json={json_path.name}\n"
                        )
                        watchdog_file.flush()
                        faulthandler.dump_traceback_later(
                            watchdog_seconds,
                            repeat=False,
                            file=watchdog_file,
                            exit=True,
                        )
                        try:
                            payload = load_normalized_table_json(json_path)
                            row_count, decision_logs, summary = process_single_json(
                                conn=conn,
                                table_columns=table_columns,
                                json_path=json_path,
                                allow_previous_period_fallback=bool(args.allow_previous_period_fallback),
                            )
                        finally:
                            faulthandler.cancel_dump_traceback_later()
                else:
                    payload = load_normalized_table_json(json_path)
                    row_count, decision_logs, summary = process_single_json(
                        conn=conn,
                        table_columns=table_columns,
                        json_path=json_path,
                        allow_previous_period_fallback=bool(args.allow_previous_period_fallback),
                    )
                for item in decision_logs:
                    decision = normalize_text(item.get("decision"))
                    decision_summary[decision] = decision_summary.get(decision, 0) + 1
                    failure_reason = normalize_text(item.get("failure_reason"))
                    if failure_reason:
                        failure_summary[failure_reason] = failure_summary.get(failure_reason, 0) + 1
                        field_code = normalize_text(item.get("field_code"))
                        if field_code in LONGTAIL_CLEANUP_FIELD_CODES:
                            targeted_failure_records.append(build_targeted_failure_record(payload, item))
                statement_key = normalize_text(payload.statement_type) or "unknown"
                statement_type_summary[statement_key] = statement_type_summary.get(statement_key, 0) + 1
                success_count += 1
                total_rows += row_count
                if row_count == 0:
                    empty_count += 1
                zero_reason = normalize_text(summary.get("zero_reason"))
                if zero_reason:
                    zero_reason_summary[zero_reason] = zero_reason_summary.get(zero_reason, 0) + 1
                if zero_reason == "no_statement_rows":
                    upstream_issue_stage = normalize_text(summary.get("upstream_issue_stage")) or "upstream_unknown"
                    upstream_issue_stage_summary[upstream_issue_stage] = upstream_issue_stage_summary.get(upstream_issue_stage, 0) + 1
                if len(issue_samples) < 12:
                    issue_samples.extend(collect_issue_samples(payload.file_id, payload.statement_type, decision_logs, limit=2))
                    issue_samples = issue_samples[:12]
                for metric_name in run_aggregate:
                    run_aggregate[metric_name] += int(summary.get(metric_name, 0) or 0)
                success_line = (
                    f"[成功] file_id={payload.file_id} | statement_type={payload.statement_type} | "
                    f"target_table={STATEMENT_TARGET_TABLE_MAP[payload.statement_type]} | extract_rows={row_count}"
                )
                if row_count == 0 and summary.get("zero_reason"):
                    not_found_preview = format_not_found_preview(decision_logs)
                    success_line += (
                        f" | zero_reason={summary['zero_reason']} | "
                        f"statement_rows={summary.get('non_empty_statement_rows', 0)} | "
                        f"field_candidates={summary.get('field_candidate_count', 0)} | "
                        f"not_found_fields={summary.get('not_found_fields', 0)} | "
                        f"only_non_target_role_fields={summary.get('only_non_target_role_fields', 0)}"
                    )
                    if summary.get("zero_reason") == "no_statement_rows":
                        success_line += f" | upstream_diag={format_upstream_diagnostic(summary)}"
                    if not_found_preview:
                        success_line += f" | not_found_preview={not_found_preview}"
                print(success_line)
            except Exception as error:
                fail_count += 1
                parsed = parse_json_name(json_path.name) or {}
                print(f"[失败] file_id={parsed.get('file_id')} | statement_type={parsed.get('statement_type')} | json={json_path.name} | error={error}")
        for key in sorted(decision_summary.keys()):
            print(f"[决策统计] decision={key} | count={decision_summary[key]}")
        for key in FAILURE_REASON_PRIORITY:
            if key in failure_summary:
                print(f"[失败分类统计] reason={key} | count={failure_summary[key]}")
        print(f"处理完成：成功 {success_count} 个，失败 {fail_count} 个，共写入 {total_rows} 条规则类抽取结果。")
        git_metadata = get_git_metadata()
        model_name = normalize_text(args.model_name) or "rule_only"
        run_history_row = {
            "run_id": run_id,
            "run_time": run_started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "git_commit": git_metadata.get("git_commit", ""),
            "branch": git_metadata.get("branch", ""),
            "prompt_name": args.prompt_name,
            "prompt_goal": args.prompt_goal,
            "model_name": model_name,
            "test_files_count": len(json_files),
            "changed_files": resolve_changed_files(args.changed_files),
            "success_count": success_count,
            "failed_count": fail_count,
            "empty_count": empty_count,
            "inserted_rows": total_rows,
            "total_target_fields": run_aggregate["total_target_fields"],
            "non_empty_fields": run_aggregate["non_empty_fields"],
            "non_empty_rate": safe_ratio(run_aggregate["non_empty_fields"], run_aggregate["total_target_fields"]),
            "key_field_total": run_aggregate["key_field_total"],
            "key_field_hit": run_aggregate["key_field_hit"],
            "key_field_hit_rate": safe_ratio(run_aggregate["key_field_hit"], run_aggregate["key_field_total"]),
            "high_risk_fill_count": run_aggregate["high_risk_fill_count"],
            "high_risk_fill_suspect_count": run_aggregate["high_risk_fill_suspect_count"],
            "not_found_count": run_aggregate["not_found_count"],
            "alias_missing_count": run_aggregate["alias_missing_count"],
            "entry_missing_count": run_aggregate["entry_missing_count"],
            "semantic_unstable_count": run_aggregate["semantic_unstable_count"],
            "unexpected_error_count": fail_count,
            "final_judgement": args.final_judgement,
            "notes": args.notes,
        }
        run_history_path = Path(args.run_history_path)
        actual_run_history_path = append_run_history(run_history_path, run_history_row)
        print(f"[运行记录] path={actual_run_history_path} | run_id={run_id}")
        metrics_payload = {
            "run_id": run_id,
            "run_time": run_started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "test_scope": "extract_attachment3_rule_based",
            "statement_type_summary": statement_type_summary,
            "zero_reason_summary": zero_reason_summary,
            "upstream_issue_stage_summary": upstream_issue_stage_summary,
            "decision_summary": decision_summary,
            "failure_summary": failure_summary,
            "issue_samples": issue_samples,
            "run_aggregate": run_aggregate,
        }
        targeted_failure_path = write_targeted_failure_debug_jsonl(
            TARGETED_FAILURE_DEBUG_DIR,
            run_id,
            targeted_failure_records,
        )
        metrics_payload["targeted_failure_debug_path"] = str(targeted_failure_path)
        metrics_payload["targeted_failure_debug_count"] = len(targeted_failure_records)
        metrics_path = write_run_metrics_json(RUN_SUMMARY_DIR, run_id, metrics_payload)
        summary_path = write_run_summary_markdown(RUN_SUMMARY_DIR, run_id, run_history_row, metrics_payload)
        print(f"[运行指标] path={metrics_path}")
        print(f"[运行摘要] path={summary_path}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
