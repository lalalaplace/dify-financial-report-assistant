from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import fitz

from statement_table_schema import NUMBER_TOKEN_PATTERN, compact_text, normalize_text


@dataclass
class PageToken:
    page_no: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block_no: int = 0
    line_no: int = 0
    word_no: int = 0

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def is_numeric(self) -> bool:
        return bool(NUMBER_TOKEN_PATTERN.fullmatch(normalize_text(self.text)))


@dataclass
class PageLine:
    page_no: int
    y0: float
    y1: float
    tokens: List[PageToken] = field(default_factory=list)
    text: str = ""

    @property
    def x0(self) -> float:
        return min((token.x0 for token in self.tokens), default=0.0)

    @property
    def x1(self) -> float:
        return max((token.x1 for token in self.tokens), default=0.0)

    @property
    def y_center(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def numeric_count(self) -> int:
        return sum(1 for token in self.tokens if token.is_numeric)


@dataclass
class PageRegion:
    page_no: int
    x0: float
    y0: float
    x1: float
    y1: float
    score: float = 0.0
    reason: str = ""


@dataclass
class PageGeometry:
    page_no: int
    width: float
    height: float
    tokens: List[PageToken]
    lines: List[PageLine]
    raw_text: str
    header_tokens: List[PageToken] = field(default_factory=list)
    footer_tokens: List[PageToken] = field(default_factory=list)


def resolve_pdf_path(file_path: str, project_root: Path) -> Path:
    """解析 PDF 绝对路径。"""
    path = Path(file_path)
    if path.is_absolute():
        return path
    return (project_root / path).resolve()


def extract_page_geometries(
    pdf_path: Path,
    page_numbers: Sequence[int],
    header_ratio: float = 0.08,
    footer_ratio: float = 0.06,
) -> List[PageGeometry]:
    """提取指定页的几何对象。"""
    geometries: List[PageGeometry] = []
    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)
        for page_no in page_numbers:
            if page_no < 1 or page_no > total_pages:
                continue

            page = doc[page_no - 1]
            raw_text = normalize_text(page.get_text("text"))
            width = float(page.rect.width)
            height = float(page.rect.height)
            words = page.get_text("words")

            tokens: List[PageToken] = []
            for item in words:
                x0, y0, x1, y1, text, block_no, line_no, word_no = item[:8]
                clean_text = normalize_text(text)
                if not clean_text:
                    continue
                tokens.append(
                    PageToken(
                        page_no=page_no,
                        x0=float(x0),
                        y0=float(y0),
                        x1=float(x1),
                        y1=float(y1),
                        text=clean_text,
                        block_no=int(block_no),
                        line_no=int(line_no),
                        word_no=int(word_no),
                    )
                )

            tokens.sort(key=lambda token: (token.y_center, token.x0))
            lines = build_page_lines(tokens)
            header_tokens, footer_tokens = split_header_footer_tokens(tokens, height, header_ratio, footer_ratio)
            geometries.append(
                PageGeometry(
                    page_no=page_no,
                    width=width,
                    height=height,
                    tokens=tokens,
                    lines=lines,
                    raw_text=raw_text,
                    header_tokens=header_tokens,
                    footer_tokens=footer_tokens,
                )
            )
    return geometries


def build_page_lines(tokens: Sequence[PageToken], y_tolerance: float = 2.8) -> List[PageLine]:
    """按 y 坐标聚合 token 为页面行。"""
    if not tokens:
        return []

    sorted_tokens = sorted(tokens, key=lambda token: (token.y_center, token.x0))
    lines: List[PageLine] = []
    current_tokens: List[PageToken] = []
    current_y: Optional[float] = None

    for token in sorted_tokens:
        if current_y is None or abs(token.y_center - current_y) <= y_tolerance:
            current_tokens.append(token)
            current_y = token.y_center if current_y is None else (current_y + token.y_center) / 2.0
            continue

        lines.append(_finalize_line(current_tokens))
        current_tokens = [token]
        current_y = token.y_center

    if current_tokens:
        lines.append(_finalize_line(current_tokens))

    return lines


