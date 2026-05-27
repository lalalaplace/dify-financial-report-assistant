from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
import os
import re

from api.chart import CHART_OUTPUT_DIR, router as chart_router

load_dotenv()

app = FastAPI(title="Dify PostgreSQL Query API")
CHART_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/charts", StaticFiles(directory=str(CHART_OUTPUT_DIR)), name="charts")
app.include_router(chart_router)

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "dbname": os.getenv("PG_DBNAME", "financial_reports"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

ALLOWED_TABLES = {
    "company_dim",
    "company_alias",
    "balance_sheet",
    "income_sheet",
    "cash_flow_sheet",
}

FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter", "truncate", "create",
    "grant", "revoke", "merge"
]

class QueryRequest(BaseModel):
    sql: str
    question: str | None = None
    query_mode: str | None = None

def validate_sql(sql: str) -> None:
    s = (sql or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="SQL 为空")

    s_lower = s.lower()

    if not s_lower.startswith("select"):
        raise HTTPException(status_code=400, detail="只允许 SELECT 查询")

    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", s_lower):
            raise HTTPException(status_code=400, detail=f"SQL 包含禁用关键字: {kw}")

    if ";" in s_lower[:-1]:
        raise HTTPException(status_code=400, detail="SQL 疑似包含多条语句")

    table_matches = re.findall(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", s_lower)
    for table_name in table_matches:
        if table_name not in ALLOWED_TABLES:
            raise HTTPException(status_code=400, detail=f"SQL 使用了未授权表: {table_name}")

def execute_sql(sql: str):
    conn = None
    cur = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql)
        rows = cur.fetchall()
        columns = list(rows[0].keys()) if rows else []
        return columns, rows
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@app.get("/health")
def health():
    return {"success": True, "message": "ok"}

@app.post("/query")
def query_db(req: QueryRequest):
    validate_sql(req.sql)

    try:
        columns, rows = execute_sql(req.sql)
        return {
            "success": True,
            "columns": columns,
            "rows": [list(r.values()) for r in rows],
            "row_count": len(rows),
            "message": "",
            "data_preview": rows[:20],
            "question": req.question,
            "query_mode": req.query_mode,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
