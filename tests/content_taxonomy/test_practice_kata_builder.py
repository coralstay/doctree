"""중간 결과물(content_data/) 기반 실습·테스트 프레임워크 빌더(practice_kata_builder.py) 및 `doctree practice` CLI 테스트."""
from __future__ import annotations

import io
import json
import re
import unicodedata
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from rich.console import Console

from doctree.content_taxonomy.practice_kata_builder import (
    _extract_markdown_highlights,
    _parse_page_number,
    _read_safe_markdown_file,
    _slugify,
    build_practice_katas,
    resolve_language_extension,
    verify_practice_katas,
)
from doctree.doctree_cli import command_dispatcher as cli
from doctree.pdf_threat_audit.pdf_page_ingestor import parse_pdf_to_content_data
from doctree.runtime_env.boundary_errors import ClassificationError


def _create_parsed_content(proj_dir: Path, content_name: str = "테스트도서") -> Path:
    pdf_path = proj_dir / "contents" / f"{content_name}.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_bytes = (
        b"%PDF-1.7\n"
        b"1 0 obj << /Type /Page >>\nstream\n"
        b"(CHAPTER: 01_Basics)\n(SECTION: 01_Intro)\n(Core concept 1)\n"
        b"endstream\nendobj\n"
        b"2 0 obj << /Type /Page >>\nstream\n"
        b"(CHAPTER: 01_Basics)\n(SECTION: 01_Intro)\n(Core concept 2)\n(CODE_PY: x = 1)\n%%FIGURE:Diag1\n"
        b"endstream\nendobj\n"
        b"3 0 obj << /Type /Page >>\nstream\n"
        b"(CHAPTER: 01_Basics)\n(SECTION: 01_Intro)\n(Core concept 3)\n(CODE_PY: y = 2)\n%%FIGURE:Diag2\n"
        b"endstream\nendobj\n"
        b"%%EOF\n"
    )
    pdf_path.write_bytes(pdf_bytes)
    res = parse_pdf_to_content_data(pdf_path, verify=True)
    return res.output_dir


def test_resolve_language_and_slugify_and_error_branches(tmp_path: Path):
    """언어 추론(py/cpp/java/sql), 슬러그 정규화 및 잘못된 입력 예외 분기를 검증한다."""
    assert _slugify("   ", 1) == "01_section_01"
    assert _slugify("---", 4) == "04_section_04"
    assert _slugify("01_---", 5) == "05_01"
    assert _slugify("０１_전각숫자", 6) == "06_전각숫자"
    assert _slugify("02_이미번호있음", 1) == "02_이미번호있음"
    assert _slugify("한글 제목!", 3) == "03_한글_제목"
    assert _parse_page_number("²", default=7) == 7
    assert _parse_page_number(True, default=8) == 8
    assert _read_safe_markdown_file(tmp_path, "bad\x00.md") is None
    assert _read_safe_markdown_file(tmp_path, "sub\\bad.md") is None
    assert _read_safe_markdown_file(tmp_path, "..") is None

    assert resolve_language_extension(Path("/ws/handcrafted-cpp/content_data/b")) == "cpp"
    assert resolve_language_extension(Path("/ws/handcrafted-java/content_data/b")) == "java"
    assert resolve_language_extension(Path("/ws/databases/content_data/b")) == "sql"
    assert resolve_language_extension(Path("/ws/handcrafted-python/content_data/b")) == "py"
    assert resolve_language_extension(Path("/ws/any"), explicit_lang="C++") == "cpp"

    with pytest.raises(ClassificationError, match="지원하지 않는 실습 언어"):
        resolve_language_extension(Path("/ws/any"), explicit_lang="rust")

    # 없는 폴더, 깨진 index.json, 빈 pages 배열 오류 검증
    with pytest.raises(ClassificationError, match="유효한 content_data"):
        build_practice_katas(tmp_path / "no_such_dir")

    bad_dir = tmp_path / "bad_book"
    bad_dir.mkdir()
    (bad_dir / "index.json").write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ClassificationError, match=r"index\.json을 읽을 수 없습니다"):
        build_practice_katas(bad_dir)

    (bad_dir / "index.json").write_text(json.dumps({"pages": []}), encoding="utf-8")
    with pytest.raises(ClassificationError, match="유효한 pages 목록이 없습니다"):
        build_practice_katas(bad_dir)

    (bad_dir / "index.json").write_text(
        json.dumps({"pages": [{"page": 1}], "toc": ["not-a-dict"]}),
        encoding="utf-8",
    )
    with pytest.raises(ClassificationError, match="toc 필드는 객체 목록이어야 합니다"):
        build_practice_katas(bad_dir)


