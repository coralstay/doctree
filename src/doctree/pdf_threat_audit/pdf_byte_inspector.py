# ---
# title: pdf_byte_inspector
# description: '로컬 PDF의 정적 무결성과 위험 태그(/JavaScript, /Launch 등)를 바이트 수준으로 검사한다'
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""로컬 PDF 정적 무결성 및 위험 태그(/JavaScript, /Launch 등) 바이트 검사기."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from doctree.runtime_env.workspace_settings import (
    PathLike,
    compute_sha256,
    discover_local_pdfs,
)

__all__ = [
    "DANGEROUS_PDF_TAGS",
    "LocalPdfAudit",
    "compute_sha256",
    "discover_local_pdfs",
    "inspect_pdf_bytes",
]

if TYPE_CHECKING:
    from doctree.pdf_threat_audit.threat_intel_client import VtVerdict

DANGEROUS_PDF_TAGS: Final[tuple[tuple[re.Pattern[bytes], str], ...]] = (  # ---- 1
    (re.compile(rb"/JavaScript(?![A-Za-z0-9_])"), "/JavaScript"),
    (re.compile(rb"/S\s*/JS(?![A-Za-z0-9_])"), "/JS"),
    (re.compile(rb"/Launch(?![A-Za-z0-9_])"), "/Launch"),
    (re.compile(rb"/EmbeddedFiles(?![A-Za-z0-9_])"), "/EmbeddedFiles"),
    (re.compile(rb"/RichMedia(?![A-Za-z0-9_])"), "/RichMedia"),
    (re.compile(rb"/XFA(?![A-Za-z0-9_])"), "/XFA"),
)


@dataclass(frozen=True, slots=True)
class LocalPdfAudit:
    """로컬 PDF 파일 1개의 정적 보안 및 무결성 점검 결과."""

    path: str
    sha256: str
    valid_pdf_header: bool
    valid_pdf_eof: bool
    dangerous_tags: tuple[str, ...] = ()
    vt_verdict: VtVerdict | None = None

    @property
    def is_safe(self) -> bool:  # ---- 2
        if not self.valid_pdf_header or not self.valid_pdf_eof:
            return False
        if self.dangerous_tags:
            return False
        if self.vt_verdict is None:
            return True
        return (
            self.vt_verdict.status not in ("malicious", "suspicious")
            and self.vt_verdict.malicious <= 0
            and self.vt_verdict.suspicious <= 0
        )


_MIN_PDF_BYTE_SIZE: Final[int] = 8
_PDF_HEADER_SCAN_BYTES: Final[int] = 1024
_PDF_TAIL_SCAN_BYTES: Final[int] = 65536
_TAG_SCAN_CHUNK_BYTES: Final[int] = 1024 * 1024
_TAG_BOUNDARY_OVERLAP_BYTES: Final[int] = 64


def inspect_pdf_bytes(path: PathLike) -> tuple[bool, bool, tuple[str, ...]]:  # ---- 3
    """로컬 PDF 바이트를 스캔해 (%PDF- 헤더 유무, %%EOF 유무, 위험 태그 목록)을 반환한다."""
    pdf_path = Path(path)
    if not pdf_path.is_file():
        return False, False, ()
    file_size = pdf_path.stat().st_size
    if file_size < _MIN_PDF_BYTE_SIZE:
        return False, False, ()

    with pdf_path.open("rb") as pdf_stream:
        header_bytes = pdf_stream.read(_PDF_HEADER_SCAN_BYTES)
        valid_header = b"%PDF-" in header_bytes

        tail_length = min(file_size, _PDF_TAIL_SCAN_BYTES)
        pdf_stream.seek(file_size - tail_length)
        tail_bytes = pdf_stream.read(tail_length)
        valid_eof = b"%%EOF" in tail_bytes

        found_tags: set[str] = set()
        pdf_stream.seek(0)
        boundary_overlap = b""
        while chunk := pdf_stream.read(_TAG_SCAN_CHUNK_BYTES):
            scan_window = boundary_overlap + chunk
            for pattern, label in DANGEROUS_PDF_TAGS:
                if label not in found_tags and pattern.search(scan_window):
                    found_tags.add(label)
            boundary_overlap = chunk[-_TAG_BOUNDARY_OVERLAP_BYTES:]

    ordered_tags = tuple(label for _, label in DANGEROUS_PDF_TAGS if label in found_tags)
    return valid_header, valid_eof, ordered_tags


# ======== 노트 ========
# 1 위험 PDF 태그 패턴
#   설명: 자동 실행, 스크립트, 외부 명령 실행에 악용되는 PDF 사전 객체 패턴(/JavaScript, /JS, /Launch, /EmbeddedFiles, /RichMedia, /XFA). 뒤의 부정 전방탐색은 /JavaScriptFoo 같은 다른 이름을 오탐하지 않기 위한 것이다.
#   개념: PDF 객체 이름(name object)
# 2 안전 판정 규칙
#   설명: 헤더/EOF가 깨졌거나 위험 태그가 있으면 안전하지 않다. 평판 결과가 있으면 함께 반영한다. doctree audit의 종료 코드가 이 값에 의존한다.
#   관련: src/doctree/doctree_cli/command_dispatcher.py (cmd_audit)
# 3 청크 스캔과 경계 겹침
#   설명: 파일 전체를 1MiB 청크로 읽되 청크 경계에 걸친 태그를 놓치지 않도록 64바이트를 겹쳐 본다. 헤더는 앞 1KiB, %%EOF는 뒤 64KiB에서만 찾는다. 8바이트 미만 파일은 무효다.
#   개념: 청크 경계 처리
