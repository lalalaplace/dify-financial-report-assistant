from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pdf_geometry_utils import (
    PageGeometry,
    PageLine,
    PageRegion,
    PageToken,
    detect_repeated_margin_texts,
    extract_page_geometries,
    filter_margin_tokens,
    group_tokens_by_x_clusters,
)
from statement_table_schema import (
    ColumnSchema,
    FINANCIAL_ROW_KEYWORDS,
    HEADER_ROLE_ALIASES,
    ITEM_NAME_ALIASES,
    NormalizedRow,
    STATEMENT_TITLE_RULES,
    compact_text,
    detect_unit,
    infer_column_roles,
    normalize_item_name,
    normalize_text,
)


SIGNATURE_KEYWORDS = [
    "法定代表人",
    "主管会计工作负责人",
    "会计机构负责人",
    "企业负责人",
    "签字",
    "负责人",
]

PARENT_TABLE_HINTS = [
    "母公司资产负债表",
    "母公司利润表",
    "母公司现金流量表",
]


@dataclass
class GeometryColumn:
    col_id: int
    x_left: float
    x_right: float
    raw_name: str
    role: str
    score: float = 0.0
    features: Dict = field(default_factory=dict)


@dataclass
class GeometryRowFragment:
    page_no: int
    y0: float
    y1: float
    item_text: str
    cells: Dict[int, str]
    note_text: str = ""
    raw_text: str = ""
    merge_confidence: float = 1.0
    merged_from_pages: List[int] = field(default_factory=list)


@dataclass
class TableRecoveryResult:
    page_geometries: List[PageGeometry]
    regions: List[PageRegion]
    geometry_columns: List[GeometryColumn]
    column_schema: List[ColumnSchema]
    rows: List[NormalizedRow]
    parser_meta: Dict
    text: str
    page_text_map: Dict[str, str]


def recover_table_from_pdf(
    pdf_path,
    page_numbers: Sequence[int],
    statement_type: str,
    title_text: str,
    is_consolidated: bool,
) -> TableRecoveryResult:
    """基于坐标恢复标准化表结构。"""
    page_geometries = extract_page_geometries(pdf_path=pdf_path, page_numbers=page_numbers)
    repeated_margin_texts = detect_repeated_margin_texts(page_geometries)
    filtered_geometries = []

    for geometry in page_geometries:
        filtered_tokens = filter_margin_tokens(geometry, repeated_margin_texts=repeated_margin_texts)
        filtered_lines = _rebuild_lines_from_tokens(filtered_tokens)
        filtered_geometries.append(
            PageGeometry(
                page_no=geometry.page_no,
                width=geometry.width,
                height=geometry.height,
                tokens=filtered_tokens,
                lines=filtered_lines,
                raw_text=geometry.raw_text,
                header_tokens=geometry.header_tokens,
                footer_tokens=geometry.footer_tokens,
            )
        )

    regions, region_meta = detect_table_regions(
        page_geometries=filtered_geometries,
        statement_type=statement_type,
        title_text=title_text,
        is_consolidated=is_consolidated,
    )
    region_lines_by_page = collect_region_lines(filtered_geometries, regions)

    geometry_columns, column_meta = recover_geometry_columns(
        statement_type=statement_type,
        region_lines_by_page=region_lines_by_page,
    )
    aligned_columns, align_meta = align_columns_across_pages(
        geometry_columns=geometry_columns,
        region_lines_by_page=region_lines_by_page,
    )

    fragments, row_meta = recover_row_fragments(
        statement_type=statement_type,
        region_lines_by_page=region_lines_by_page,
        geometry_columns=aligned_columns,
    )
    merged_fragments, merge_meta = merge_fragments_across_pages(
        statement_type=statement_type,
        row_fragments=fragments,
        geometry_columns=aligned_columns,
    )

    note_meta = detect_note_column(aligned_columns, merged_fragments)
    column_schema = build_column_schema_from_geometry(statement_type, aligned_columns, note_meta)
    normalized_rows = build_normalized_rows(merged_fragments, column_schema, note_meta)

    page_text_map = {str(geometry.page_no): geometry.raw_text for geometry in filtered_geometries}
    full_text = "\n".join(geometry.raw_text for geometry in filtered_geometries if geometry.raw_text)
    unit, currency = detect_unit(full_text)

    parser_meta = {
        "recovery_method": "geometry_table_recovery_v1",
        "source_pages": [geometry.page_no for geometry in filtered_geometries],
        "detected_regions": [region.__dict__ for region in regions],
        "column_boundaries": [
            {
                "col_id": column.col_id,
                "x_left": round(column.x_left, 2),
                "x_right": round(column.x_right, 2),
                "raw_name": column.raw_name,
                "role": column.role,
                "score": round(column.score, 4),
                "features": column.features,
            }
            for column in aligned_columns
        ],
        "row_count_before_merge": len(fragments),
        "row_count_after_merge": len(normalized_rows),
        "repeated_headers_removed": row_meta.get("repeated_headers_removed", 0),
        "note_column_detected": note_meta["note_column_detected"],
        "note_column_index": note_meta.get("note_column_index"),
        "note_column_score": round(note_meta.get("note_column_score", 0.0), 4),
        "note_column_features": note_meta.get("note_column_features", {}),
        "cross_page_merges": merge_meta.get("cross_page_merges", 0),
        "cross_page_merge_details": merge_meta.get("merge_details", []),
        "ambiguous_parent_mix": region_meta.get("ambiguous_parent_mix", False),
        "column_alignment": align_meta,
        "page_word_counts": {str(geometry.page_no): len(geometry.tokens) for geometry in filtered_geometries},
        "unit_detected": unit or "元",
        "currency_detected": currency or "人民币",
        "parse_confidence": estimate_parse_confidence(
            normalized_rows=normalized_rows,
            aligned_columns=aligned_columns,
            region_count=len(regions),
            cross_page_merges=merge_meta.get("cross_page_merges", 0),
            note_score=note_meta.get("note_column_score", 0.0),
        ),
    }

    return TableRecoveryResult(
        page_geometries=filtered_geometries,
        regions=regions,
        geometry_columns=aligned_columns,
        column_schema=column_schema,
        rows=normalized_rows,
        parser_meta=parser_meta,
        text=full_text,
        page_text_map=page_text_map,
    )