def test_build_practice_katas_across_languages_and_idempotency(tmp_path: Path):
    """content_data/로부터 4종 언어(py/cpp/java/sql) 실습 패키지 생성, 파일 보존 및 덮어쓰기를 검증한다."""
    proj = tmp_path / "handcrafted-python"
    content_dir = _create_parsed_content(proj, "파이썬마스터")

    # 1) 기본(py) 생성: 2페이지가 동일한 장·절(01_Basics / 01_Intro)로 묶여 1개 실습 과제가 생성됨
    res_py = build_practice_katas(content_dir)
    assert res_py.lang_ext == "py"
    assert res_py.chapters_count == 1
    assert res_py.katas_count == 1
    kata_dir = res_py.kata_dirs[0]
    assert (kata_dir / "README.md").is_file()
    assert (kata_dir / "PITFALLS.md").is_file()
    assert (kata_dir / ".skeleton" / "main.py").is_file()
    assert (kata_dir / ".skeleton" / "test.py").is_file()
    assert (kata_dir / "main.py").is_file()
    assert (kata_dir / "test.py").is_file()
    assert ((kata_dir / "README.md").stat().st_mode & 0o777) == 0o444
    assert ((kata_dir / "PITFALLS.md").stat().st_mode & 0o777) == 0o444
    assert ((kata_dir / ".skeleton" / "main.py").stat().st_mode & 0o777) == 0o444
    assert ((kata_dir / "main.py").stat().st_mode & 0o777) == 0o644
    assert "1p ~ 3p" in (kata_dir / "README.md").read_text(encoding="utf-8")
    pitfalls_default = (kata_dir / "PITFALLS.md").read_text(encoding="utf-8")
    assert "## 1. 언제 제대로 동작하지 않는가 (Failure Modes & Edge Cases)" in pitfalls_default
    assert "## 2. 자주 하는 실수 (Common Mistakes & Anti-Patterns)" in pitfalls_default
    assert "## 3. 성능·메모리·환경 유의사항 (Performance & Environment Caveats)" in pitfalls_default
    assert "## 4. 디버깅 체크리스트 (Verification Checklist)" in pitfalls_default

    # 2) overwrite=False일 때 기존 파일 보존, overwrite=True일 때 덮어쓰기
    (kata_dir / "main.py").write_text("# user typed solution\n", encoding="utf-8")
    build_practice_katas(content_dir, overwrite=False)
    assert (kata_dir / "main.py").read_text(encoding="utf-8") == "# user typed solution\n"

    build_practice_katas(content_dir, overwrite=True)
    assert "@snippet" in (kata_dir / "main.py").read_text(encoding="utf-8")

    # 3) cpp, java, sql 언어 지정 및 custom practice_root, toc 없는 index.json 분기
    index_path = content_dir / "index.json"
    idx_data = json.loads(index_path.read_text(encoding="utf-8"))
    idx_data.pop("toc", None)
    # 마크다운 파일이 없는 페이지도 안전하게 기본 템플릿으로 생성되는지 확인하기 위해 2페이지 md 삭제
    (content_dir / "파이썬마스터-2page.md").unlink()
    index_path.write_text(json.dumps(idx_data), encoding="utf-8")

    for lang in ("cpp", "java", "sql"):
        custom_prac = tmp_path / f"prac_{lang}"
        res_lang = build_practice_katas(content_dir, practice_root=custom_prac, lang=lang, overwrite=True)
        assert res_lang.lang_ext == lang
        assert (res_lang.kata_dirs[0] / "PITFALLS.md").is_file()
        assert (res_lang.kata_dirs[0] / f"main.{lang}").is_file()
        assert (res_lang.kata_dirs[0] / f"test.{lang}").is_file()


