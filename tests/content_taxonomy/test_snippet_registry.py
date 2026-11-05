"""[[snippet]] / @snippet 추출, SHA-256 인간 검증 레지스트리 및 마크다운·블로그 내보내기(snippet_registry.py) 테스트."""
from __future__ import annotations

import io
import json
import unicodedata
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from rich.console import Console

from doctree.content_taxonomy.snippet_registry import (
    SnippetStatus,
    _extract_snippets_from_file,
    _extract_symbol_name,
    _parse_marker_description,
    export_snippets_blog_posts,
    export_snippets_markdown,
    format_snippets_report,
    load_snippet_registry,
    save_snippet_registry,
    scan_project_snippets,
    sync_snippet_registry,
    verify_snippet,
)
from doctree.doctree_cli import command_dispatcher as cli
from doctree.runtime_env.boundary_errors import ClassificationError


def test_marker_parsing_symbol_extraction_and_registry_io(tmp_path: Path):
    """[[snippet]] / @snippet 마커 파싱, 심볼 추출 폴백, 레지스트리 로드·저장 예외 분기를 검증한다."""
    assert _parse_marker_description("int regular_code = 1;") is None
    assert _parse_marker_description("[[snippet]]") == ""
    assert _parse_marker_description('[[snippet("RAII 소멸자")]]') == "RAII 소멸자"
    assert _parse_marker_description("@snippet") == ""
    assert _parse_marker_description('@snippet("인덱스 스캔")') == "인덱스 스캔"
    assert _parse_marker_description("/* @snippet 블록 주석 설명 */") == "블록 주석 설명"

    # 예약어(main/if)만 있거나 기호만 있을 때 fallback 및 기본값("snippet") 반환 검증
    assert _extract_symbol_name("int main() { if (true) {} return 0; }", "___") == "snippet"
    assert _extract_symbol_name("SELECT 1;", "01_쿼리_기본") == "01_쿼리_기본"

    # 지원하지 않는 확장자 및 마커 없는 파일 분기
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("[[snippet]]\n", encoding="utf-8")
    assert _extract_snippets_from_file(txt_file, tmp_path) == []

    no_marker_py = tmp_path / "plain.py"
    no_marker_py.write_text("def f():\n    return 1\n", encoding="utf-8")
    assert _extract_snippets_from_file(no_marker_py, tmp_path) == []

    # load_snippet_registry 오류 분기 (미존재, 깨진 JSON, 잘못된 스키마, 비사전 항목 필터링)
    assert load_snippet_registry(tmp_path / "missing.json") == {}
    bad_reg = tmp_path / "bad.json"
    bad_reg.write_text("{corrupt", encoding="utf-8")
    assert load_snippet_registry(bad_reg) == {}
    bad_reg.write_text(json.dumps({"snippets": []}), encoding="utf-8")
    assert load_snippet_registry(bad_reg) == {}
    bad_reg.write_text(json.dumps({"snippets": {"bad": 123, "ok": {"status": "VERIFIED"}}}), encoding="utf-8")
    assert load_snippet_registry(bad_reg) == {"ok": {"status": "VERIFIED"}}

    # 존재하지 않는 경로 스캔 시 빈 튜플 반환
    assert scan_project_snippets(tmp_path / "non_existent_project") == ()