def detect_table_regions(
    page_geometries: Sequence[PageGeometry],
    statement_type: str,
    title_text: str,
    is_consolidated: bool,
) -> Tuple[List[PageRegion], Dict]:
    """识别页面中的报表主体区域。"""
    regions: List[PageRegion] = []
    ambiguous_parent_mix = False

    for geometry in page_geometries:
        title_line = find_title_line(geometry.lines, title_text, statement_type)
        title_y = title_line.y0 if title_line else geometry.height * 0.12
        region_top = title_y + (title_line.y1 - title_line.y0 if title_line else 24.0) + 8.0
        region_bottom = geometry.height * 0.93

        for line in geometry.lines:
            text = compact_text(line.text)
            if any(keyword in text for keyword in SIGNATURE_KEYWORDS):
                region_bottom = min(region_bottom, line.y0 - 4.0)
                break

        if is_consolidated:
            parent_lines = [
                line
                for line in geometry.lines
                if (
                    any(compact_text(keyword) in compact_text(line.text) for keyword in PARENT_TABLE_HINTS)
                    or ("母公司" in compact_text(line.text) and any(compact_text(keyword) in compact_text(line.text) for keyword in ["资产负债表", "利润表", "现金流量表"]))
                )
            ]
            if parent_lines:
                ambiguous_parent_mix = True
                first_parent_line = min(parent_lines, key=lambda line: line.y0)
                if first_parent_line.y0 > region_top:
                    region_bottom = min(region_bottom, first_parent_line.y0 - 6.0)

        score = score_region(geometry, region_top, region_bottom, statement_type, title_line)
        regions.append(
            PageRegion(
                page_no=geometry.page_no,
                x0=geometry.width * 0.03,
                y0=max(0.0, region_top),
                x1=geometry.width * 0.97,
                y1=max(region_top + 20.0, region_bottom),
                score=score,
                reason="title_distance+numeic_density+keyword_density",
            )
        )

    return regions, {"ambiguous_parent_mix": ambiguous_parent_mix}


def find_title_line(lines: Sequence[PageLine], title_text: str, statement_type: str) -> Optional[PageLine]:
    """定位标题行。"""
    compact_title = compact_text(title_text)
    title_keywords = []
    if compact_title:
        title_keywords.append(compact_title)
    title_keywords.extend(
        compact_text(keyword)
        for group in ("strong", "weak")
        for keyword in STATEMENT_TITLE_RULES.get(statement_type, {}).get(group, [])
    )

    for line in lines[:30]:
        compact_line = compact_text(line.text)
        if any(keyword and keyword in compact_line for keyword in title_keywords):
            return line
    return None


