"""터미널 출력 포맷터 및 대시보드(src.doctree_cli.terminal_tree_renderer) 단위 테스트."""
import io
import unicodedata
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from rich.console import Console

from doctree.content_taxonomy.category_classifier import (
    Category,
    ClassificationCatalog,
    ClassificationResult,
    ClassifiedContent,
    Subcategory,
)
from doctree.doctree_cli import terminal_tree_renderer as ui
from doctree.doctree_cli.terminal_tree_renderer import _single_line
from doctree.pdf_threat_audit.pdf_byte_inspector import DANGEROUS_PDF_TAGS, LocalPdfAudit
from doctree.pdf_threat_audit.threat_intel_client import VerdictStatus, VtVerdict


def test_format_classification_summary_renders_tree_and_unclassified_paths():
    catalog = ClassificationCatalog(
        categories=(
            Category(
                id="cs",
                name="컴퓨터\n과학\x7f",
                subcategories=(
                    Subcategory(id="cs.algo", name="알고리즘"),
                    Subcategory(id="cs.os", name="운영체제"),
                ),
            ),
        ),
        assignments=(),
    )
    result = ClassificationResult(
        catalog=catalog,
        contents=(
            ClassifiedContent(record={"id": "1", "name": "알고리즘.pdf", "folder_path": "CS"}, category_id="cs.algo"),
            ClassifiedContent(record={"id": "2", "name": "미분류_폴더.pdf", "folder_path": "기타/"}, category_id=None),
            ClassifiedContent(record={"id": "3", "name": "미분류_루트.pdf", "folder_path": ""}, category_id=None),
        ),
    )

    summary = ui.format_classification_summary(result)
    assert "파일 3개 · 분류 1권 · 미분류 2권" in summary
    assert "1. 컴퓨터 과학" in summary
    assert "알고리즘 (1)" in summary
    assert "운영체제 (0)" in summary
    assert "알고리즘.pdf" in summary
    assert "- 미분류_루트.pdf (id=3, path=미분류_루트.pdf)" in summary
    assert "- 미분류_폴더.pdf (id=2, path=기타/미분류_폴더.pdf)" in summary

    # 분류표에 존재하지 않는 category_id가 들어오면 ValueError 발생
    bad_result = ClassificationResult(
        catalog=catalog,
        contents=(ClassifiedContent(record={"id": "9", "name": "이상값.pdf"}, category_id="unknown.sub"),),
    )
    with pytest.raises(ValueError, match="분류표에 없는 세부분야 ID"):
        ui.format_classification_summary(bad_result)


def test_print_account_dashboard_outputs_none_limited_and_unlimited_storage():
    buf = io.StringIO()
    with patch("doctree.doctree_cli.terminal_tree_renderer.console", Console(file=buf, width=120, color_system=None)):
        ui.print_account_dashboard(None)
        ui.print_account_dashboard({
            "name": "coralstay",
            "email": "coralstay@example.com",
            "limit_gb": 100.0,
            "usage_gb": 25.0,
            "drive_usage_gb": 20.0,
            "usage_percent": 25.0,
        })
        ui.print_account_dashboard({
            "name": "OrgUser",
            "email": "org@example.com",
            "limit_gb": None,
            "usage_gb": 50.0,
            "drive_usage_gb": 45.0,
            "usage_percent": 0.0,
        })
    out = buf.getvalue()
    assert "건너뜁니다" in out
    assert "coralstay" in out
    assert "25.0 GB / 100.0 GB" in out
    assert "50.0 GB (무제한/기관)" in out


@settings(max_examples=80, deadline=None)
@given(
    raw_text=st.text(max_size=60),
    valid_header=st.booleans(),
    valid_eof=st.booleans(),
    tags=st.lists(
        st.sampled_from([label for _, label in DANGEROUS_PDF_TAGS]),
        max_size=3,
        unique=True,
    ).map(tuple),
    vt_verdict=st.one_of(
        st.none(),
        st.builds(
            VtVerdict,
            sha256=st.just("a" * 64),
            status=st.sampled_from(list(VerdictStatus)),
            malicious=st.integers(min_value=0, max_value=5),
            suspicious=st.integers(min_value=0, max_value=5),
        ),
    ),
)
@example(
    raw_text="한글\x85\x9b[31m\u2028\u2029\u200b\u1100\u1161",
    valid_header=True,
    valid_eof=True,
    tags=(),
    vt_verdict=VtVerdict(sha256="a" * 64, status=VerdictStatus.SUSPICIOUS, malicious=0, suspicious=3),
)
@example(
    raw_text="정상",
    valid_header=True,
    valid_eof=True,
    tags=(),
    vt_verdict=VtVerdict(sha256="a" * 64, status=VerdictStatus.CLEAN, malicious=2, suspicious=0),
)
def test_property_fuzz_single_line_and_audit_reasons_biconditional(
    raw_text: str,
    valid_header: bool,
    valid_eof: bool,
    tags: tuple[str, ...],
    vt_verdict: VtVerdict | None,
) -> None:
    """_single_line의 C0/C1/비가시 제어문자 제거·멱등성과 LocalPdfAudit.is_safe ⟺ (reasons == []) 동치 불변식을 검증한다."""
    line = _single_line(raw_text)
    assert _single_line(line) == line
    assert _single_line(unicodedata.normalize("NFD", raw_text)) == line
    assert "\n" not in line and "\r" not in line
    assert all(ch.isprintable() for ch in line)

    audit = LocalPdfAudit(
        path="/tmp/fuzz.pdf",
        sha256="a" * 64,
        valid_pdf_header=valid_header,
        valid_pdf_eof=valid_eof,
        dangerous_tags=tags,
        vt_verdict=vt_verdict,
    )
    reasons = ui.format_local_audit_reasons(audit)
    assert audit.is_safe is (len(reasons) == 0)


@pytest.mark.parametrize(
    "usage_percent, expected_filled",
    [
        (0.0, 0),
        (4.9, 0),
        (5.0, 1),
        (50.0, 10),
        (100.0, 20),
        (125.0, 20),
    ],
)
def test_storage_quota_gauge_boundary_values(usage_percent: float, expected_filled: int) -> None:
    """print_account_dashboard의 20칸 게이지 바 경계값(0%, 4.9%, 5.0%, 100%, 125% 상한 클램핑)을 검증한다."""
    buf = io.StringIO()
    test_console = Console(file=buf, width=120, force_terminal=False, color_system=None)
    with patch.object(ui, "console", test_console):
        ui.print_account_dashboard(
            {
                "name": "BoundaryUser",
                "email": "boundary@example.com",
                "limit_gb": 100.0,
                "usage_gb": usage_percent,
                "drive_usage_gb": usage_percent,
                "usage_percent": usage_percent,
            }
        )
    expected_bar = "[" + ("█" * expected_filled) + ("░" * (20 - expected_filled)) + "]"
    assert expected_bar in buf.getvalue()

