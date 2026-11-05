"""로컬 PDF 바이트 정적 무결성 및 위험 태그 검사기(doctree.pdf_threat_audit.pdf_byte_inspector) 단위 테스트."""
import dataclasses
import hashlib
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from doctree.pdf_threat_audit.pdf_byte_inspector import (
    DANGEROUS_PDF_TAGS,
    LocalPdfAudit,
    compute_sha256,
    discover_local_pdfs,
    inspect_pdf_bytes,
)
from doctree.pdf_threat_audit.threat_intel_client import VerdictStatus, VtVerdict


def _make_pdf(path: Path, body: bytes = b"1 0 obj << /Type /Catalog >> endobj\n", valid: bool = True) -> str:
    content = (b"%PDF-1.7\n" + body + b"\n%%EOF\n") if valid else b"NOT A PDF"
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_compute_sha256_and_clean_pdf_inspection(tmp_path):
    pdf = tmp_path / "clean.pdf"
    expected_sha = _make_pdf(pdf)

    # 작은 chunk_size로 여러 번 나눠 읽어도 동일한 SHA-256 해시를 반환해야 한다
    assert compute_sha256(pdf, chunk_size=7) == expected_sha
    header_ok, eof_ok, tags = inspect_pdf_bytes(pdf)
    assert (header_ok, eof_ok, tags) == (True, True, ())


def test_inspect_pdf_size_boundaries_and_partial_corruption(tmp_path):
    # 존재하지 않는 경로 및 디렉터리 경로는 예외 없이 즉시 (False, False, ()) 반환
    assert inspect_pdf_bytes(tmp_path / "missing.pdf") == (False, False, ())
    assert inspect_pdf_bytes(tmp_path) == (False, False, ())

    # 경계값: 0바이트 및 7바이트(< 8) 파일은 즉시 (False, False, ()) 반환
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(b"")
    assert inspect_pdf_bytes(empty_pdf) == (False, False, ())

    seven_bytes = tmp_path / "seven.pdf"
    seven_bytes.write_bytes(b"%PDF-1.")
    assert inspect_pdf_bytes(seven_bytes) == (False, False, ())

    # 경계값: 정확히 8바이트(== 8) 파일은 헤더 검사를 수행한다 (< 8 vs <= 8 뮤테이션 검증)
    eight_bytes = tmp_path / "eight.pdf"
    eight_bytes.write_bytes(b"%PDF-1.7")
    assert inspect_pdf_bytes(eight_bytes) == (True, False, ())

    # 헤더만 있고 %%EOF가 없는 손상 파일
    no_eof = tmp_path / "no_eof.pdf"
    no_eof.write_bytes(b"%PDF-1.7\n1 0 obj <<>> endobj\n")
    assert inspect_pdf_bytes(no_eof) == (True, False, ())

    # %%EOF만 있고 %PDF- 헤더가 없는 손상 파일
    no_header = tmp_path / "no_header.pdf"
    no_header.write_bytes(b"CORRUPT_HEADER\n1 0 obj <<>> endobj\n%%EOF\n")
    assert inspect_pdf_bytes(no_header) == (False, True, ())


def test_inspect_pdf_detects_all_dangerous_tags_and_rejects_lookalikes(tmp_path):
    mal_pdf = tmp_path / "exploit.pdf"
    _make_pdf(
        mal_pdf,
        body=(
            b"1 0 obj << /OpenAction << /S /JavaScript /S /JS (app.launchURL) >> "
            b"/Launch (cmd.exe) /EmbeddedFiles <<>> /RichMedia <<>> /XFA [] >> endobj"
        ),
    )

    header_ok, eof_ok, tags = inspect_pdf_bytes(mal_pdf)
    assert header_ok is True
    assert eof_ok is True
    assert tags == ("/JavaScript", "/JS", "/Launch", "/EmbeddedFiles", "/RichMedia", "/XFA")

    # 접두사만 같고 뒤에 영숫자/밑줄이 이어지는 정상 식별자나 압축 스트림 내 '/JS'는 오탐하지 않는다
    benign_pdf = tmp_path / "benign_lookalikes.pdf"
    _make_pdf(
        benign_pdf,
        body=(
            b"stream\n\x89\x50/JS\x00\xff\nendstream\n"
            b"<< /JavaScriptEngine 1 /S /JScript /LaunchPad 2 /EmbeddedFilesList 3 /RichMediaPlayer 4 /XFA_Form 5 >>"
        ),
    )
    assert inspect_pdf_bytes(benign_pdf) == (True, True, ())