def score_region(
    geometry: PageGeometry,
    region_top: float,
    region_bottom: float,
    statement_type: str,
    title_line: Optional[PageLine],
) -> float:
    """对候选区域评分。"""
    region_lines = [line for line in geometry.lines if line.y0 >= region_top and line.y1 <= region_bottom]
    numeric_density = sum(1 for line in region_lines if line.numeric_count >= 2)
    keyword_hits = sum(
        1
        for line in region_lines[:50]
        for keyword in FINANCIAL_ROW_KEYWORDS.get(statement_type, [])
        if compact_text(keyword) in compact_text(line.text)
    )
    title_bonus = 12.0 if title_line is not None else 0.0
    return min(100.0, title_bonus + numeric_density * 1.8 + keyword_hits * 2.5)


def collect_region_lines(
    page_geometries: Sequence[PageGeometry],
    regions: Sequence[PageRegion],
) -> Dict[int, List[PageLine]]:
    """提取落在表区域内的行。"""
    region_map = {region.page_no: region for region in regions}
    result: Dict[int, List[PageLine]] = {}
    for geometry in page_geometries:
        region = region_map.get(geometry.page_no)
        if region is None:
            continue
        region_lines = [
            line
            for line in geometry.lines
            if line.y0 >= region.y0 and line.y1 <= region.y1 and line.x1 >= region.x0 and line.x0 <= region.x1
        ]
        result[geometry.page_no] = region_lines
    return result


def recover_geometry_columns(
    statement_type: str,
    region_lines_by_page: Dict[int, List[PageLine]],
) -> Tuple[List[GeometryColumn], Dict]:
    """恢复列边界。"""
    header_candidates = collect_header_candidates(region_lines_by_page)
    data_tokens = []
    for lines in region_lines_by_page.values():
        for line in lines:
            data_tokens.extend(line.tokens)

    numeric_tokens = [token for token in data_tokens if token.is_numeric]
    numeric_clusters = group_tokens_by_x_clusters(numeric_tokens, tolerance=20.0)
    numeric_clusters = [cluster for cluster in numeric_clusters if len(cluster) >= max(2, len(region_lines_by_page))]
    numeric_clusters.sort(key=lambda cluster: sum(token.x_center for token in cluster) / len(cluster))

    amount_columns: List[GeometryColumn] = []
    for index, cluster in enumerate(numeric_clusters[:2], start=1):
        x_left = min(token.x0 for token in cluster)
        x_right = max(token.x1 for token in cluster)
        amount_columns.append(
            GeometryColumn(
                col_id=index,
                x_left=x_left - 2.0,
                x_right=x_right + 2.0,
                raw_name="",
                role="unknown",
                score=0.5,
                features={
                    "cluster_size": len(cluster),
                    "numeric_ratio": 1.0,
                },
            )
        )

    item_tokens = [
        token
        for token in data_tokens
        if not token.is_numeric
        and token.x_center < (amount_columns[0].x_left if amount_columns else 999999)
    ]
    item_x_left = min((token.x0 for token in item_tokens), default=0.0)
    item_x_right = max((token.x1 for token in item_tokens), default=(amount_columns[0].x_left - 8.0 if amount_columns else 220.0))
    item_column = GeometryColumn(
        col_id=0,
        x_left=max(0.0, item_x_left - 2.0),
        x_right=max(item_x_right + 2.0, item_x_left + 120.0),
        raw_name="项目",
        role="item_name",
        score=1.0,
        features={"token_count": len(item_tokens)},
    )

    geometry_columns = [item_column] + amount_columns
    note_meta = infer_note_geometry_column(item_column, amount_columns, header_candidates, data_tokens, region_lines_by_page)
    if note_meta is not None:
        note_column = GeometryColumn(
            col_id=1,
            x_left=note_meta["x_left"],
            x_right=note_meta["x_right"],
            raw_name=note_meta["raw_name"],
            role="note_ref",
            score=note_meta["score"],
            features=note_meta["features"],
        )
        shifted_columns = [item_column, note_column]
        for index, column in enumerate(amount_columns, start=2):
            shifted_columns.append(
                GeometryColumn(
                    col_id=index,
                    x_left=column.x_left,
                    x_right=column.x_right,
                    raw_name=column.raw_name,
                    role=column.role,
                    score=column.score,
                    features=column.features,
                )
            )
        geometry_columns = shifted_columns

    apply_header_labels(statement_type, geometry_columns, header_candidates)
    return geometry_columns, {
        "header_candidates": header_candidates,
        "numeric_cluster_count": len(numeric_clusters),
    }


