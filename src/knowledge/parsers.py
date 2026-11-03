"""文档解析器。

输入：文件名与原始 bytes。
输出：带标题层级的 `ParsedDocument`。
作用：把 PDF/DOCX/TXT/MD/CSV/JSON 统一成可切片的文本块。
实现：PDF 使用 pypdf，DOCX 使用 python-docx，其余使用标准库；只解析文本层。
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any

from src.knowledge.models import ParsedBlock, ParsedDocument

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json"}
MAX_PDF_PAGES = 200

_WHITESPACE_RE = re.compile(r"[ \t\u3000]+")
_NULL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class DocumentParseError(ValueError):
    """文档无法解析或没有可用文本。"""


def parse_document(
    filename: str,
    content: bytes,
    *,
    max_pdf_pages: int = MAX_PDF_PAGES,
) -> ParsedDocument:
    """按扩展名解析文档。

    输入：文件名、原始内容、PDF 页数上限。
    输出：ParsedDocument；空文本抛 DocumentParseError。
    作用：上传接口与测试共用同一条解析路径。
    实现：先做编码/空白清洗，再按格式抽取段落和 heading_path。
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise DocumentParseError(f"不支持的文件类型：{suffix or '无扩展名'}")
    if not content:
        raise DocumentParseError("文件为空")
    title = Path(filename).stem or filename

    try:
        if suffix == ".pdf":
            blocks = _parse_pdf(content, max_pdf_pages=max_pdf_pages)
        elif suffix == ".docx":
            blocks = _parse_docx(content)
        elif suffix == ".md":
            blocks = _parse_markdown(_decode_text(content))
        elif suffix == ".csv":
            blocks = _parse_csv(_decode_text(content), title)
        elif suffix == ".json":
            blocks = _parse_json(_decode_text(content), title)
        else:
            blocks = _parse_text(_decode_text(content))
    except DocumentParseError:
        raise
    except Exception as exc:  # 解析库边界，统一转成显式解析错误。
        raise DocumentParseError(f"文档解析失败：{type(exc).__name__}") from exc

    cleaned: list[ParsedBlock] = []
    for block in blocks:
        text = clean_text(block.text)
        if not text:
            continue
        cleaned.append(
            ParsedBlock(
                text=text,
                heading_path=[clean_text(item) for item in block.heading_path if clean_text(item)],
                kind=block.kind,
                metadata=block.metadata,
            )
        )
    if not cleaned:
        raise DocumentParseError("文档没有可用文本（扫描件 PDF 暂不支持 OCR）")
    return ParsedDocument(title=title, blocks=cleaned, metadata={"extension": suffix})