def test_inspect_pdf_detects_tag_split_across_1mb_chunk_boundary(tmp_path):
    """1MB(1048576B) 청크 경계에 걸쳐 쪼개진 위험 태그('/Java' + 'Script')도 슬라이딩 윈도우로 탐지한다."""
    chunk_boundary_pdf = tmp_path / "split_boundary.pdf"
    prefix = b"%PDF-1.7\n/Launch "
    # 첫 번째 1MB 청크의 끝부분에 b"/Java"가 위치하고 두 번째 청크 시작에 b"Script\n"이 오도록 패딩
    pad_len = (1024 * 1024) - len(prefix) - len(b"/Java")
    payload = prefix + (b"A" * pad_len) + b"/JavaScript\n/Launch\n%%EOF\n"
    chunk_boundary_pdf.write_bytes(payload)

    header_ok, eof_ok, tags = inspect_pdf_bytes(chunk_boundary_pdf)
    assert header_ok is True
    assert eof_ok is True
    assert tags == ("/JavaScript", "/Launch")


@pytest.mark.parametrize(
    "header,eof,tags,verdict,expected_safe",
    [
        (True, True, (), None, True),
        (False, True, (), None, False),
        (True, False, (), None, False),
        (True, True, ("/Launch",), None, False),
        (True, True, (), VtVerdict(sha256="a", status=VerdictStatus.CLEAN), True),
        (True, True, (), VtVerdict(sha256="a", status=VerdictStatus.NOT_FOUND), True),
        (True, True, (), VtVerdict(sha256="a", status=VerdictStatus.MALICIOUS, malicious=1), False),
        (True, True, (), VtVerdict(sha256="a", status=VerdictStatus.SUSPICIOUS, suspicious=1), False),
    ],
)
def test_local_pdf_audit_is_safe_truth_table_and_immutability(header, eof, tags, verdict, expected_safe):
    audit = LocalPdfAudit(
        path="/tmp/test.pdf",
        sha256="a" * 64,
        valid_pdf_header=header,
        valid_pdf_eof=eof,
        dangerous_tags=tags,
        vt_verdict=verdict,
    )
    assert audit.is_safe is expected_safe
    with pytest.raises(dataclasses.FrozenInstanceError):
        audit.path = "/tmp/mutated.pdf"  # type: ignore[misc]


def test_discover_local_pdfs_deduplicates_and_skips_missing_or_non_pdf(tmp_path):
    sub = tmp_path / "contents"
    sub.mkdir()
    pdf1 = sub / "a_sample.pdf"
    pdf2 = sub / "b_upper.PDF"
    txt = sub / "ignore.txt"
    nested_dir = sub / "nested.pdf"  # 디렉터리 이름이 .pdf여도 파일이 아니면 제외
    nested_dir.mkdir()

    _make_pdf(pdf1)
    _make_pdf(pdf2)
    txt.write_text("not a pdf", encoding="utf-8")

    missing_root = tmp_path / "non_existent_root"
    # 동일 파일 직접 경로 중복 전달 + 상위 폴더 전달 시에도 중복 없이 정렬된 목록 반환
    found = discover_local_pdfs([missing_root, pdf1, pdf1, txt, sub])
    assert found == [pdf1, pdf2]