def collect_header_candidates(region_lines_by_page: Dict[int, List[PageLine]]) -> List[str]:
    """收集表头候选。"""
    candidates: List[str] = []
    for lines in region_lines_by_page.values():
        for line in lines[:8]:
            text = normalize_text(line.text)
            if not text:
                continue
            if any(keyword in text for keyword in ["项目", "附注", "期末", "期初", "本期", "上期", "本报告期", "上年同期", "年", "月", "日"]):
                candidates.append(text)
    deduped: List[str] = []
    seen = set()
    for item in candidates:
        key = compact_text(item)
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped[:10]


def infer_note_geometry_column(
    item_column: GeometryColumn,
    amount_columns: Sequence[GeometryColumn],
    header_candidates: Sequence[str],
    data_tokens: Sequence[PageToken],
    region_lines_by_page: Dict[int, List[PageLine]],
) -> Optional[Dict]:
    """识别附注列。"""
    if not amount_columns:
        return None

    header_note_token = find_header_note_token(region_lines_by_page)
    if header_note_token is not None:
        x_left = max(item_column.x_right + 1.0, header_note_token.x0 - 8.0)
        x_right = min(amount_columns[0].x_left - 1.0, header_note_token.x1 + 8.0)
        if x_right > x_left:
            return {
                "x_left": x_left,
                "x_right": x_right,
                "raw_name": header_note_token.text,
                "score": 0.92,
                "features": {
                    "header_hit": 1.0,
                    "short_code_ratio": 0.0,
                    "numeric_ratio": 0.0,
                    "position_bonus": 0.2,
                    "width_bonus": 0.15,
                    "detected_from": "header_token",
                },
            }

    gap_left = item_column.x_right
    gap_right = amount_columns[0].x_left
    if gap_right - gap_left < 12:
        return None

    gap_tokens = [token for token in data_tokens if token.x_center >= gap_left and token.x_center <= gap_right]
    if not gap_tokens:
        return None

    short_code_tokens = [
        token
        for token in gap_tokens
        if len(compact_text(token.text)) <= 4 and not token.is_numeric and compact_text(token.text)
    ]
    numeric_tokens = [token for token in gap_tokens if token.is_numeric]

    header_hit = 0.0
    for line in header_candidates:
        compact_line = compact_text(line)
        if "附注" in compact_line or "注释" in compact_line:
            header_hit = 0.35
            break

    short_code_ratio = len(short_code_tokens) / max(1, len(gap_tokens))
    numeric_ratio = len(numeric_tokens) / max(1, len(gap_tokens))
    width = gap_right - gap_left
    position_bonus = 0.2
    width_bonus = 0.15 if width <= 60 else 0.05
    score = header_hit + short_code_ratio * 0.35 + position_bonus + width_bonus - numeric_ratio * 0.25

    if header_hit > 0 and gap_right - gap_left >= 18:
        return {
            "x_left": gap_left + 1.0,
            "x_right": gap_right - 1.0,
            "raw_name": "附注",
            "score": max(score, 0.72),
            "features": {
                "header_hit": round(header_hit, 4),
                "short_code_ratio": round(short_code_ratio, 4),
                "numeric_ratio": round(numeric_ratio, 4),
                "position_bonus": position_bonus,
                "width_bonus": width_bonus,
                "width": round(width, 2),
                "detected_from": "header_gap_hint",
            },
        }

    if score < 0.38:
        return None

    return {
        "x_left": gap_left + 1.0,
        "x_right": gap_right - 1.0,
        "raw_name": "附注",
        "score": score,
        "features": {
            "header_hit": round(header_hit, 4),
            "short_code_ratio": round(short_code_ratio, 4),
            "numeric_ratio": round(numeric_ratio, 4),
            "position_bonus": position_bonus,
            "width_bonus": width_bonus,
            "width": round(width, 2),
        },
    }


def find_header_note_token(region_lines_by_page: Dict[int, List[PageLine]]) -> Optional[PageToken]:
    """从表头中直接查找附注列标题 token。"""
    for lines in region_lines_by_page.values():
        for line in lines[:8]:
            for token in line.tokens:
                compact_token = compact_text(token.text)
                if compact_token in {"附注", "注释", "附注号"}:
                    return token
    return None