def clean_text(text: str) -> str:
    """输入：原始文本；输出：去控制字符、压缩空格、保留换行的文本。"""
    text = _NULL_RE.sub("", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _decode_text(content: bytes) -> str:
    """输入：bytes；输出：UTF-8/GB18030 文本；作用：兼容常见中文文本文件。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentParseError("文本文件编码无法识别")


def _split_paragraphs(text: str) -> list[str]:
    """输入：文本；输出：按空行切分的段落。"""
    parts = re.split(r"\n\s*\n+", clean_text(text))
    return [part.strip() for part in parts if part.strip()]


def _parse_text(text: str) -> list[ParsedBlock]:
    """输入：TXT；输出：段落块；作用：无结构文本的最小解析。"""
    return [ParsedBlock(text=part, kind="paragraph") for part in _split_paragraphs(text)]


def _parse_markdown(text: str) -> list[ParsedBlock]:
    """输入：Markdown；输出：按标题层级组织的段落块。

    实现：识别 ATX 标题；代码围栏保留为普通段落；列表项合并为段落。
    """
    blocks: list[ParsedBlock] = []
    headings: list[str] = []
    paragraph: list[str] = []
    in_code = False

    def flush() -> None:
        if paragraph:
            blocks.append(
                ParsedBlock(text="\n".join(paragraph), heading_path=list(headings), kind="paragraph")
            )
            paragraph.clear()

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            paragraph.append(line)
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if heading and not in_code:
            flush()
            level = len(heading.group(1))
            title = clean_text(heading.group(2))
            headings = headings[: level - 1] + [title]
            continue
        if not line.strip():
            flush()
        else:
            paragraph.append(line.strip())
    flush()
    if not blocks and text.strip():
        blocks.append(ParsedBlock(text=text, kind="paragraph"))
    return blocks


def _parse_csv(text: str, title: str) -> list[ParsedBlock]:
    """输入：CSV；输出：逐行记录块；作用：每行对应一个 Child。"""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise DocumentParseError("CSV 没有表头") from exc
    header = [clean_text(item) or f"列{index + 1}" for index, item in enumerate(header)]
    rows: list[ParsedBlock] = []
    for row_number, row in enumerate(reader, start=1):
        values = [clean_text(value) for value in row]
        if not any(values):
            continue
        pairs = []
        for index, value in enumerate(values):
            if not value:
                continue
            key = header[index] if index < len(header) else f"列{index + 1}"
            pairs.append(f"{key}：{value}")
        if pairs:
            rows.append(
                ParsedBlock(
                    text="；".join(pairs),
                    heading_path=[title],
                    kind="record",
                    metadata={"row": row_number},
                )
            )
    if not rows:
        raise DocumentParseError("CSV 没有数据行")
    return rows


def _parse_json(text: str, title: str) -> list[ParsedBlock]:
    """输入：JSON；输出：逐对象记录块；作用：数组/records 列表一行一个 Child。"""
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DocumentParseError("JSON 格式不合法") from exc

    records: list[Any]
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("items", "records", "data", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                records = value
                break
        else:
            records = [payload]
    else:
        records = [payload]

    blocks: list[ParsedBlock] = []
    for index, item in enumerate(records, start=1):
        text = _json_to_text(item)
        if text:
            blocks.append(
                ParsedBlock(
                    text=text,
                    heading_path=[title],
                    kind="record",
                    metadata={"record": index},
                )
            )
    if not blocks:
        raise DocumentParseError("JSON 没有可用记录")
    return blocks


def _json_to_text(value: Any, prefix: str = "") -> str:
    """输入：JSON 值；输出：扁平 key-value 文本；作用：保留字段语义。"""
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, (dict, list)):
                nested = _json_to_text(item, name)
                if nested:
                    parts.append(nested)
            elif item is not None:
                parts.append(f"{name}：{clean_text(str(item))}")
        return "；".join(parts)
    if isinstance(value, list):
        return "；".join(
            item for item in (_json_to_text(item, prefix) for item in value) if item
        )
    return clean_text(str(value)) if value is not None else ""


def _parse_docx(content: bytes) -> list[ParsedBlock]:
    """输入：DOCX bytes；输出：标题层级 + 段落/表格行。"""
    from docx import Document  # 延迟导入，缺少依赖时给出明确解析错误

    document = Document(io.BytesIO(content))
    blocks: list[ParsedBlock] = []
    headings: list[str] = []

    for paragraph in document.paragraphs:
        text = clean_text(paragraph.text)
        if not text:
            continue
        style = (paragraph.style.name if paragraph.style else "") or ""
        heading_match = re.match(r"Heading\s+(\d+)", style, re.IGNORECASE)
        if heading_match:
            level = int(heading_match.group(1))
            headings = headings[: level - 1] + [text]
            continue
        blocks.append(ParsedBlock(text=text, heading_path=list(headings), kind="paragraph"))

    for table in document.tables:
        for row in table.rows:
            values = [clean_text(cell.text) for cell in row.cells]
            text = "；".join(value for value in values if value)
            if text:
                blocks.append(
                    ParsedBlock(text=text, heading_path=list(headings), kind="record")
                )
    return blocks


def _parse_pdf(content: bytes, *, max_pdf_pages: int) -> list[ParsedBlock]:
    """输入：PDF bytes；输出：按页组织的文本块。"""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    if len(reader.pages) > max_pdf_pages:
        raise DocumentParseError(f"PDF 超过 {max_pdf_pages} 页限制")
    blocks: list[ParsedBlock] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        paragraphs = _split_paragraphs(text)
        for paragraph in paragraphs:
            blocks.append(
                ParsedBlock(
                    text=paragraph,
                    heading_path=[f"第 {page_number} 页"],
                    kind="paragraph",
                    metadata={"page": page_number},
                )
            )
    if not blocks:
        raise DocumentParseError("PDF 没有文本层（扫描件暂不支持 OCR）")
    return blocks