def test_build_practice_katas_track_hierarchy_mirroring_and_pitfalls(tmp_path: Path):
    """<트랙_또는_분류>/<도서_슬러그>/<장>/<절> 4계층 1:1 미러링과 PITFALLS.md 추출 및 독립 경로 분기를 검증한다."""
    proj = tmp_path / "handcrafted-cpp"
    track_pdf = proj / "contents" / "c-and-cpp" / "08_prof_cpp" / "전문가를위한CPP.pdf"
    track_pdf.parent.mkdir(parents=True, exist_ok=True)
    track_pdf.write_bytes(
        b"%PDF-1.7\n"
        b"1 0 obj << /Type /Page >>\nstream\n"
        b"(CHAPTER: 01_Memory)\n(SECTION: 01_RAII)\n"
        b"(PITFALL: Double free when raw pointer is copied)\n"
        b"(CAVEAT: Requires C++20 move semantics)\n"
        b"endstream\nendobj\n"
        b"%%EOF\n"
    )
    parsed_track = parse_pdf_to_content_data(track_pdf, verify=True)
    assert parsed_track.output_dir == proj / "content_data" / "c-and-cpp" / "08_prof_cpp"
    res_track_kata = build_practice_katas(parsed_track.output_dir)
    assert res_track_kata.practice_content_dir == proj / "practice" / "c-and-cpp" / "08_prof_cpp"
    track_kata_dir = res_track_kata.kata_dirs[0]
    assert track_kata_dir == proj / "practice" / "c-and-cpp" / "08_prof_cpp" / "01_Memory" / "01_RAII"
    track_readme = (track_kata_dir / "README.md").read_text(encoding="utf-8")
    assert "../../../../../content_data/c-and-cpp/08_prof_cpp/08_prof_cpp-1page.md" in track_readme
    assert (
        track_kata_dir / "../../../../../content_data/c-and-cpp/08_prof_cpp/08_prof_cpp-1page.md"
    ).resolve().is_file()
    track_pitfalls = (track_kata_dir / "PITFALLS.md").read_text(encoding="utf-8")
    assert "- Double free when raw pointer is copied" in track_pitfalls
    assert "- Requires C++20 move semantics" in track_pitfalls

    # content_data 폴더 아래가 아닌 독립 폴더일 때 기본 practice 경로 분기 (문자열/무효 페이지 번호 및 경로 탈출 md_file 방어 포함)
    standalone_content = tmp_path / "standalone_content"
    standalone_content.mkdir()
    (standalone_content / "index.json").write_text(
        json.dumps(
            {
                "pages": [
                    {"page": "2", "md_file": "../escape.md"},
                    {"page": None, "md_file": "missing.md"},
                ],
                "toc": [
                    {"page": "2", "chapter": "   ", "section": unicodedata.normalize("NFD", "01_한글절"), "md_file": "../escape.md"},
                    {"page": None, "chapter": "", "section": "01_한글절", "md_file": "   "},
                ],
            }
        ),
        encoding="utf-8",
    )
    res_standalone = build_practice_katas(standalone_content)
    assert res_standalone.practice_content_dir == tmp_path / "practice" / "standalone_content"
    assert res_standalone.katas_count == 1


def test_cli_practice_subcommand(tmp_path: Path):
    """CLI `doctree practice` 명령(단일 도서 폴더, 프로젝트 루트, content_data 루트, --check, 오류 분기)을 검증한다."""
    proj = tmp_path / "handcrafted-cpp"
    content_dir = _create_parsed_content(proj, "모던CPP")

    # 실습 생성 전 verify_practice_katas 호출 시 실패(False)
    ok_before, missing_before = verify_practice_katas(proj / "no_practice")
    assert ok_before is False
    assert missing_before[0].startswith("practice_dir:")

    empty_prac = tmp_path / "empty_practice_dir"
    empty_prac.mkdir()
    ok_empty, _ = verify_practice_katas(empty_prac)
    assert ok_empty is False

    buf = io.StringIO()
    test_console = Console(file=buf, width=120, force_terminal=False, color_system=None)

    with patch.object(cli, "console", test_console):
        # 1) 프로젝트 루트 지정 시 하위 content_data/* 자동 탐색
        assert cli.main(["practice", str(proj)]) == 0
        assert "🎯 실습(Kata) 생성 완료: 모던CPP" in buf.getvalue()

        # 2) 생성 직후 --check 실행 시 4대 표준 파일 검증 통과(0)
        buf.truncate(0)
        buf.seek(0)
        assert cli.main(["practice", str(proj), "--check"]) == 0
        assert "✅ 실습(Kata) 4대 표준 파일 검증 통과:" in buf.getvalue()

        # 단일 리프 카타 디렉터리 직접 검증 통과 및 파일 삭제 시 누락 감지(--check 종료 코드 1)
        kata_leaf = next((proj / "practice").rglob("01_Intro"))
        assert verify_practice_katas(kata_leaf) == (True, ())
        (kata_leaf / "PITFALLS.md").unlink()
        (kata_leaf / "main.cpp").unlink()
        (kata_leaf / "test.cpp").unlink()
        assert cli.main(["practice", str(proj), "--check"]) == 1
        assert cli.main(["practice", str(kata_leaf)]) == 0
        assert (kata_leaf / "PITFALLS.md").is_file()

        # 3) 단일 도서 content_data/<도서명> 직접 지정 (--overwrite로 복구)
        assert cli.main(["practice", str(content_dir), "--overwrite"]) == 0
        assert verify_practice_katas(proj)[0] is True

        # 4) content_data 폴더 자체 지정
        assert cli.main(["practice", str(proj / "content_data")]) == 0

        # 5) index.json이 없는 빈 디렉터리 또는 존재하지 않는 경로 지정 시 종료 코드 1
        empty = tmp_path / "empty_dir"
        empty.mkdir()
        assert cli.main(["practice", str(empty)]) == 1
        assert cli.main(["practice", str(tmp_path / "non_existent_path")]) == 1