def apply_header_labels(
    statement_type: str,
    geometry_columns: List[GeometryColumn],
    header_candidates: Sequence[str],
) -> None:
    """利用表头语义修正列角色。"""
    header_schema = infer_column_roles(header_lines=header_candidates, statement_type=statement_type)

    amount_role_sequence = [item.role for item in header_schema if item.role != "item_name"]
    amount_name_sequence = [item.raw_name for item in header_schema if item.role != "item_name"]

    amount_columns = [column for column in geometry_columns if column.role not in {"item_name", "note_ref"}]
    for index, column in enumerate(amount_columns):
        if index < len(amount_role_sequence):
            column.role = amount_role_sequence[index]
            column.raw_name = amount_name_sequence[index]
        elif index == 0:
            column.role = "current_period"
            column.raw_name = HEADER_ROLE_ALIASES.get(statement_type, {}).get("current_period", ["本期"])[0]
        elif index == 1:
            column.role = "previous_period"
            column.raw_name = HEADER_ROLE_ALIASES.get(statement_type, {}).get("previous_period", ["上期"])[0]


def align_columns_across_pages(
    geometry_columns: Sequence[GeometryColumn],
    region_lines_by_page: Dict[int, List[PageLine]],
) -> Tuple[List[GeometryColumn], Dict]:
    """跨页对齐列边界。"""
    aligned: List[GeometryColumn] = []
    for column in geometry_columns:
        aligned.append(
            GeometryColumn(
                col_id=column.col_id,
                x_left=column.x_left,
                x_right=column.x_right,
                raw_name=column.raw_name,
                role=column.role,
                score=column.score,
                features=column.features,
            )
        )
    return aligned, {"pages_aligned": sorted(region_lines_by_page.keys()), "column_count": len(aligned)}


def recover_row_fragments(
    statement_type: str,
    region_lines_by_page: Dict[int, List[PageLine]],
    geometry_columns: Sequence[GeometryColumn],
) -> Tuple[List[GeometryRowFragment], Dict]:
    """按几何列恢复行碎片。"""
    fragments: List[GeometryRowFragment] = []
    repeated_headers_removed = 0

    for page_no, lines in region_lines_by_page.items():
        lines_to_use = list(lines)
        removed_count, cleaned_lines = detect_repeated_header_on_new_page(lines_to_use, geometry_columns)
        repeated_headers_removed += removed_count
        for line in cleaned_lines:
            fragment = build_row_fragment_from_line(line, geometry_columns)
            if fragment is None:
                continue
            fragments.append(fragment)

    return fragments, {"repeated_headers_removed": repeated_headers_removed}


def detect_repeated_header_on_new_page(
    lines: Sequence[PageLine],
    geometry_columns: Sequence[GeometryColumn],
) -> Tuple[int, List[PageLine]]:
    """识别并剔除跨页重复表头。"""
    removed = 0
    cleaned: List[PageLine] = []
    header_keywords = {"项目", "附注", "期末", "期初", "本期", "上期", "本报告期", "上年同期"}

    for index, line in enumerate(lines):
        compact_line = compact_text(line.text)
        if index <= 4 and any(keyword in compact_line for keyword in header_keywords):
            removed += 1
            continue
        cleaned.append(line)
    return removed, cleaned


def build_row_fragment_from_line(line: PageLine, geometry_columns: Sequence[GeometryColumn]) -> Optional[GeometryRowFragment]:
    """将一行几何对象转换为行碎片。"""
    item_parts: List[str] = []
    cells: Dict[int, str] = {}
    note_text = ""

    for token in line.tokens:
        column = locate_token_column(token, geometry_columns)
        if column is None:
            continue
        if column.role == "item_name":
            item_parts.append(token.text)
        elif column.role == "note_ref":
            note_text = f"{note_text} {token.text}".strip()
        else:
            cells[column.col_id] = f"{cells.get(column.col_id, '')} {token.text}".strip()

    item_text = "".join(item_parts).strip()
    if should_skip_fragment_line(item_text=item_text, raw_text=line.text, cells=cells):
        return None
    if not item_text and not cells:
        return None

    return GeometryRowFragment(
        page_no=line.page_no,
        y0=line.y0,
        y1=line.y1,
        item_text=item_text,
        cells=cells,
        note_text=note_text,
        raw_text=line.text,
        merge_confidence=1.0,
        merged_from_pages=[line.page_no],
    )