def test_snippet_lifecycle_pending_verified_outdated_and_exports(tmp_path: Path):
    """스니펫 스캔(PENDING_REVIEW) -> 인간 검증(VERIFIED) -> 코드 수정(OUTDATED) -> 마크다운·블로그 내보내기를 검증한다."""
    proj = tmp_path / "handcrafted-cpp"
    kata_dir = proj / "practice" / "01_Memory" / "01_RAII"
    kata_dir.mkdir(parents=True)
    (proj / "tips").mkdir(parents=True)

    # tips/ 내부 파일은 스캔 대상에서 제외되는지 확인하기 위한 더미 파일
    (proj / "tips" / "ignored.py").write_text("# @snippet ignored\ndef ignored(): pass\n", encoding="utf-8")
    (kata_dir / "README.md").write_text("# non-code file\n", encoding="utf-8")

    cpp_file = kata_dir / "main.cpp"
    cpp_file.write_text(
        '// [[snippet("RAII 스마트 포인터 래퍼")]]\n'
        "// content=prof_cpp | chapter=01_Memory | section=01_RAII | pages=1p\n"
        "struct ScopedBuffer {\n"
        "    int size;\n"
        "};\n\n"
        '[[snippet("인라인 덧셈")]] int fast_add(int a, int b) { return a + b; }\n'
        "// @snippet\n",
        encoding="utf-8",
    )

    # 1) 최초 스캔 (practice/ 폴더 직접 전달 및 프로젝트 루트 전달 모두 지원)
    recs_from_prac = scan_project_snippets(proj / "practice")
    assert len(recs_from_prac) == 3
    recs = sync_snippet_registry(proj)
    assert len(recs) == 3
    assert recs[0].symbol == "ScopedBuffer"
    assert recs[0].description == "RAII 스마트 포인터 래퍼"
    assert recs[0].status == SnippetStatus.PENDING_REVIEW
    assert recs[1].symbol == "fast_add"
    assert recs[1].description == "인라인 덧셈"
    # 파일 마지막 줄 단독 마커 폴백 검증
    assert recs[2].code == "// @snippet"

    # 빈 verified_sha256 레지스트리 엔트리와 대조 시 PENDING_REVIEW 유지 검증
    reg_path = proj / "tips" / "snippets_registry.json"
    save_snippet_registry(reg_path, recs)
    assert scan_project_snippets(proj)[0].status == SnippetStatus.PENDING_REVIEW

    # 2) 검증 전 블로그 내보내기 호출 시 전체 스니펫 폴백 내보내기 동작 확인
    pre_posts = export_snippets_blog_posts(proj, date_prefix="2026-10-30")
    assert len(pre_posts) == 3

    # 3) 인간 검증 승인 (verify_snippet: 심볼명 지정 및 오류 분기)
    with pytest.raises(ClassificationError, match="검증할 스니펫 심볼 또는 설명을 지정하세요"):
        verify_snippet(proj, "   ")
    with pytest.raises(ClassificationError, match="검증할 스니펫을 찾을 수 없습니다"):
        verify_snippet(proj, "non_existent_symbol")

    verified = verify_snippet(proj, "ScopedBuffer", reviewer="coralstay", verified_at="2026-10-30")
    assert len(verified) == 1
    assert verified[0].status == SnippetStatus.VERIFIED
    assert verified[0].reviewer == "coralstay"

    # 4) 검증 후 블로그 내보내기 시 VERIFIED 스니펫만 선택되어 내보내짐
    verified_posts = export_snippets_blog_posts(proj, tmp_path / "custom_posts", date_prefix="2026-10-30")
    assert len(verified_posts) == 1
    assert verified_posts[0].name == "2026-10-30-scopedbuffer.md"
    assert 'status: "VERIFIED"' in verified_posts[0].read_text(encoding="utf-8")

    # 5) 검증된 스니펫의 코드를 수정하면 자동으로 OUTDATED로 강등되며 재동기화해도 OUTDATED가 유지됨
    cpp_file.write_text(
        '// [[snippet("RAII 스마트 포인터 래퍼")]]\n'
        "struct ScopedBuffer {\n"
        "    int size;\n"
        "    int capacity;\n"
        "};\n\n"
        '[[snippet("인라인 덧셈")]] int fast_add(int a, int b) { return a + b; }\n',
        encoding="utf-8",
    )
    after_edit = sync_snippet_registry(proj)
    assert after_edit[0].status == SnippetStatus.OUTDATED
    after_resync = sync_snippet_registry(proj)
    assert after_resync[0].status == SnippetStatus.OUTDATED

    # 6) 마크다운 대시보드(tips/snippets.md) 및 리포트 포매팅 검증
    md_path = export_snippets_markdown(proj)
    assert md_path == proj / "tips" / "snippets.md"
    md_text = md_path.read_text(encoding="utf-8")
    assert "`OUTDATED` 1" in md_text and "`PENDING_REVIEW` 1" in md_text
    assert "OUTDATED 1" in format_snippets_report(after_resync)
    assert "총 1개" in format_snippets_report(after_resync, query="fast_add")


