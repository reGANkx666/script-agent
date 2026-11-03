"""知识库原文件删除边界测试。"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from src.api.routes_knowledge import _remove_uploaded_file


def _workspace_dir(name: str) -> Path:
    """输入：测试名；输出：工作区内的临时目录；作用：避免 Windows Temp ACL 影响测试。"""
    path = Path.cwd() / ".tmp" / f"kg-delete-{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_remove_uploaded_file_inside_upload_root() -> None:
    """uploads 内的原文件可以删除。"""
    root = _workspace_dir("inside")
    try:
        uploads = root / "uploads"
        uploads.mkdir()
        target = uploads / "doc.md"
        target.write_text("x", encoding="utf-8")
        _remove_uploaded_file(str(uploads), str(target))
        assert not target.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_remove_uploaded_file_outside_upload_root_is_ignored() -> None:
    """不在 uploads 内的路径不能借删除接口删除。"""
    root = _workspace_dir("outside")
    try:
        uploads = root / "uploads"
        uploads.mkdir()
        outside = root / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        _remove_uploaded_file(str(uploads), str(outside))
        assert outside.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_remove_uploaded_file_missing_is_safe() -> None:
    """文件不存在时不抛异常。"""
    root = _workspace_dir("missing")
    try:
        uploads = root / "uploads"
        uploads.mkdir()
        _remove_uploaded_file(str(uploads), str(uploads / "missing.md"))
    finally:
        shutil.rmtree(root, ignore_errors=True)