def should_skip_fragment_line(item_text: str, raw_text: str, cells: Dict[int, str]) -> bool:
    """过滤页眉、标题、签字和非表格噪声。"""
    compact_item = compact_text(item_text)
    compact_raw = compact_text(raw_text)
    if not compact_item and not cells:
        return True

    noise_keywords = [
        "财务报表",
        "年度报告",
        "合并资产负债表",
        "母公司资产负债表",
        "合并利润表",
        "母公司利润表",
        "合并现金流量表",
        "母公司现金流量表",
        "编制单位",
        "币种",
        "单位",
        "负责人",
        "页",
    ]
    if any(keyword in compact_item for keyword in noise_keywords):
        return True
    if any(keyword in compact_raw for keyword in noise_keywords):
        return True
    if compact_item in {"项目", "附注"}:
        return True
    if compact_raw.startswith("2022年") or compact_raw.startswith("2021年") or compact_raw.startswith("2023年") or compact_raw.startswith("2024年"):
        return True
    return False


def locate_token_column(token: PageToken, geometry_columns: Sequence[GeometryColumn]) -> Optional[GeometryColumn]:
    """为 token 找到所属列。"""
    for column in geometry_columns:
        if token.x_center >= column.x_left and token.x_center <= column.x_right:
            return column
    return None


def merge_fragments_across_pages(
    statement_type: str,
    row_fragments: Sequence[GeometryRowFragment],
    geometry_columns: Sequence[GeometryColumn],
) -> Tuple[List[GeometryRowFragment], Dict]:
    """跨页合并断裂行。"""
    merged = list(row_fragments)
    cross_page_merges = 0
    merge_details: List[Dict] = []

    index = 0
    while index < len(merged) - 1:
        current = merged[index]
        nxt = merged[index + 1]
        if should_merge_page_break_rows(current, nxt, geometry_columns):
            merged_row, merge_reason, merge_confidence = merge_broken_rows(current, nxt)
            merged[index] = merged_row
            merged.pop(index + 1)
            cross_page_merges += 1
            merge_details.append(
                {
                    "from_pages": [current.page_no, nxt.page_no],
                    "reason": merge_reason,
                    "confidence": round(merge_confidence, 4),
                }
            )
            continue
        index += 1

    return merged, {"cross_page_merges": cross_page_merges, "merge_details": merge_details}


def should_merge_page_break_rows(
    last_row: GeometryRowFragment,
    first_row: GeometryRowFragment,
    geometry_columns: Sequence[GeometryColumn],
) -> bool:
    """判断是否需要跨页拼接断裂行。"""
    if first_row.page_no == last_row.page_no:
        return False
    if first_row.page_no != last_row.page_no + 1:
        return False

    last_item = normalize_item_name(last_row.item_text)
    first_item = normalize_item_name(first_row.item_text)
    last_amount_count = len(last_row.cells)
    first_amount_count = len(first_row.cells)

    if last_item and not first_item and first_amount_count > 0:
        return True
    if not last_amount_count and first_amount_count > 0 and last_item:
        return True
    if last_item and first_item and len(last_item) <= 8 and len(first_item) <= 12 and first_amount_count == 0:
        return True
    if last_item and first_item and last_amount_count == 0 and first_amount_count > 0:
        return True
    return False


def merge_broken_rows(
    last_row: GeometryRowFragment,
    first_row: GeometryRowFragment,
) -> Tuple[GeometryRowFragment, str, float]:
    """合并跨页断裂行。"""
    item_text = (last_row.item_text + first_row.item_text).strip() if first_row.item_text else last_row.item_text
    merged_cells = dict(last_row.cells)
    for key, value in first_row.cells.items():
        if key not in merged_cells or not normalize_text(merged_cells[key]):
            merged_cells[key] = value

    merge_reason = "page_break_item_or_amount_completion"
    merge_confidence = 0.86
    merged_row = GeometryRowFragment(
        page_no=last_row.page_no,
        y0=last_row.y0,
        y1=first_row.y1,
        item_text=item_text,
        cells=merged_cells,
        note_text=last_row.note_text or first_row.note_text,
        raw_text=(last_row.raw_text + "\n" + first_row.raw_text).strip(),
        merge_confidence=merge_confidence,
        merged_from_pages=sorted(set(last_row.merged_from_pages + first_row.merged_from_pages)),
    )
    return merged_row, merge_reason, merge_confidence


