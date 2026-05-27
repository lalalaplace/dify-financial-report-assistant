import os
import re
import uuid
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHART_OUTPUT_DIR = Path(os.getenv("CHART_OUTPUT_DIR", PROJECT_ROOT / "output" / "charts"))
CHART_PUBLIC_DIR = Path("output") / "charts"
SUPPORTED_CHART_TYPES = {"bar", "line", "pie", "table"}

router = APIRouter()


class ChartRequest(BaseModel):
    question: str | None = None
    chart_type: str = "bar"
    title: str | None = None
    x_field: str | None = None
    y_field: str | None = None
    columns: list[str] | None = None
    rows: list[list[Any]] | None = None
    records: list[dict[str, Any]] | None = None
    chart_request: dict[str, Any] | None = Field(default=None, description="兼容 Dify 传入的嵌套图表配置。")


def normalize_chart_type(chart_type: str | None) -> str:
    value = (chart_type or "bar").strip().lower()
    if value not in SUPPORTED_CHART_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的图表类型: {chart_type}")
    return value


def safe_filename(title: str | None) -> str:
    prefix = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "_", title or "chart").strip("_")
    if not prefix:
        prefix = "chart"
    return f"{prefix[:40]}_{uuid.uuid4().hex[:8]}.png"


def merge_chart_payload(req: ChartRequest) -> dict[str, Any]:
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    nested = payload.pop("chart_request") or {}
    if not isinstance(nested, dict):
        raise HTTPException(status_code=400, detail="chart_request 必须是对象")
    for key, value in nested.items():
        if payload.get(key) in (None, [], ""):
            payload[key] = value
    return payload


def records_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = payload.get("records")
    if records:
        if not all(isinstance(item, dict) for item in records):
            raise HTTPException(status_code=400, detail="records 必须是对象数组")
        return records

    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    if not columns or not rows:
        raise HTTPException(status_code=400, detail="缺少图表数据，请提供 records 或 columns + rows")
    return [dict(zip(columns, row)) for row in rows]


def to_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def choose_fields(records: list[dict[str, Any]], x_field: str | None, y_field: str | None) -> tuple[str, str]:
    if not records:
        raise HTTPException(status_code=400, detail="图表数据为空")
    fields = list(records[0].keys())
    if not fields:
        raise HTTPException(status_code=400, detail="图表字段为空")

    if x_field and x_field not in fields:
        raise HTTPException(status_code=400, detail=f"x_field 不存在: {x_field}")
    if y_field and y_field not in fields:
        raise HTTPException(status_code=400, detail=f"y_field 不存在: {y_field}")

    chosen_x = x_field or fields[0]
    chosen_y = y_field
    if not chosen_y:
        for field in fields:
            if field == chosen_x:
                continue
            if any(to_number(row.get(field)) is not None for row in records):
                chosen_y = field
                break
    if not chosen_y:
        raise HTTPException(status_code=400, detail="未找到可绘制的数值字段")
    return chosen_x, chosen_y


def configure_plot_style() -> None:
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def draw_chart(records: list[dict[str, Any]], chart_type: str, title: str, x_field: str, y_field: str, output_path: Path) -> None:
    labels = [str(row.get(x_field, "")) for row in records]
    values = [to_number(row.get(y_field)) for row in records]
    valid_pairs = [(label, value) for label, value in zip(labels, values) if value is not None]
    if not valid_pairs and chart_type != "table":
        raise HTTPException(status_code=400, detail=f"字段 {y_field} 没有可绘制的数值")

    configure_plot_style()
    fig, ax = plt.subplots(figsize=(9, 5), dpi=140)
    try:
        if chart_type == "bar":
            labels, values = zip(*valid_pairs)
            ax.bar(labels, values, color="#2563eb")
            ax.set_ylabel(y_field)
            ax.tick_params(axis="x", rotation=30)
        elif chart_type == "line":
            labels, values = zip(*valid_pairs)
            ax.plot(labels, values, marker="o", color="#2563eb")
            ax.set_ylabel(y_field)
            ax.tick_params(axis="x", rotation=30)
        elif chart_type == "pie":
            labels, values = zip(*valid_pairs)
            ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
            ax.axis("equal")
        else:
            columns = list(records[0].keys())
            rows = [[str(row.get(column, "")) for column in columns] for row in records[:20]]
            ax.axis("off")
            table = ax.table(cellText=rows, colLabels=columns, loc="center")
            table.auto_set_font_size(False)
            table.set_fontsize(8)
            table.scale(1, 1.4)

        ax.set_title(title)
        fig.tight_layout()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output_path, bbox_inches="tight")
    finally:
        plt.close(fig)


@router.post("/chart")
def create_chart(req: ChartRequest):
    payload = merge_chart_payload(req)
    records = records_from_payload(payload)
    chart_type = normalize_chart_type(payload.get("chart_type"))
    title = payload.get("title") or payload.get("question") or "财务数据图表"
    x_field, y_field = choose_fields(records, payload.get("x_field"), payload.get("y_field"))
    filename = safe_filename(title)
    output_path = CHART_OUTPUT_DIR / filename

    draw_chart(records, chart_type, title, x_field, y_field, output_path)
    return {
        "success": True,
        "chart_type": chart_type,
        "chart_file": str(CHART_PUBLIC_DIR / filename),
        "chart_url": f"/charts/{filename}",
        "x_field": x_field,
        "y_field": y_field,
        "message": "图表生成成功",
    }