def split_header_footer_tokens(
    tokens: Sequence[PageToken],
    page_height: float,
    header_ratio: float,
    footer_ratio: float,
) -> Tuple[List[PageToken], List[PageToken]]:
    """根据页高拆分页眉页脚区域。"""
    header_limit = page_height * header_ratio
    footer_limit = page_height * (1.0 - footer_ratio)

    header_tokens = [token for token in tokens if token.y_center <= header_limit]
    footer_tokens = [token for token in tokens if token.y_center >= footer_limit]
    return header_tokens, footer_tokens


def detect_repeated_margin_texts(page_geometries: Sequence[PageGeometry]) -> Dict[str, set]:
    """检测跨页重复的页眉页脚文本。"""
    header_counter: Dict[str, int] = {}
    footer_counter: Dict[str, int] = {}

    for geometry in page_geometries:
        header_texts = {compact_text(token.text) for token in geometry.header_tokens if compact_text(token.text)}
        footer_texts = {compact_text(token.text) for token in geometry.footer_tokens if compact_text(token.text)}
        for text in header_texts:
            header_counter[text] = header_counter.get(text, 0) + 1
        for text in footer_texts:
            footer_counter[text] = footer_counter.get(text, 0) + 1

    threshold = max(2, len(page_geometries) // 2 + 1)
    return {
        "header": {text for text, count in header_counter.items() if count >= threshold},
        "footer": {text for text, count in footer_counter.items() if count >= threshold},
    }


def filter_margin_tokens(
    geometry: PageGeometry,
    repeated_margin_texts: Optional[Dict[str, set]] = None,
) -> List[PageToken]:
    """过滤页眉页脚与高频重复区域 token。"""
    repeated_margin_texts = repeated_margin_texts or {"header": set(), "footer": set()}
    header_texts = repeated_margin_texts.get("header", set())
    footer_texts = repeated_margin_texts.get("footer", set())

    filtered: List[PageToken] = []
    for token in geometry.tokens:
        compact = compact_text(token.text)
        if compact in header_texts and token.y_center <= geometry.height * 0.12:
            continue
        if compact in footer_texts and token.y_center >= geometry.height * 0.88:
            continue
        filtered.append(token)
    return filtered


def group_tokens_by_x_clusters(tokens: Sequence[PageToken], tolerance: float = 16.0) -> List[List[PageToken]]:
    """按 x 中心聚合列簇。"""
    if not tokens:
        return []

    sorted_tokens = sorted(tokens, key=lambda token: token.x_center)
    clusters: List[List[PageToken]] = []
    current_cluster: List[PageToken] = []
    current_center: Optional[float] = None

    for token in sorted_tokens:
        if current_center is None or abs(token.x_center - current_center) <= tolerance:
            current_cluster.append(token)
            current_center = token.x_center if current_center is None else (current_center + token.x_center) / 2.0
            continue

        clusters.append(current_cluster)
        current_cluster = [token]
        current_center = token.x_center

    if current_cluster:
        clusters.append(current_cluster)

    return clusters


def summarize_region_lines(lines: Iterable[PageLine]) -> Dict[str, float]:
    """统计区域行摘要。"""
    line_list = list(lines)
    numeric_lines = sum(1 for line in line_list if line.numeric_count >= 2)
    return {
        "line_count": float(len(line_list)),
        "numeric_line_count": float(numeric_lines),
    }


def _finalize_line(tokens: Sequence[PageToken]) -> PageLine:
    ordered = sorted(tokens, key=lambda token: token.x0)
    text = " ".join(token.text for token in ordered)
    y0 = min(token.y0 for token in ordered)
    y1 = max(token.y1 for token in ordered)
    return PageLine(page_no=ordered[0].page_no, y0=y0, y1=y1, tokens=list(ordered), text=text)