def test_standalone_sql_snippet_and_empty_blog_export(tmp_path: Path):
    """practice/ 하위 폴더가 없는 일반 디렉터리 스캔 및 빈 프로젝트 블로그 내보내기 예외를 검증한다."""
    standalone_dir = tmp_path / "standalone_snippets"
    standalone_dir.mkdir()
    (standalone_dir / "query.sql").write_text(
        "-- @snippet 인덱스 힌트 쿼리\nSELECT /*+ INDEX(t idx_t) */ id FROM users t;\n",
        encoding="utf-8",
    )
    sql_recs = scan_project_snippets(standalone_dir)
    assert len(sql_recs) == 1
    assert sql_recs[0].language == "sql"
    assert sql_recs[0].symbol == "users"

    empty_proj = tmp_path / "empty_proj"
    empty_proj.mkdir()
    with pytest.raises(ClassificationError, match="블로그 포스트로 내보낼 스니펫이 없습니다"):
        export_snippets_blog_posts(empty_proj)


def test_cli_snippet_subcommand(tmp_path: Path):
    """CLI `doctree snippet` 서브커맨드의 조회·검증·마크다운/블로그 내보내기 및 오류 분기를 검증한다."""
    proj = tmp_path / "handcrafted-python"
    kata_dir = proj / "practice" / "01_Ch" / "01_Sec"
    kata_dir.mkdir(parents=True)
    (kata_dir / "main.py").write_text(
        '# @snippet("피보나치 메모이제이션")\n'
        "def fib_memo(n: int) -> int:\n"
        "    return n if n <= 1 else fib_memo(n - 1) + fib_memo(n - 2)\n",
        encoding="utf-8",
    )

    buf = io.StringIO()
    test_console = Console(file=buf, width=120, force_terminal=False, color_system=None)

    with patch.object(cli, "console", test_console):
        # 1) 기본 목록 및 상태 출력
        assert cli.main(["snippet", "--path", str(proj), "--list", "--status"]) == 0
        assert "PENDING_REVIEW 1" in buf.getvalue()

        # 2) --verify 인간 검증 승인
        buf.truncate(0)
        buf.seek(0)
        assert cli.main(["snippet", "--path", str(proj), "--verify", "fib_memo", "--reviewer", "coralstay"]) == 0
        assert "✅ 스니펫 인간 검증 완료" in buf.getvalue()

        # 3) --export 및 --export-blog 실행
        buf.truncate(0)
        buf.seek(0)
        assert cli.main(["snippet", "--path", str(proj), "--export", "--export-blog", "--status"]) == 0
        out = buf.getvalue()
        assert "📝 스니펫 마크다운 대시보드 내보내기 완료:" in out
        assert "📰 기술 블로그 포스트 내보내기 완료 (1건):" in out
        assert "VERIFIED 1" in out
        assert (proj / "tips" / "snippets.md").is_file()
        assert list((proj / "tips" / "posts").glob("*-fib_memo.md"))

        # 4) 존재하지 않는 심볼 --verify 시 종료 코드 1
        assert cli.main(["snippet", "--path", str(proj), "--verify", "unknown_sym"]) == 1


@settings(max_examples=60, deadline=None)
@given(
    desc=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters='"\'\r\n\\'),
        max_size=40,
    ),
    func_suffix=st.from_regex(r"[a-zA-Z0-9_]{1,16}", fullmatch=True),
)
@example(desc="한글 NFD \u1100\u1161 설명", func_suffix="solve_1")
def test_property_fuzz_snippet_marker_nfc_and_symbol_invariants(desc: str, func_suffix: str) -> None:
    """임의 설명 문자열(NFD 포함)과 함수 접미사에 대해 마커 설명 NFC 정규화 및 심볼 추출 불변식을 검증한다."""
    cpp_attr_line = f'[[snippet("{desc}")]]'
    decorator_line = f'@snippet("{desc}")'
    parsed_attr = _parse_marker_description(cpp_attr_line)
    parsed_dec = _parse_marker_description(decorator_line)
    assert parsed_attr is not None
    assert parsed_attr == parsed_dec
    assert unicodedata.is_normalized("NFC", parsed_attr)

    sym = _extract_symbol_name(f"def kata_{func_suffix}():\n    return 1\n", desc)
    assert sym == f"kata_{func_suffix}"