def detect_note_column(
    geometry_columns: Sequence[GeometryColumn],
    row_fragments: Sequence[GeometryRowFragment],
) -> Dict:
    """输出附注列识别摘要。"""
    note_columns = [column for column in geometry_columns if column.role == "note_ref"]
    if not note_columns:
        return {
            "note_column_detected": False,
            "note_column_index": None,
            "note_column_score": 0.0,
            "note_column_features": {},
        }

    note_column = note_columns[0]
    non_empty_notes = sum(1 for row in row_fragments if normalize_text(row.note_text))
    note_ratio = non_empty_notes / max(1, len(row_fragments))
    score = min(1.0, note_column.score + note_ratio * 0.2)
    features = dict(note_column.features)
    features["non_empty_note_ratio"] = round(note_ratio, 4)
    return {
        "note_column_detected": True,
        "note_column_index": note_column.col_id,
        "note_column_score": score,
        "note_column_features": features,
    }


def build_column_schema_from_geometry(
    statement_type: str,
    geometry_columns: Sequence[GeometryColumn],
    note_meta: Dict,
) -> List[ColumnSchema]:
    """将几何列转换为输出列结构。"""
    schema: List[ColumnSchema] = []
    for column in geometry_columns:
        raw_name = column.raw_name or default_column_name(statement_type, column.role)
        schema.append(ColumnSchema(col_id=column.col_id, raw_name=raw_name, role=column.role))
    return schema


def default_column_name(statement_type: str, role: str) -> str:
    """为缺失表头的列提供默认名。"""
    if role == "item_name":
        return "项目"
    if role == "note_ref":
        return "附注"
    if role == "current_period":
        return HEADER_ROLE_ALIASES.get(statement_type, {}).get("current_period", ["本期"])[0]
    if role == "previous_period":
        return HEADER_ROLE_ALIASES.get(statement_type, {}).get("previous_period", ["上期"])[0]
    return role


def build_normalized_rows(
    fragments: Sequence[GeometryRowFragment],
    column_schema: Sequence[ColumnSchema],
    note_meta: Dict,
) -> List[NormalizedRow]:
    """构建最终输出行。"""
    rows: List[NormalizedRow] = []
    role_by_id = {column.col_id: column.role for column in column_schema}
    note_index = note_meta.get("note_column_index")

    for index, fragment in enumerate(fragments):
        normalized_name = normalize_item_name(fragment.item_text)
        normalized_name = ITEM_NAME_ALIASES.get(normalized_name, normalized_name)
        if not normalized_name:
            continue

        cells: Dict[str, str] = {}
        if note_index is not None and normalize_text(fragment.note_text):
            cells[str(note_index)] = normalize_text(fragment.note_text)

        for col_id, value in fragment.cells.items():
            if role_by_id.get(col_id) in {"item_name"}:
                continue
            cells[str(col_id)] = normalize_text(value)

        rows.append(
            NormalizedRow(
                row_id=index,
                raw_item_name=fragment.item_text,
                normalized_item_name=normalized_name,
                cells=cells,
                source_page=fragment.page_no,
                raw_line_text=fragment.raw_text,
                merge_confidence=fragment.merge_confidence,
                merged_from_pages=fragment.merged_from_pages,
                extra_info={"source_pages": fragment.merged_from_pages},
            )
        )
    return rows


def estimate_parse_confidence(
    normalized_rows: Sequence[NormalizedRow],
    aligned_columns: Sequence[GeometryColumn],
    region_count: int,
    cross_page_merges: int,
    note_score: float,
) -> float:
    """估算解析置信度。"""
    row_score = min(len(normalized_rows), 40) / 40.0
    column_score = min(len(aligned_columns), 4) / 4.0
    region_score = min(region_count, 3) / 3.0
    merge_bonus = min(cross_page_merges, 3) * 0.04
    confidence = row_score * 0.55 + column_score * 0.2 + region_score * 0.1 + min(note_score, 1.0) * 0.1 + merge_bonus
    return round(min(1.0, max(0.0, confidence)), 4)


def _rebuild_lines_from_tokens(tokens: Sequence[PageToken]) -> List[PageLine]:
    from pdf_geometry_utils import build_page_lines

    return build_page_lines(tokens)