@settings(max_examples=80, deadline=None)
@given(
    delim=st.sampled_from([b" ", b"\n", b"\r", b"\t", b"/", b"(", b"<", b"[", b">"]),
    ident_suffix=st.from_regex(r"\A[A-Za-z0-9_]{1,12}\Z", fullmatch=True),
    valid_header=st.booleans(),
    valid_eof=st.booleans(),
    tags=st.lists(
        st.sampled_from([label for _, label in DANGEROUS_PDF_TAGS]),
        max_size=3,
        unique=True,
    ).map(tuple),
    vt_verdict=st.builds(
        VtVerdict,
        sha256=st.just("a" * 64),
        status=st.sampled_from(list(VerdictStatus)),
        malicious=st.integers(min_value=0, max_value=3),
        suspicious=st.integers(min_value=0, max_value=3),
    ),
)
@example(
    delim=b"(",
    ident_suffix="Engine_1",
    valid_header=True,
    valid_eof=True,
    tags=(),
    vt_verdict=VtVerdict(sha256="a" * 64, status=VerdictStatus.CLEAN, malicious=1, suspicious=0),
)
def test_property_fuzz_local_pdf_audit_and_tag_regex_invariants(
    delim: bytes,
    ident_suffix: str,
    valid_header: bool,
    valid_eof: bool,
    tags: tuple[str, ...],
    vt_verdict: VtVerdict,
) -> None:
    """DANGEROUS_PDF_TAGS 정규식의 구분자 탐지·식별자 접미어 오탐 배제 및 LocalPdfAudit.is_safe 불변식을 검증한다."""
    suffix_bytes = ident_suffix.encode("ascii")
    for pattern, label in DANGEROUS_PDF_TAGS:
        raw_tag = b"/S /JS" if label == "/JS" else label.encode("ascii")
        assert pattern.search(b"<< " + raw_tag + delim + b">>") is not None
        assert pattern.search(b"<< " + raw_tag + suffix_bytes + b" >>") is None

    audit = LocalPdfAudit(
        path="/tmp/fuzz.pdf",
        sha256="a" * 64,
        valid_pdf_header=valid_header,
        valid_pdf_eof=valid_eof,
        dangerous_tags=tags,
        vt_verdict=vt_verdict,
    )
    expected_vt_safe = (
        vt_verdict.status not in (VerdictStatus.MALICIOUS, VerdictStatus.SUSPICIOUS)
        and vt_verdict.malicious == 0
        and vt_verdict.suspicious == 0
    )
    assert audit.is_safe is (valid_header and valid_eof and not tags and expected_vt_safe)


def test_pdf_header_and_tail_window_exact_boundaries(tmp_path: Path) -> None:
    """1024바이트 헤더 창·65536바이트 꼬리 창 경계값(Off-by-One) 및 주석 패딩 메타모픽 불변성을 검증한다."""
    # 1) %PDF- 헤더가 정확히 1024바이트 창 내부 끝(offset 1019..1024)에 걸친 경우 -> True
    header_in = tmp_path / "header_in.pdf"
    header_in.write_bytes(b"X" * 1019 + b"%PDF-1.7\n%%EOF\n")
    assert inspect_pdf_bytes(header_in)[:2] == (True, True)

    # 2) %PDF- 헤더가 1020바이트 오프셋에서 시작해 1024바이트 창에서 1바이트 잘린 경우 -> False
    header_out = tmp_path / "header_out.pdf"
    header_out.write_bytes(b"X" * 1020 + b"%PDF-1.7\n%%EOF\n")
    assert inspect_pdf_bytes(header_out)[:2] == (False, True)

    # 3) %%EOF가 마지막 65536바이트 창 내부 시작점에 위치한 경우 -> True
    tail_in = tmp_path / "tail_in.pdf"
    tail_in.write_bytes(b"%PDF-1.7\n" + b"%%EOF" + b"A" * (65536 - 5))
    assert inspect_pdf_bytes(tail_in)[:2] == (True, True)

    # 4) %%EOF가 마지막 65536바이트 창 밖으로 1바이트 밀려난 경우 -> False
    tail_out = tmp_path / "tail_out.pdf"
    tail_out.write_bytes(b"%PDF-1.7\n" + b"%%EOF" + b"A" * (65536 - 4))
    assert inspect_pdf_bytes(tail_out)[:2] == (True, False)

    # 5) 메타모픽 관계: 무해한 PDF 주석(% comment) 패딩 시 판정 불변, 위험 태그(/S /JS) 삽입 시에만 unsafe 전이
    base_pdf = tmp_path / "base.pdf"
    padded_pdf = tmp_path / "padded.pdf"
    tagged_pdf = tmp_path / "tagged.pdf"
    _make_pdf(base_pdf, body=b"1 0 obj << /Type /Page >> endobj")
    _make_pdf(padded_pdf, body=b"% benign comment\n1 0 obj << /Type /Page >> endobj\n% footer comment")
    _make_pdf(tagged_pdf, body=b"% benign comment\n1 0 obj << /S /JS (alert) >> endobj")
    assert inspect_pdf_bytes(base_pdf) == inspect_pdf_bytes(padded_pdf) == (True, True, ())
    assert inspect_pdf_bytes(tagged_pdf) == (True, True, ("/JS",))


