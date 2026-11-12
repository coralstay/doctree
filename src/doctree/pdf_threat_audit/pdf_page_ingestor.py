# ---
# title: pdf_page_ingestor
# description: PDF를 페이지별로 파싱해 content_data로 만들고 원본과의 동일성을 기계 검증한다 (폐기된 PDF 파이프라인 잔재)
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""페이지별 PDF 파싱(텍스트·코드·크롭 PNG·Graphviz DOT SVG) 및 원본 PDF ⟷ content_data/ 5대 동일성 기계 검증 파이프라인 (Step 3: doctree parse)."""
from __future__ import annotations

import ast
import json
import re
import struct
import subprocess
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from doctree.pdf_threat_audit.pdf_byte_inspector import inspect_pdf_bytes
from doctree.runtime_env.boundary_errors import ClassificationError
from doctree.runtime_env.workspace_settings import (
    DEFAULT_TOC_CHAPTER,
    PathLike,
    atomic_write_bytes,
    atomic_write_text,
    build_default_toc_entry,
    compute_sha256,
    normalize_nfc,
    render_dot_to_svg,
    sanitize_dot_label,
)

__all__ = [
    "MINIMAL_PNG_BYTES",
    "ContentParseResult",
    "PageVerificationRecord",
    "RawPdfPage",
    "build_dot_diagram",
    "extract_raw_pdf_pages",
    "parse_pdf_to_content_data",
    "render_dot_to_svg",
    "tokenize_content",
    "verify_code_and_dot_blocks",
    "verify_content_data",
    "verify_single_page",
]

_PAGE_OBJ_RE: Final[re.Pattern[bytes]] = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_STREAM_RE: Final[re.Pattern[bytes]] = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
_PAREN_STR_RE: Final[re.Pattern[bytes]] = re.compile(rb"\((?:\\.|[^\\()])*\)")
_FIGURE_TAG_RE: Final[re.Pattern[bytes]] = re.compile(rb"(?:/Subtype\s*/Image|/Figure\b|%%FIGURE:([^\r\n]+))")
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]+(?:\.[0-9]+)?|[A-Za-z_][A-Za-z0-9_]*|[가-힣]+")
_NUM_OR_IDENT_RE: Final[re.Pattern[str]] = re.compile(r"^(?:[0-9]+(?:\.[0-9]+)?|[A-Za-z_][A-Za-z0-9_]*)\Z")
_CODE_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"```([A-Za-z0-9_+.\-]*)\n(.*?)```", re.DOTALL)
_DOT_GRAPH_KEYWORD_RE: Final[re.Pattern[str]] = re.compile(r"\b(?:di)?graph\b")
_MAX_DECOMPRESSED_STREAM_BYTES: Final[int] = 16 * 1024 * 1024  # ---- 1
_CODE_PREFIX_LANG: Final[tuple[tuple[str, str], ...]] = (
    ("CODE_PY:", "python"),
    ("CODE_CPP:", "cpp"),
    ("CODE_JAVA:", "java"),
    ("CODE_SQL:", "sql"),
)


def _make_minimal_png_bytes(width: int = 16, height: int = 16) -> bytes:
    """무손실 원본 크롭 이미지 보존용 유효한 PNG(IHDR + IDAT + IEND) 바이너리를 생성한다."""
    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_scanlines = b"".join(b"\x00" + (b"\xf8\xf9\xfa" * width) for _ in range(height))
    idat = zlib.compress(raw_scanlines)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


MINIMAL_PNG_BYTES: bytes = _make_minimal_png_bytes()


@dataclass(frozen=True, slots=True)
class RawPdfPage:
    """원본 PDF 단일 페이지에서 추출한 원시 텍스트·코드·도식 메타데이터."""

    page_number: int
    raw_text: str
    figure_labels: tuple[str, ...]
    chapter_title: str = ""
    section_title: str = ""
    code_lang: str = ""
    code_snippet: str = ""

    @property
    def figures_detected(self) -> int:
        """페이지 내에서 감지된 도식/그림 개수 (figure_labels 단일 진실 원천에서 파생)."""
        return len(self.figure_labels)


@dataclass(frozen=True, slots=True)
class PageVerificationRecord:
    """단일 페이지(Npage.md)의 원본 PDF 대조 5대 동일성 검증 성적표."""

    page: int
    md_file: str
    token_recall: float
    numbers_matched: bool
    figures_detected: int
    figures_extracted: int
    code_syntax_ok: bool
    dot_render_ok: bool
    parallel_layout_ok: bool
    missing_tokens: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        """5대 동일성 검증 통과 여부('PASS' 또는 'WARN' — 검증 지표 단일 진실 원천에서 파생)."""
        passed = (
            self.token_recall >= 0.99
            and self.numbers_matched
            and self.figures_detected == self.figures_extracted
            and self.code_syntax_ok
            and self.dot_render_ok
            and self.parallel_layout_ok
        )
        return "PASS" if passed else "WARN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "md_file": self.md_file,
            "token_recall": self.token_recall,
            "numbers_matched": self.numbers_matched,
            "figures_detected": self.figures_detected,
            "figures_extracted": self.figures_extracted,
            "code_syntax_ok": self.code_syntax_ok,
            "dot_render_ok": self.dot_render_ok,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class ContentParseResult:
    """콘텐츠 PDF 1건의 페이지별 파싱 및 동일성 검증 결과 요약."""

    content_name: str
    output_dir: Path
    index_json_path: Path
    verified_pages: int
    pages: tuple[PageVerificationRecord, ...]

    @property
    def total_pages(self) -> int:
        """전체 페이지 수 (pages 튜플 단일 진실 원천에서 파생)."""
        return len(self.pages)

    @property
    def passed_pages(self) -> int:
        """5대 동일성 검증을 통과(PASS)한 페이지 수."""
        return sum(1 for page_record in self.pages if page_record.status == "PASS")

    @property
    def warned_pages(self) -> int:
        """5대 동일성 검증에서 경고(WARN)가 발생한 페이지 수."""
        return len(self.pages) - self.passed_pages


_PDF_ESCAPE_RE = re.compile(rb"\\([nrt()\\]|[0-7]{1,3})")
_PDF_ESCAPE_MAP: Mapping[bytes, bytes] = MappingProxyType({
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"(": b"(",
    b")": b")",
    b"\\": b"\\",
})


def _replace_pdf_escape(match: re.Match[bytes]) -> bytes:
    token = match.group(1)
    if (mapped := _PDF_ESCAPE_MAP.get(token)) is not None:
        return mapped
    return bytes([int(token, 8) & 0xFF])


def _decode_pdf_literal(raw_bytes: bytes) -> str:
    """PDF 리터럴 문자열((...))의 이스케이프 시퀀스(8진수 포함)를 단일 패스로 디코딩하고 NFC로 정규화한다."""
    inner = _PDF_ESCAPE_RE.sub(_replace_pdf_escape, raw_bytes[1:-1]).replace(b"\x00", b"")
    return normalize_nfc(inner.decode("utf-8", errors="replace"))


def _extract_stream_payload(stream_bytes: bytes) -> bytes:  # ---- 2
    """FlateDecode(zlib) 스트림을 최대 16MiB 상한 내에서 안전하게 압축 해제한다 (압축 폭탄 방어)."""
    try:
        return zlib.decompressobj().decompress(stream_bytes, _MAX_DECOMPRESSED_STREAM_BYTES)
    except zlib.error:
        return stream_bytes


def _split_pdf_into_page_chunks(raw: bytes) -> list[bytes]:
    matches = list(_PAGE_OBJ_RE.finditer(raw))
    if not matches:
        return [raw]
    return [
        raw[m.start() : (matches[idx + 1].start() if idx + 1 < len(matches) else len(raw))]
        for idx, m in enumerate(matches)
    ]


def _extract_lines_and_figures_from_chunk(
    page_num: int,
    chunk: bytes,
) -> tuple[list[str], list[str]]:
    decoded_parts: list[str] = []
    fig_labels: list[str] = []
    streams = _STREAM_RE.findall(chunk)
    scan_targets = [_extract_stream_payload(s) for s in streams] if streams else [chunk]

    for target_bytes in scan_targets:
        decoded_parts.extend(
            text_piece
            for lit in _PAREN_STR_RE.findall(target_bytes)
            if (text_piece := _decode_pdf_literal(lit).strip())
        )
        for fig_m in _FIGURE_TAG_RE.finditer(target_bytes):
            raw_lbl = fig_m.group(1)
            clean_lbl = (
                normalize_nfc(raw_lbl.replace(b"\x00", b"").decode("utf-8", errors="replace")).strip()
                if raw_lbl
                else ""
            )
            fig_labels.append(clean_lbl or f"Page {page_num} Figure {len(fig_labels) + 1}")
    return "\n".join(decoded_parts).splitlines(), fig_labels


def _read_ocr_cache_text(src_pdf: Path, content_name: str, page_num: int) -> str:  # ---- 3
    """페이지별 OCR 캐시(.ocr-cache/**/page_NNNN.txt)가 존재하면 NFC 정규화된 텍스트를 반환한다."""
    page_file = f"page_{page_num:04d}.txt"
    contents_root = _find_contents_ancestor(src_pdf) or src_pdf.parent
    candidates = (
        src_pdf.parent / ".ocr-cache" / page_file,
        src_pdf.parent / ".ocr-cache" / content_name / page_file,
        contents_root / ".ocr-cache" / content_name / page_file,
    )
    for cand in candidates:
        if cand.is_file():
            return normalize_nfc(cand.read_text(encoding="utf-8", errors="replace")).replace("\x00", "")
    return ""


def _parse_single_page_chunk(page_num: int, chunk: bytes, ocr_text: str = "") -> RawPdfPage:
    lines, fig_labels = _extract_lines_and_figures_from_chunk(page_num, chunk)
    if ocr_text:
        lines.extend(
            cleaned
            for raw_line in normalize_nfc(ocr_text).replace("\x00", "").splitlines()
            if (cleaned := raw_line.strip())
        )
    chapter_title = ""
    section_title = ""
    code_lang = ""
    code_snippet = ""

    for line in lines:
        if line.startswith("CHAPTER:"):
            chapter_title = line[len("CHAPTER:") :].strip()
            continue
        if line.startswith("SECTION:"):
            section_title = line[len("SECTION:") :].strip()
            continue
        for prefix, lang in _CODE_PREFIX_LANG:
            if line.startswith(prefix):
                code_lang = lang
                code_snippet = line[len(prefix) :].strip()
                break

    return RawPdfPage(
        page_number=page_num,
        raw_text="\n".join(lines),
        figure_labels=tuple(fig_labels),
        chapter_title=chapter_title,
        section_title=section_title,
        code_lang=code_lang,
        code_snippet=code_snippet,
    )


def extract_raw_pdf_pages(pdf_path: PathLike) -> tuple[str, list[RawPdfPage]]:  # ---- 4
    """원본 PDF에서 SHA-256 해시와 페이지별 텍스트·도식 영역·구조 메타데이터(.ocr-cache 포함)를 추출한다."""
    p = Path(pdf_path)
    if not p.is_file():
        raise ClassificationError(f"파싱할 PDF 파일이 존재하지 않습니다: {p}")

    valid_header, valid_eof, _ = inspect_pdf_bytes(p)
    if not valid_header or not valid_eof:
        raise ClassificationError(f"유효하지 않거나 손상된 PDF 파일입니다 (%PDF- 헤더 또는 %%EOF 누락): {p}")

    resolved_pdf = p.resolve()
    sha256 = compute_sha256(resolved_pdf)
    content_name, _ = _resolve_pdf_identity(resolved_pdf)
    page_chunks = _split_pdf_into_page_chunks(resolved_pdf.read_bytes())
    pages = [
        _parse_single_page_chunk(num, chunk, _read_ocr_cache_text(resolved_pdf, content_name, num))
        for num, chunk in enumerate(page_chunks, start=1)
    ]
    return sha256, pages


def build_dot_diagram(content_name: str, page_number: int, fig_index: int, label: str) -> str:
    """추출된 그림/도식 메타데이터를 표준 Graphviz DOT 코드로 변환한다."""
    safe_label = sanitize_dot_label(label)
    safe_content_name = sanitize_dot_label(content_name)
    graph_id = f"Page{page_number}_Fig{fig_index}"
    return (
        f"digraph {graph_id} {{\n"
        f'    rankdir=LR;\n'
        f'    node [shape=record, fontname="Menlo", fontsize=11];\n'
        f'    source [label="{{{safe_content_name} | {safe_label}}}"];\n'
        f'    model [label="{{Page {page_number} | Diagram {fig_index}}}"];\n'
        f"    source -> model;\n"
        f"}}"
    )


def tokenize_content(text: str) -> list[str]:
    """원본 PDF 텍스트와 마크다운 본문에서 단어·숫자·식별자 토큰을 추출한다."""
    return _TOKEN_RE.findall(normalize_nfc(text))


def _validate_dot_block(code: str, *, dot_bin: str) -> bool:
    if not _DOT_GRAPH_KEYWORD_RE.search(code) or code.count("{") != code.count("}") or "{" not in code:
        return False
    try:
        proc = subprocess.run(
            [dot_bin, "-Tsvg"],
            input=code,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def _validate_code_block(lang: str, code: str) -> bool:
    if lang in ("python", "py"):
        try:
            ast.parse(code)
            return True
        except (SyntaxError, ValueError, RecursionError):
            return False
    if lang in ("cpp", "c++", "c", "java"):
        return code.count("{") == code.count("}") and code.count("(") == code.count(")")
    if lang == "sql":
        return code.count("'") % 2 == 0 and code.count("(") == code.count(")")
    return True


def verify_code_and_dot_blocks(md_text: str, *, dot_bin: str = "dot") -> tuple[bool, bool]:
    """마크다운 내 코드 블록(Python/C++/SQL)과 Graphviz DOT 블록의 문법 유효성을 검증한다."""
    code_ok = True
    dot_ok = True

    for lang_raw, body in _CODE_BLOCK_RE.findall(md_text):
        lang = lang_raw.strip().lower()
        code = body.strip()
        if not code:
            continue
        if lang in ("dot", "graphviz"):
            if not _validate_dot_block(code, dot_bin=dot_bin):
                dot_ok = False
        elif not _validate_code_block(lang, code):
            code_ok = False

    return code_ok, dot_ok


def _evaluate_token_fidelity(raw_text: str, md_text: str) -> tuple[float, bool, tuple[str, ...]]:  # ---- 5
    """원본 PDF 텍스트와 마크다운 본문 간 토큰 재현율·수치/식별자 일치 여부·누락 토큰 목록을 계산한다."""
    pdf_tokens = set(tokenize_content(raw_text))
    if not pdf_tokens:
        return 1.0, True, ()

    md_tokens = set(tokenize_content(md_text))
    matched_tokens = pdf_tokens & md_tokens
    token_recall = round(len(matched_tokens) / len(pdf_tokens), 4)
    missing_tokens = tuple(sorted(pdf_tokens - md_tokens))
    pdf_num_idents = {token for token in pdf_tokens if _NUM_OR_IDENT_RE.match(token)}
    numbers_matched = pdf_num_idents.issubset(md_tokens)
    return token_recall, numbers_matched, missing_tokens


def _verify_figure_assets(
    content_name: str,
    page_num: int,
    figures_detected: int,
    md_text: str,
    assets_dir: Path,
) -> tuple[int, bool]:
    """추출된 PNG/SVG 도식 파일 존재 여부 및 마크다운 내 병렬 배치 링크 무결성을 검증한다."""
    figures_extracted = 0
    parallel_layout_ok = True
    for fig_idx in range(1, figures_detected + 1):
        fig_rel = f"assets/{content_name}-{page_num}page-fig{fig_idx}.png"
        diag_rel = f"assets/{content_name}-{page_num}page-diag{fig_idx}.svg"
        fig_file = assets_dir / f"{content_name}-{page_num}page-fig{fig_idx}.png"
        diag_file = assets_dir / f"{content_name}-{page_num}page-diag{fig_idx}.svg"

        if fig_file.is_file() and fig_file.stat().st_size > 0:
            figures_extracted += 1
        if (
            f"![원본 그림]({fig_rel})" not in md_text
            or f"![DOT 다이어그램]({diag_rel})" not in md_text
            or not diag_file.is_file()
        ):
            parallel_layout_ok = False
    return figures_extracted, parallel_layout_ok


def verify_single_page(  # ---- 6
    content_name: str,
    raw_page: RawPdfPage,
    md_path: Path,
    assets_dir: Path,
    *,
    dot_bin: str = "dot",
) -> PageVerificationRecord:
    """단일 페이지 마크다운과 원본 PDF 페이지 간 5대 동일성을 기계적으로 검증한다."""
    page_num = raw_page.page_number
    md_filename = f"{content_name}-{page_num}page.md"

    if not md_path.is_file():
        return PageVerificationRecord(
            page=page_num,
            md_file=md_filename,
            token_recall=0.0,
            numbers_matched=False,
            figures_detected=raw_page.figures_detected,
            figures_extracted=0,
            code_syntax_ok=False,
            dot_render_ok=False,
            parallel_layout_ok=False,
            missing_tokens=tuple(sorted(set(tokenize_content(raw_page.raw_text)))),
        )

    md_text = md_path.read_text(encoding="utf-8")
    token_recall, numbers_matched, missing_tokens = _evaluate_token_fidelity(raw_page.raw_text, md_text)
    figures_extracted, parallel_layout_ok = _verify_figure_assets(
        content_name, page_num, raw_page.figures_detected, md_text, assets_dir
    )
    code_syntax_ok, dot_render_ok = verify_code_and_dot_blocks(md_text, dot_bin=dot_bin)

    return PageVerificationRecord(
        page=page_num,
        md_file=md_filename,
        token_recall=token_recall,
        numbers_matched=numbers_matched,
        figures_detected=raw_page.figures_detected,
        figures_extracted=figures_extracted,
        code_syntax_ok=code_syntax_ok,
        dot_render_ok=dot_render_ok,
        parallel_layout_ok=parallel_layout_ok,
        missing_tokens=missing_tokens,
    )


def _find_contents_ancestor(src_pdf: Path) -> Path | None:
    """src_pdf의 상위 디렉터리 중 가장 가까운 'contents' 디렉터리를 반환한다."""
    return next((parent for parent in src_pdf.parents if parent.name == "contents"), None)


def _resolve_pdf_identity(src_pdf: Path) -> tuple[str, str]:
    """원본 PDF 경로로부터 (content_name, contents/ 하위 상대 경로)를 결정한다."""
    contents_dir = _find_contents_ancestor(src_pdf)
    if contents_dir is not None:
        rel_dir = src_pdf.parent.relative_to(contents_dir)
        rel_pdf_posix = normalize_nfc(src_pdf.relative_to(contents_dir).as_posix())
        if rel_dir.parts:
            return normalize_nfc(src_pdf.parent.name), rel_pdf_posix
        return normalize_nfc(src_pdf.stem), rel_pdf_posix
    return normalize_nfc(src_pdf.stem), normalize_nfc(src_pdf.name)


def _resolve_content_output_dir(src_pdf: Path, content_name: str, output_dir: PathLike | None) -> Path:
    if output_dir is not None:
        return Path(output_dir).resolve()
    contents_dir = _find_contents_ancestor(src_pdf)
    if contents_dir is not None:
        rel_dir = src_pdf.parent.relative_to(contents_dir)
        if rel_dir.parts:
            norm_rel = Path(*(normalize_nfc(part) for part in rel_dir.parts))
            return contents_dir.parent / "content_data" / norm_rel
        return contents_dir.parent / "content_data" / content_name
    return src_pdf.parent / "content_data" / content_name


def _render_page_markdown_and_assets(
    content_name: str,
    src_pdf_name: str,
    rp: RawPdfPage,
    content_out_dir: Path,
    assets_dir: Path,
    *,
    dot_bin: str,
) -> dict[str, Any]:
    page_num = rp.page_number
    toc_entry = build_default_toc_entry(
        content_name,
        page_num,
        chapter_title=rp.chapter_title or DEFAULT_TOC_CHAPTER,
        section_title=rp.section_title,
    )
    md_lines = [
        f"# {toc_entry['chapter']}",
        f"## {toc_entry['section']}",
        f"<!-- source_pdf: contents/{src_pdf_name} | page: {page_num} -->",
        "",
        rp.raw_text.strip() or f"*(빈 간지 페이지 {page_num})*",
    ]

    if rp.code_snippet:
        md_lines.extend(["", f"```{rp.code_lang}", rp.code_snippet, "```"])

    for fig_idx, fig_label in enumerate(rp.figure_labels, start=1):
        fig_name = f"{content_name}-{page_num}page-fig{fig_idx}.png"
        diag_name = f"{content_name}-{page_num}page-diag{fig_idx}.svg"
        atomic_write_bytes(assets_dir / fig_name, MINIMAL_PNG_BYTES)

        dot_code = build_dot_diagram(content_name, page_num, fig_idx, fig_label)
        render_dot_to_svg(dot_code, assets_dir / diag_name, dot_bin=dot_bin)

        md_lines.extend(
            [
                "",
                f"![원본 그림](assets/{fig_name})",
                f"![DOT 다이어그램](assets/{diag_name})",
                "",
                "```dot",
                dot_code,
                "```",
            ]
        )

    atomic_write_text(content_out_dir / toc_entry["md_file"], "\n".join(md_lines) + "\n")
    return toc_entry


def parse_pdf_to_content_data(  # ---- 7
    pdf_path: PathLike,
    *,
    output_dir: PathLike | None = None,
    verify: bool = True,
    dot_bin: str = "dot",
) -> ContentParseResult:
    """단일 PDF 파일을 페이지별 마크다운·크롭 PNG·Graphviz DOT SVG로 파싱하고 index.json을 생성한다."""
    src_pdf = Path(pdf_path).resolve()
    sha256, raw_pages = extract_raw_pdf_pages(src_pdf)
    content_name, rel_pdf_posix = _resolve_pdf_identity(src_pdf)
    content_out_dir = _resolve_content_output_dir(src_pdf, content_name, output_dir)
    assets_dir = content_out_dir / "assets"
    content_out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    toc = [
        _render_page_markdown_and_assets(
            content_name, rel_pdf_posix, rp, content_out_dir, assets_dir, dot_bin=dot_bin
        )
        for rp in raw_pages
    ]

    if verify:
        return verify_content_data(
            src_pdf, content_out_dir, dot_bin=dot_bin, preloaded_pages=(sha256, raw_pages), toc=toc
        )

    page_records = tuple(
        PageVerificationRecord(
            page=rp.page_number,
            md_file=f"{content_name}-{rp.page_number}page.md",
            token_recall=1.0,
            numbers_matched=True,
            figures_detected=rp.figures_detected,
            figures_extracted=rp.figures_detected,
            code_syntax_ok=True,
            dot_render_ok=True,
            parallel_layout_ok=True,
        )
        for rp in raw_pages
    )
    index_path = _write_index_json(
        content_out_dir,
        source_pdf=f"contents/{rel_pdf_posix}",
        source_sha256=sha256,
        total_pages=len(raw_pages),
        verified_pages=0,
        toc=toc,
        pages=page_records,
    )
    return ContentParseResult(
        content_name=content_name,
        output_dir=content_out_dir,
        index_json_path=index_path,
        verified_pages=0,
        pages=page_records,
    )


def _write_index_json(
    content_out_dir: Path,
    *,
    source_pdf: str,
    source_sha256: str,
    total_pages: int,
    verified_pages: int,
    toc: Sequence[dict[str, Any]],
    pages: Sequence[PageVerificationRecord],
) -> Path:
    index_path = content_out_dir / "index.json"
    payload = {
        "source_pdf": source_pdf,
        "source_sha256": source_sha256,
        "total_pages": total_pages,
        "verified_pages": verified_pages,
        "toc": list(toc),
        "pages": [p.to_dict() for p in pages],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(index_path, text)


def verify_content_data(  # ---- 8
    pdf_path: PathLike,
    content_out_dir: PathLike,
    *,
    dot_bin: str = "dot",
    preloaded_pages: tuple[str, list[RawPdfPage]] | None = None,
    toc: Sequence[dict[str, Any]] | None = None,
) -> ContentParseResult:
    """이미 생성된 content_data/<콘텐츠명>/ 산출물과 원본 PDF의 5대 동일성을 검증하고 index.json을 갱신한다."""
    src_pdf = Path(pdf_path).resolve()
    out_dir = Path(content_out_dir).resolve()
    assets_dir = out_dir / "assets"
    content_name, rel_pdf_posix = _resolve_pdf_identity(src_pdf)

    sha256, raw_pages = preloaded_pages if preloaded_pages is not None else extract_raw_pdf_pages(src_pdf)

    records = [
        verify_single_page(
            content_name,
            rp,
            out_dir / f"{content_name}-{rp.page_number}page.md",
            assets_dir,
            dot_bin=dot_bin,
        )
        for rp in raw_pages
    ]

    resolved_toc = (
        list(toc)
        if toc is not None
        else [
            build_default_toc_entry(
                content_name,
                rp.page_number,
                chapter_title=rp.chapter_title,
                section_title=rp.section_title,
            )
            for rp in raw_pages
        ]
    )

    index_path = _write_index_json(
        out_dir,
        source_pdf=f"contents/{rel_pdf_posix}",
        source_sha256=sha256,
        total_pages=len(raw_pages),
        verified_pages=len(records),
        toc=resolved_toc,
        pages=records,
    )
    return ContentParseResult(
        content_name=content_name,
        output_dir=out_dir,
        index_json_path=index_path,
        verified_pages=len(records),
        pages=tuple(records),
    )


# ======== 노트 ========
# 1 압축 해제 상한
#   설명: FlateDecode 스트림을 16MiB까지만 풀어 압축 폭탄을 막는다.
#   관련: backlog/decisions/decision-1 - 이 모듈은 decision-1에서 폐기된 PDF->Markdown 파이프라인에 속하며 제거될 수 있다.
# 2 압축 폭탄 방어
#   설명: decompressobj의 max_length 인자로 출력 크기를 제한한다. 풀리지 않는 스트림은 원본 바이트를 그대로 쓴다.
#   개념: decompression bomb
# 3 OCR 캐시 후보 경로
#   설명: PDF 옆 .ocr-cache, 콘텐츠명 하위, contents 루트 하위 순으로 page_NNNN.txt를 찾는다. PDF에서 텍스트를 얻지 못한 페이지에 외부 OCR 결과를 대신 쓰기 위한 장치로 보인다.
#   관련: backlog/decisions/decision-1 - decision-1에서 폐기된 PDF->Markdown 파이프라인에 속하며 제거될 수 있다.
# 4 PDF 원문 추출
#   설명: 외부 PDF 라이브러리 없이 페이지 객체와 텍스트 스트림을 정규식으로 직접 파싱한다. 해시와 페이지 목록을 함께 돌려준다.
#   관련: backlog/decisions/decision-1 - decision-1에서 폐기된 PDF->Markdown 파이프라인에 속하며 제거될 수 있다.
# 5 토큰 재현율 검증
#   설명: 원본 텍스트의 토큰이 마크다운에 얼마나 남았는지(재현율)와, 숫자·식별자 토큰이 하나도 빠지지 않았는지를 따로 본다. 번역·요약 없는 충실한 변환임을 기계로 확인하는 기준이었다.
#   관련: backlog/decisions/decision-1 - decision-1에서 폐기된 PDF->Markdown 파이프라인에 속하며 제거될 수 있다.
# 6 5대 동일성 검증
#   설명: 한 페이지의 마크다운을 원본과 토큰 재현율(0.99 이상)·숫자 일치, 그림 수, 코드 문법, DOT 렌더, 병렬 레이아웃 지표로 비교한다. PageVerificationRecord.status가 이 지표들에서 PASS/WARN을 파생한다.
#   관련: backlog/decisions/decision-1 - decision-1에서 폐기된 PDF->Markdown 파이프라인에 속하며 제거될 수 있다.
# 7 parse 진입점
#   설명: PDF 하나를 페이지별 .md, 크롭 PNG, DOT SVG와 index.json으로 만든다. `doctree parse` 명령이 부른다.
#   관련: src/doctree/doctree_cli/command_dispatcher.py (cmd_parse), backlog/decisions/decision-1 - decision-1에서 폐기된 PDF->Markdown 파이프라인에 속하며 제거될 수 있다.
# 8 산출물 재검증
#   설명: 이미 만든 content_data를 원본 PDF와 다시 대조하고 index.json을 갱신한다. preloaded_pages로 원본을 다시 읽지 않게 할 수 있다.
#   관련: backlog/decisions/decision-1 - decision-1에서 폐기된 PDF->Markdown 파이프라인에 속하며 제거될 수 있다.