@settings(max_examples=80, deadline=None)
@given(
    raw_text=st.text(max_size=60),
    idx=st.integers(min_value=1, max_value=999),
    raw_page=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-100, max_value=10_000),
        st.text(max_size=20),
    ),
)
@example(raw_text="---", idx=1, raw_page="²")
@example(raw_text="01_---", idx=2, raw_page="①")
@example(raw_text="01__-a", idx=2, raw_page="①")
@example(raw_text="０１_전각_\u1100\u1161", idx=3, raw_page="  000  ")
@example(raw_text="01_A-B", idx=4, raw_page=True)
def test_property_fuzz_slugify_and_parse_page_number_invariants(
    raw_text: str,
    idx: int,
    raw_page: Any,
) -> None:
    """임의 유니코드·전각 숫자·위첨자·불리언 입력에 대해 _slugify 멱등성/안전 문자셋 및 _parse_page_number 양의 정수 불변식을 검증한다."""
    slug = _slugify(raw_text, idx)
    assert _slugify(slug, idx) == slug
    assert _slugify(unicodedata.normalize("NFD", raw_text), idx) == slug
    assert "__" not in slug
    assert re.fullmatch(r"[0-9]{2,}_[A-Za-z0-9_가-힣\-]*[A-Za-z0-9가-힣]", slug)

    parsed = _parse_page_number(raw_page, default=idx)
    assert type(parsed) is int
    assert parsed >= 1
    assert _parse_page_number(parsed, default=idx) == parsed


def test_extract_markdown_highlights_metamorphic_and_atomic_write_fault_preservation(tmp_path: Path) -> None:
    """[Pass 8/9] 마크다운 장식 요소(#, >, !, <!--, ```, 빈 줄) 추가 시 하이라이트 불변성 및 OSError 시 기존 카타 보존을 검증한다."""
    md_dir = tmp_path / "md_metamorphic"
    md_dir.mkdir()
    base_md = (
        "Core invariant 1\n"
        "Core invariant 2\n"
        "```dot\ndigraph G { a -> b; }\n```\n"
        "```python\ndef solve():\n    return 42\n```\n"
    )
    decorated_md = (
        "# Heading 1\n\n"
        "> Blockquote remark\n"
        "![diagram](assets/fig1.png)\n"
        "<!-- HTML comment -->\n"
        "Core invariant 1\n\n"
        "   \n"
        "## Subheading\n"
        "> Another quote\n"
        "Core invariant 2\n"
        "```dot\ndigraph G { a -> b; }\n```\n"
        "```python\ndef solve():\n    return 42\n```\n"
    )
    (md_dir / "base.md").write_text(base_md, encoding="utf-8")
    (md_dir / "decorated.md").write_text(decorated_md, encoding="utf-8")
    assert _extract_markdown_highlights(md_dir, ("base.md",)) == _extract_markdown_highlights(md_dir, ("decorated.md",))

    proj = tmp_path / "handcrafted-python"
    content_dir = _create_parsed_content(proj, "원자성테스트")
    res = build_practice_katas(content_dir)
    kata_dir = res.kata_dirs[0]
    main_py = kata_dir / "main.py"
    readme_md = kata_dir / "README.md"
    main_py.write_text("# preserved user kata\n", encoding="utf-8")
    original_readme = readme_md.read_bytes()

    with (
        patch.object(Path, "replace", side_effect=OSError("simulated kata disk full")),
        pytest.raises(OSError, match="simulated kata disk full"),
    ):
        build_practice_katas(content_dir, overwrite=True)

    assert main_py.read_text(encoding="utf-8") == "# preserved user kata\n"
    assert readme_md.read_bytes() == original_readme
    assert not list(kata_dir.glob(".*.tmp"))


