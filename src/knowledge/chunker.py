"""Parent-Child 切片器。

输入：解析后的结构块。
输出：短 Child（检索/引用）+ 长 Parent（Writer 上下文）。
作用：把“找得准”和“看得懂”拆成两个粒度。
实现：确定性句子/记录切分；按 heading 和长度聚合 Parent；不调用 LLM。
"""

from __future__ import annotations

import hashlib
import re
from collections import OrderedDict

from src.knowledge.models import ChunkDraft, ChunkType, ParsedBlock, ParsedDocument

CHUNKER_VERSION = "pc-v1"
CHILD_MIN_CHARS = 20
CHILD_MAX_CHARS = 120
PARENT_MAX_CHARS = 800
PARENT_MAX_CHILDREN = 8
PARENT_MIN_CHILDREN = 3

_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*")
_SOFT_SPLIT_RE = re.compile(r"(?<=[，、,：:])\s*")


def build_chunks(document_id: str, parsed: ParsedDocument) -> list[ChunkDraft]:
    """把解析块切成 parent/child 两层。

    输入：document_id、ParsedDocument。
    输出：先 parent 后 child 的 ChunkDraft 列表（ordinal 全局递增）。
    作用：入库服务据此批量 embedding 和写库。
    实现：按 heading 分组；Child 句子/记录级；Parent 聚合 3–8 个 Child / 最多 800 字。
    """
    groups = _group_blocks(parsed.blocks)
    drafts: list[ChunkDraft] = []
    ordinal = 0
    parent_ordinal = 0
    parent_child_counts: OrderedDict[str, int] = OrderedDict()

    for heading_path, units in groups:
        for start in range(0, len(units), PARENT_MAX_CHILDREN):
            window = units[start : start + PARENT_MAX_CHILDREN]
            if not window:
                continue
            parent_text = "\n".join(unit for unit, _kind in window)
            if len(parent_text) > PARENT_MAX_CHARS:
                window = _shrink_window(window, PARENT_MAX_CHARS)
                if not window:
                    continue
                parent_text = "\n".join(unit for unit, _kind in window)
            parent_ordinal += 1
            ordinal += 1
            parent_id = _chunk_id(document_id, "p", parent_ordinal, parent_text)
            drafts.append(
                ChunkDraft(
                    id=parent_id,
                    document_id=document_id,
                    chunk_type=ChunkType.PARENT,
                    ordinal=ordinal,
                    text=parent_text,
                    heading_path=list(heading_path),
                    metadata={"child_count": len(window)},
                )
            )
            parent_child_counts[parent_id] = 0

            for text, kind in window:
                ordinal += 1
                child_id = _chunk_id(document_id, "c", ordinal, text)
                heading_text = " / ".join(heading_path)
                embedding_text = f"{heading_text}\n{text}" if heading_text else text
                drafts.append(
                    ChunkDraft(
                        id=child_id,
                        document_id=document_id,
                        chunk_type=ChunkType.CHILD,
                        ordinal=ordinal,
                        text=text,
                        parent_id=parent_id,
                        heading_path=list(heading_path),
                        material_id=f"M-{child_id}",
                        embedding_text=embedding_text,
                        token_count=len(text),
                        metadata={"source_kind": kind},
                    )
                )
                parent_child_counts[parent_id] += 1

    return drafts


def _group_blocks(blocks: list[ParsedBlock]) -> list[tuple[list[str], list[tuple[str, str]]]]:
    """输入：解析块；输出：(heading, units) 分组；作用：不跨标题聚合 Parent。"""
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    current_heading: list[str] | None = None
    current_units: list[tuple[str, str]] = []

    for block in blocks:
        heading = list(block.heading_path)
        if current_heading is not None and heading != current_heading and current_units:
            groups.append((current_heading, current_units))
            current_units = []
        current_heading = heading
        for unit in _split_block(block):
            current_units.append((unit, block.kind))
            if len(current_units) >= PARENT_MAX_CHILDREN:
                groups.append((current_heading, current_units))
                current_units = []
    if current_units and current_heading is not None:
        groups.append((current_heading, current_units))
    return groups


def _split_block(block: ParsedBlock) -> list[str]:
    """输入：结构块；输出：Child 文本列表；作用：记录一行一条，正文按句切。"""
    text = block.text.strip()
    if not text:
        return []
    if block.kind == "record":
        return _split_long(text, CHILD_MAX_CHARS)
    pieces = [piece.strip() for piece in _SENTENCE_RE.split(text) if piece.strip()]
    if not pieces:
        return [text]
    result: list[str] = []
    for piece in pieces:
        result.extend(_split_long(piece, CHILD_MAX_CHARS))
    return [item for item in result if item]


def _split_long(text: str, max_chars: int) -> list[str]:
    """输入：过长文本；输出：尽量在逗号/分号处切开的短块。"""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = max(window.rfind(mark) for mark in ("，", "、", "；", "：", ",", " ")) + 1
        if cut <= 0:
            cut = max_chars
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return [part for part in parts if part]


def _shrink_window(
    window: list[tuple[str, str]], max_chars: int
) -> list[tuple[str, str]]:
    """输入：Parent 候选 window；输出：满足长度上限的前缀；作用：控制上下文长度。"""
    selected: list[tuple[str, str]] = []
    total = 0
    for text, kind in window:
        addition = len(text) + (1 if selected else 0)
        if selected and total + addition > max_chars:
            break
        selected.append((text, kind))
        total += addition
    return selected


def _chunk_id(document_id: str, prefix: str, ordinal: int, text: str) -> str:
    """输入：文档、层级、序号、文本；输出：稳定短 ID。"""
    digest = hashlib.sha1(f"{document_id}:{prefix}:{ordinal}:{text}".encode("utf-8")).hexdigest()[:12]
    return f"{document_id[:8]}-{prefix}{ordinal:04d}-{digest}"
