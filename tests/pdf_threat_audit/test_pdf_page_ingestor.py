"""페이지별 PDF 파싱·크롭 PNG·Graphviz DOT SVG 및 5대 동일성 기계 검증(pdf_page_ingestor.py) 단위·통합 테스트."""
from __future__ import annotations

import io
import json
import subprocess
import unicodedata
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from rich.console import Console

from doctree.doctree_cli import command_dispatcher as cli
from doctree.pdf_threat_audit.pdf_page_ingestor import (
    _MAX_DECOMPRESSED_STREAM_BYTES,
    RawPdfPage,
    _extract_stream_payload,
    _parse_single_page_chunk,
    _split_pdf_into_page_chunks,
    build_dot_diagram,
    extract_raw_pdf_pages,
    parse_pdf_to_content_data,
    render_dot_to_svg,
    tokenize_content,
    verify_code_and_dot_blocks,
    verify_content_data,
    verify_single_page,
)
from doctree.runtime_env.boundary_errors import ClassificationError


def _build_multi_page_pdf(path: Path) -> Path:
    """FlateDecode 압축 스트림, 일반 스트림, 빈 간지 페이지, 그림 태그, Python/C++/SQL 코드를 포함한 4페이지 PDF 생성."""
    p1_stream = zlib.compress(
        (
            "(CHAPTER: 01_인덱스_튜닝)\n"
            "(SECTION: 01_BTree_구조)\n"
            "(BTree index depth 3 and key 50.)\n"
            "(CODE_PY: def calc\\(x\\): return x + 1)\n"
            "%%FIGURE:B+Tree Root to Leaf\n"
        ).encode()
    )
    p2_stream = (
        b"(CHAPTER: 01_\xec\x9d\xb8\xeb\x8d\xb1\xec\x8a\xa4_\xed\x8a\x9c\xeb\x8b\x9d)\n"
        b"(SECTION: 02_CPP_\xec\x8a\xac\xeb\xa1\xaf)\n"
        b"(CODE_CPP: int main\\(\\) { return 0; })\n"
        b"/Subtype /Image\n"
    )
    p3_stream = (
        b"(CHAPTER: 02_SQL_\xec\x8b\xa4\xed\x96\x89\xea\xb3\x84\xed\x9a\x8d)\n"
        b"(SECTION: 01_SELECT_\xec\xbf\xbc\xb8\xac)\n"
        b"(CODE_SQL: SELECT id FROM users WHERE id = 1;)\n"
    )
    # Page 4: 빈 간지 페이지
    p4_stream = b"()"

    pdf_bytes = (
        b"%PDF-1.7\n"
        b"1 0 obj << /Type /Page >>\nstream\n" + p1_stream + b"\nendstream\nendobj\n"
        b"2 0 obj << /Type /Page >>\nstream\n" + p2_stream + b"\nendstream\nendobj\n"
        b"3 0 obj << /Type /Page >>\nstream\n" + p3_stream + b"\nendstream\nendobj\n"
        b"4 0 obj << /Type /Page >>\nstream\n" + p4_stream + b"\nendstream\nendobj\n"
        b"%%EOF\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes)
    return path


def test_extract_and_parse_pdf_to_content_data_and_verify(tmp_path: Path):
    """다중 페이지 PDF 파싱(verify=True / False) 및 5대 동일성 검증(PASS / WARN) 전 분기를 검증한다."""
    # 1) 존재하지 않거나 손상된 PDF 예외 검증
    with pytest.raises(ClassificationError, match="존재하지 않습니다"):
        extract_raw_pdf_pages(tmp_path / "missing.pdf")

    bad_pdf = tmp_path / "corrupt.pdf"
    bad_pdf.write_bytes(b"NOT_A_PDF")
    with pytest.raises(ClassificationError, match="유효하지 않거나 손상된 PDF"):
        extract_raw_pdf_pages(bad_pdf)

    # /Type /Page가 없고 stream도 없는 최소 단일 청크 PDF 추출
    single_chunk_pdf = tmp_path / "single.pdf"
    single_chunk_pdf.write_bytes(b"%PDF-1.7\n(Single page literal text 100)\n%%EOF\n")
    _, single_pages = extract_raw_pdf_pages(single_chunk_pdf)
    assert len(single_pages) == 1
    assert "Single page literal text 100" in single_pages[0].raw_text

    # 2) 4페이지 표준 PDF 파싱 (contents/ 폴더 아래 배치 & 기본 출력 경로 & verify=False)
    proj = tmp_path / "databases"
    pdf_path = _build_multi_page_pdf(proj / "contents" / "친절한-SQL-튜닝.pdf")

    res_no_verify = parse_pdf_to_content_data(pdf_path, verify=False)
    assert res_no_verify.total_pages == 4
    assert res_no_verify.verified_pages == 0
    assert res_no_verify.passed_pages == 4

    # 3) verify=True 실행 및 index.json 검증
    res_verified = parse_pdf_to_content_data(pdf_path, verify=True)
    assert res_verified.total_pages == 4
    assert res_verified.verified_pages == 4
    assert res_verified.passed_pages == 4
    assert res_verified.warned_pages == 0
    index_data = json.loads(res_verified.index_json_path.read_text(encoding="utf-8"))
    assert index_data["total_pages"] == 4
    assert index_data["pages"][0]["figures_detected"] == 1
    assert index_data["pages"][0]["figures_extracted"] == 1
    assert index_data["pages"][0]["status"] == "PASS"

    # preloaded_pages=None 및 toc=None으로 verify_content_data 단독 호출
    re_verified = verify_content_data(pdf_path, res_verified.output_dir)
    assert re_verified.passed_pages == 4

    # contents/ 폴더가 아닌 경로 및 명시적 output_dir 지정 분기
    custom_out = tmp_path / "custom_content_data"
    res_single = parse_pdf_to_content_data(single_chunk_pdf, output_dir=custom_out, verify=True)
    assert res_single.output_dir == custom_out
    res_non_contents = parse_pdf_to_content_data(single_chunk_pdf, verify=False)
    assert res_non_contents.output_dir == tmp_path / "content_data" / "single"


def test_parse_pdf_directory_mirroring_and_ocr_cache(tmp_path: Path):
    """contents/ 하위 폴더 1:1 디렉토리명 미러링 및 .ocr-cache 연동을 검증한다."""
    proj = tmp_path / "databases"

    # 1) contents/ 하위 폴더(BPF-tooling 및 <트랙_또는_분류>/<도서_슬러그>) 1:1 디렉토리명 미러링 검증
    bpf_pdf = _build_multi_page_pdf(
        proj / "contents" / "BPF-tooling" / "BPF-성능-분석-도구.pdf"
    )
    res_bpf = parse_pdf_to_content_data(bpf_pdf, verify=True)
    assert res_bpf.content_name == "BPF-tooling"
    assert res_bpf.output_dir == proj / "content_data" / "BPF-tooling"
    assert (res_bpf.output_dir / "BPF-tooling-1page.md").is_file()
    assert (
        "<!-- source_pdf: contents/BPF-tooling/BPF-성능-분석-도구.pdf | page: 1 -->"
        in (res_bpf.output_dir / "BPF-tooling-1page.md").read_text(encoding="utf-8")
    )
    bpf_index = json.loads(res_bpf.index_json_path.read_text(encoding="utf-8"))
    assert bpf_index["source_pdf"] == "contents/BPF-tooling/BPF-성능-분석-도구.pdf"

    track_pdf = _build_multi_page_pdf(
        proj / "contents" / "linux-kernel" / "BPF-tooling" / "BPF-성능-분석-도구.pdf"
    )
    res_track = parse_pdf_to_content_data(track_pdf, verify=True)
    assert res_track.content_name == "BPF-tooling"
    assert res_track.output_dir == proj / "content_data" / "linux-kernel" / "BPF-tooling"
    track_index = json.loads(res_track.index_json_path.read_text(encoding="utf-8"))
    assert track_index["source_pdf"] == "contents/linux-kernel/BPF-tooling/BPF-성능-분석-도구.pdf"

    # 2) 스캔본 PDF (빈 텍스트 스트림) + .ocr-cache/page_0001.txt 연동 검증
    scanned_pdf = proj / "contents" / "spring" / "01_toby" / "스캔도서.pdf"
    scanned_pdf.parent.mkdir(parents=True, exist_ok=True)
    scanned_pdf.write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Page >>\nstream\n()\nendstream\nendobj\n%%EOF\n")
    ocr_file = proj / "contents" / ".ocr-cache" / "01_toby" / "page_0001.txt"
    ocr_file.parent.mkdir(parents=True, exist_ok=True)
    ocr_file.write_text(
        "CHAPTER: 01_IoC_Container\n\n"
        "SECTION: 01_BeanFactory\n"
        "PITFALL: Circular dependency in constructor injection\n"
        "CODE_JAVA: class BeanFactory { int id() { return 1; } }\n",
        encoding="utf-8",
    )
    res_ocr = parse_pdf_to_content_data(scanned_pdf, verify=True)
    assert res_ocr.passed_pages == 1
    ocr_md = (res_ocr.output_dir / "01_toby-1page.md").read_text(encoding="utf-8")
    assert "# 01_IoC_Container" in ocr_md
    assert "## 01_BeanFactory" in ocr_md
    assert "```java\nclass BeanFactory { int id() { return 1; } }\n```" in ocr_md


def test_verify_code_and_dot_blocks_and_single_page_warn_branches(tmp_path: Path):
    """코드 블록(Python/C++/SQL)·DOT 구문 검증 실패 및 페이지 누락/도식 누락 WARN 분기를 검증한다."""
    # 1) 빈 코드 블록, 잘못된 DOT 블록, dot 실행 실패(returncode != 0), dot 바이너리 없음(OSError)
    md_bad_dot = "```\n\n```\n```dot\ninvalid_dot_without_braces\n```\n"
    code_ok, dot_ok = verify_code_and_dot_blocks(md_bad_dot)
    assert code_ok is True
    assert dot_ok is False

    md_dot_syntax_err = "```dot\ndigraph G { a -> ; }\n```\n"
    _, dot_ok2 = verify_code_and_dot_blocks(md_dot_syntax_err, dot_bin="dot")
    assert dot_ok2 is False

    md_valid_dot = "```dot\ndigraph G { a -> b; }\n```\n"
    _, dot_ok3 = verify_code_and_dot_blocks(md_valid_dot, dot_bin="non_existent_dot_binary_xyz")
    assert dot_ok3 is True

    # 2) Python / C++ / SQL 문법 오류 및 정상 분기
    assert verify_code_and_dot_blocks("```python\ndef broken(:\n```\n") == (False, True)
    assert verify_code_and_dot_blocks("```cpp\nint main() { return 0;\n```\n") == (False, True)
    assert verify_code_and_dot_blocks("```sql\nSELECT 'unterminated FROM t;\n```\n") == (False, True)
    assert verify_code_and_dot_blocks("```sql\nSELECT * FROM t;\n```\n") == (True, True)
    assert verify_code_and_dot_blocks("```txt\nplain text\n```\n") == (True, True)

    # 3) render_dot_to_svg 폴백(존재하지 않는 dot 바이너리 지정 또는 잘못된 DOT 구문으로 returncode != 0일 때 fallback SVG 생성)
    fallback_svg = tmp_path / "assets" / "fallback.svg"
    assert render_dot_to_svg("digraph G { a -> b; }", fallback_svg, dot_bin="non_existent_dot_xyz") is True
    assert "DOT Fallback SVG" in fallback_svg.read_text(encoding="utf-8")
    fallback_svg2 = tmp_path / "assets" / "fallback2.svg"
    assert render_dot_to_svg("digraph G { broken -> ; }", fallback_svg2, dot_bin="dot") is True
    assert "DOT Fallback SVG" in fallback_svg2.read_text(encoding="utf-8")

    # 4) verify_single_page: 마크다운 파일이 삭제된 경우 및 도식 링크/파일이 누락된 경우 WARN 판정
    raw_page = RawPdfPage(
        page_number=1,
        raw_text="Important token 1024",
        figure_labels=("Fig1",),
    )
    missing_md_rec = verify_single_page("Book", raw_page, tmp_path / "missing.md", tmp_path / "assets")
    assert missing_md_rec.status == "WARN"
    assert missing_md_rec.token_recall == 0.0

    incomplete_md = tmp_path / "Book-1page.md"
    incomplete_md.write_text("# Only partial text without numbers or figure links\n", encoding="utf-8")
    warn_rec = verify_single_page("Book", raw_page, incomplete_md, tmp_path / "assets")
    assert warn_rec.status == "WARN"
    assert warn_rec.numbers_matched is False
    assert warn_rec.parallel_layout_ok is False
    assert "1024" in warn_rec.missing_tokens


def test_cli_parse_subcommand(tmp_path: Path):
    """CLI `doctree parse` 명령(--verify, --strict, 디렉터리/파일 대상, 오류 처리)을 검증한다."""
    proj = tmp_path / "databases"
    pdf_path = _build_multi_page_pdf(proj / "contents" / "친절한-SQL-튜닝.pdf")

    buf = io.StringIO()
    test_console = Console(file=buf, width=120, force_terminal=False, color_system=None)

    with patch.object(cli, "console", test_console):
        # 1) 프로젝트 디렉터리 지정 시 하위 contents/*.pdf 자동 탐색 및 --verify 통과
        assert cli.main(["parse", str(proj), "--verify", "--strict"]) == 0
        assert "📄 PDF 파싱 완료: 친절한-SQL-튜닝" in buf.getvalue()

        # 2) 단일 PDF 파일 직접 지정
        assert cli.main(["parse", str(pdf_path)]) == 0

        # 3) PDF가 없는 빈 폴더 지정 시 종료 코드 1
        empty_dir = tmp_path / "empty_proj"
        empty_dir.mkdir()
        assert cli.main(["parse", str(empty_dir)]) == 1

        # 4) --strict 모드에서 WARN 페이지 발생 시 종료 코드 1 반환
        warn_res = parse_pdf_to_content_data(pdf_path, verify=True)
        warn_record = warn_res.pages[0].__class__(
            page=1,
            md_file="x.md",
            token_recall=0.5,
            numbers_matched=False,
            figures_detected=1,
            figures_extracted=0,
            code_syntax_ok=False,
            dot_render_ok=False,
            parallel_layout_ok=False,
            missing_tokens=("a",),
        )
        fake_content_res = warn_res.__class__(
            content_name="ContentDoc",
            output_dir=warn_res.output_dir,
            index_json_path=warn_res.index_json_path,
            verified_pages=1,
            pages=(warn_record,),
        )
        with patch(
            "doctree.doctree_cli.command_dispatcher.parse_pdf_to_content_data",
            return_value=fake_content_res,
        ):
            assert cli.main(["parse", str(pdf_path), "--strict"]) == 1


_ADVERSARIAL_PDF_TOKENS = st.sampled_from(
    [
        b"(",
        b")",
        b"((unbalanced",
        b"(\\000\\400\\101\\050\\377\\777\\999\\n\\r\\t\\(\\)\\\\)",
        b"(CHAPTER: \xec\x9d\xb8\xeb\x8d\xb1\xec\x8a\xa4\xff)",
        b"(CODE_PY: def f\\(x\\):\\000 return x)",
        b"stream\r\ncorrupt_zlib_payload_\xff\x00\r\nendstream",
        b"/Type /Page",
        b"/Subtype /Image",
        b"%%FIGURE:Fig | <port> {brace}\n",
    ]
)


@settings(max_examples=80, deadline=None)
@given(
    raw_fragments=st.lists(st.one_of(st.binary(max_size=64), _ADVERSARIAL_PDF_TOKENS), max_size=12),
    compressed_text=st.binary(max_size=64),
)
@example(
    raw_fragments=[
        b"/Type /Page\nstream\n(\\000\\400\\101\\777\\n\\t)\nendstream\n",
        b"/Type /Page\n(((unbalanced\\777) %%FIGURE:\xff\xfe\n%%FIGURE:   \x00  \n",
    ],
    compressed_text=b"(CHAPTER: Octal \\102)\n%%FIGURE:ZlibFig\n",
)
def test_property_fuzz_pdf_stream_and_literal_parser_resilience(
    raw_fragments: list[bytes],
    compressed_text: bytes,
) -> None:
    """임의 바이너리·깨진 8진수 이스케이프·불균형 괄호·손상된 zlib 스트림 입력 시에도 예외 없이 NFC 정규화된 페이지를 반환한다."""
    valid_z = b"stream\n" + zlib.compress(b"(" + compressed_text + b")") + b"\nendstream"
    payload = b"\n".join([*raw_fragments, valid_z])
    chunks = _split_pdf_into_page_chunks(payload)
    assert len(chunks) >= 1

    for idx, chunk in enumerate(chunks, start=1):
        page = _parse_single_page_chunk(idx, chunk)
        assert page.page_number == idx
        assert unicodedata.is_normalized("NFC", page.raw_text)
        assert "\x00" not in page.raw_text
        assert "\x00" not in page.code_snippet
        assert page.figures_detected == len(page.figure_labels)
        assert all(lbl and "\x00" not in lbl and unicodedata.is_normalized("NFC", lbl) for lbl in page.figure_labels)


@settings(max_examples=80, deadline=None)
@given(
    content_name=st.text(max_size=60),
    page_number=st.integers(min_value=1, max_value=5000),
    fig_index=st.integers(min_value=1, max_value=500),
    label=st.text(max_size=80),
)
@example(
    content_name='Book | {A} <port> "quote" \\ \n\r\t\x00\x85\u200b\u202e\ufeff ```',
    page_number=1,
    fig_index=1,
    label='Fig | {B} <tag> "x" \\ \x7f\x85\u2028\u2029\u200b\u202e ```',
)
def test_property_fuzz_build_dot_diagram_delimiter_invariance(
    content_name: str,
    page_number: int,
    fig_index: int,
    label: str,
) -> None:
    """임의의 문자열이 입력되어도 build_dot_diagram의 DOT record 구분자(|, {, }, <, >)와 마크다운 펜스가 절대 깨지지 않는다."""
    dot_code = build_dot_diagram(content_name, page_number, fig_index, label)
    lines = dot_code.splitlines()
    source_line = next(line for line in lines if "source [label=" in line)

    assert dot_code.count("{") == 3
    assert dot_code.count("}") == 3
    assert source_line.count("{") == 1
    assert source_line.count("}") == 1
    assert source_line.count("|") == 1
    assert "<" not in source_line
    assert ">" not in source_line
    assert "```" not in dot_code
    assert all(ch.isprintable() for ch in source_line)
    assert verify_code_and_dot_blocks(
        f"```dot\n{dot_code}\n```\n```python\nx = 1\x00\n```\n",
        dot_bin="non_existent_dot_xyz",
    ) == (False, True)


def test_zlib_decompression_cap_java_syntax_and_verification_metamorphic(tmp_path: Path) -> None:
    """zlib 압축 폭탄 방어 상한, Java 구문 검증, DOT 타임아웃 폴백 및 5대 검증 메타모픽 관계를 검증한다."""
    # 1) Java 코드 블록 균형 괄호(True) 및 불균형 괄호(False) 검증
    assert verify_code_and_dot_blocks("```java\nclass A { void f() {} }\n```\n") == (True, True)
    assert verify_code_and_dot_blocks("```java\nclass A { void f() {\n```\n") == (False, True)

    # 2) Graphviz dot 실행 시 subprocess.TimeoutExpired 발생 시 정규식 폴백 작동 검증
    with patch(
        "doctree.pdf_threat_audit.pdf_page_ingestor.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="dot", timeout=5),
    ):
        assert verify_code_and_dot_blocks("```dot\ndigraph G { a -> b; }\n```\n", dot_bin="dot") == (True, True)
        assert verify_code_and_dot_blocks("```dot\ndigraph G { a -> b\n```\n", dot_bin="dot") == (True, False)

    # 3) zlib 스트림 해제 시 _MAX_DECOMPRESSED_STREAM_BYTES 상한 적용 확인
    assert _MAX_DECOMPRESSED_STREAM_BYTES == 16 * 1024 * 1024
    oversized_stream = zlib.compress(b"A" * (_MAX_DECOMPRESSED_STREAM_BYTES + 1024), level=1)
    assert len(_extract_stream_payload(oversized_stream)) == _MAX_DECOMPRESSED_STREAM_BYTES

    # 4) 메타모픽 관계 A: PDF 스트림 외부/내부 주석(`% ...`) 패딩 시 추출 토큰 및 페이지 파싱 결과 불변
    base_chunk = b"1 0 obj << /Type /Page >>\nstream\n(BTree depth 4 leaf 128)\nendstream\nendobj\n"
    comment_padded_chunk = (
        b"1 0 obj << /Type /Page >>\nstream\n% header comment\n(BTree depth 4 leaf 128)\n% footer\nendstream\nendobj\n"
    )
    parsed_base = _parse_single_page_chunk(1, base_chunk)
    parsed_padded = _parse_single_page_chunk(1, comment_padded_chunk)
    assert parsed_base == parsed_padded
    assert tokenize_content(parsed_base.raw_text) == tokenize_content(parsed_padded.raw_text)

    # 5) 메타모픽 관계 B: 5대 동일성 검증 토큰/숫자 보존 시 PASS -> 토큰/숫자 삭제 시 WARN 전이
    raw_page = RawPdfPage(
        page_number=1,
        chapter_title="01_인덱스",
        section_title="01_BTree",
        raw_text="BTree depth 4 leaf 128",
        figure_labels=(),
        code_snippet="",
        code_lang="",
    )
    assert raw_page.figures_detected == 0
    md_full = tmp_path / "full.md"
    md_full.write_text("# BTree\n\n| 원문 |\n| --- |\n| BTree depth 4 leaf 128 |\n", encoding="utf-8")
    rec_ok = verify_single_page("Book", raw_page, md_full, tmp_path)
    assert rec_ok.status == "PASS"
    assert rec_ok.token_recall == 1.0
    assert rec_ok.numbers_matched is True

    md_missing_num = tmp_path / "missing_num.md"
    md_missing_num.write_text("# BTree\n\n| 원문 |\n| --- |\n| BTree depth 4 leaf |\n", encoding="utf-8")
    rec_warn = verify_single_page("Book", raw_page, md_missing_num, tmp_path)
    assert rec_warn.status == "WARN"
    assert rec_warn.token_recall < rec_ok.token_recall
    assert rec_warn.numbers_matched is False
    assert "128" in rec_warn.missing_tokens



