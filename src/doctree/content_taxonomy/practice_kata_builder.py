# ---
# title: practice_kata_builder
# description: content_data 중간 결과물에서 실습·테스트 프레임워크(practice/)를 생성한다 (폐기된 PDF 파이프라인 잔재)
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""중간 결과물(content_data/<도서명>/*page.md)을 장·절·주제별로 묶어 실습·테스트 프레임워크(practice/)를 생성하는 빌더 (Step 4: doctree practice)."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from doctree.runtime_env.boundary_errors import ClassificationError
from doctree.runtime_env.workspace_settings import (
    DEFAULT_TOC_CHAPTER,
    PathLike,
    atomic_write_text,
    build_default_toc_entry,
    normalize_nfc,
    render_dot_to_svg,
    sanitize_dot_label,
)

__all__ = [
    "SUPPORTED_LANG_MAP",
    "PracticeBuildResult",
    "backfill_practice_pitfalls",
    "build_practice_katas",
    "resolve_language_extension",
    "verify_practice_katas",
]

SUPPORTED_LANG_MAP: Final[Mapping[str, str]] = MappingProxyType(
    {
        "py": "py",
        "python": "py",
        "cpp": "cpp",
        "c++": "cpp",
        "c": "cpp",
        "java": "java",
        "sql": "sql",
    }
)

_DOT_BLOCK_RE = re.compile(r"```dot\n(.*?)```", re.DOTALL)
_CODE_BLOCK_RE = re.compile(r"```(python|py|cpp|c\+\+|c|java|sql)\n(.*?)```", re.DOTALL)
_UNSAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9가-힣\-]+")
_NUMBERED_SLUG_RE = re.compile(r"^[0-9]{2,}_[A-Za-z0-9가-힣]")
_DOT_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]+")
_PITFALL_LINE_RE = re.compile(r"^(?:[-*]\s+)?(?:PITFALL|CAVEAT):\s*(.+)$", re.IGNORECASE)


def _slugify(text: str, index: int) -> str:
    cleaned = _UNSAFE_SLUG_RE.sub("_", normalize_nfc(text).strip()).strip("_-")
    if not cleaned:
        cleaned = f"section_{index:02d}"
    if _NUMBERED_SLUG_RE.match(cleaned):
        return cleaned
    return f"{index:02d}_{cleaned}"


@dataclass(frozen=True, slots=True)
class PracticeBuildResult:
    """콘텐츠 1건의 practice/ 실습 프레임워크 생성 결과."""

    content_name: str
    practice_content_dir: Path
    lang_ext: str
    chapters_count: int
    kata_dirs: tuple[Path, ...]

    @property
    def katas_count(self) -> int:
        """생성된 실습(Kata) 과제 디렉터리 개수 (kata_dirs 단일 진실 원천에서 파생)."""
        return len(self.kata_dirs)


_PATH_KEYWORD_LANG_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("cpp", "cpp"),
    ("java", "java"),
    ("databases", "sql"),
    ("sql", "sql"),
)


def resolve_language_extension(
    content_data_dir: Path,
    explicit_lang: str | None = None,
) -> str:
    """명시적 언어 옵션 또는 프로젝트 경로명으로부터 실습 코드 확장자(py/cpp/java/sql)를 결정한다."""
    if explicit_lang is not None:
        key = explicit_lang.strip().lower()
        if key not in SUPPORTED_LANG_MAP:
            raise ClassificationError(
                f"지원하지 않는 실습 언어입니다: {explicit_lang!r} (지원: py, cpp, java, sql)"
            )
        return SUPPORTED_LANG_MAP[key]

    parts_lower = "/".join(part.lower() for part in content_data_dir.parts)
    for keyword, ext in _PATH_KEYWORD_LANG_RULES:
        if keyword in parts_lower:
            return ext
    return "py"


_DEFAULT_REF_CODE_BY_LANG: Final[Mapping[str, str]] = MappingProxyType(
    {
        "cpp": (
            "#include <iostream>\n\n"
            "int solve_kata() {\n"
            "    return 42;\n"
            "}\n\n"
            "int main() {\n"
            "    std::cout << solve_kata() << std::endl;\n"
            "    return 0;\n"
            "}"
        ),
        "java": (
            "public class Main {\n"
            "    public static int solveKata() {\n"
            "        return 42;\n"
            "    }\n"
            "    public static void main(String[] args) {\n"
            "        System.out.println(solveKata());\n"
            "    }\n"
            "}"
        ),
        "sql": "SELECT 42 AS kata_result;",
        "py": (
            "def solve_kata() -> int:\n"
            "    return 42\n\n\n"
            'if __name__ == "__main__":\n'
            "    print(solve_kata())"
        ),
    }
)

_COMMENT_PREFIX_BY_LANG: Final[Mapping[str, str]] = MappingProxyType(
    {
        "cpp": "//",
        "java": "//",
        "sql": "--",
        "py": "#",
    }
)


def _default_main_and_test_templates(
    content_name: str,
    chapter_dir: str,
    section_slug: str,
    pages_label: str,
    target_code: str,
    lang_ext: str,
) -> tuple[str, str, str]:
    """언어별(py/cpp/java/sql) 목표 코드, 스켈레톤 main.<ext>, 단위 테스트 test.<ext>를 반환한다."""
    header = f"content={content_name} | chapter={chapter_dir} | section={section_slug} | pages={pages_label}"
    comment = _COMMENT_PREFIX_BY_LANG[lang_ext]
    ref_code = target_code or _DEFAULT_REF_CODE_BY_LANG[lang_ext]
    main_code = f"{comment} @snippet {section_slug}\n{comment} {header}\n{ref_code}\n"

    if lang_ext == "cpp":
        test_code = (
            f"// Unit test: {header}\n"
            "#include <cassert>\n"
            "#include <iostream>\n\n"
            "int main() {\n"
            f'    std::cout << "PASS: {section_slug}" << std::endl;\n'
            "    return 0;\n"
            "}\n"
        )
    elif lang_ext == "java":
        test_code = (
            f"// Unit test: {header}\n"
            "public class Test {\n"
            "    public static void main(String[] args) {\n"
            f'        System.out.println("PASS: {section_slug}");\n'
            "    }\n"
            "}\n"
        )
    elif lang_ext == "sql":
        test_code = f"-- Unit test: {header}\nEXPLAIN {ref_code}\n"
    else:
        test_code = (
            f"# Unit test: {header}\n"
            "import pathlib\n\n"
            'if __name__ == "__main__":\n'
            '    main_file = pathlib.Path(__file__).with_name("main.py")\n'
            "    assert main_file.is_file()\n"
            f'    print("PASS: {section_slug}")\n'
        )
    return ref_code, main_code, test_code


def _parse_page_number(raw_page: Any, default: int = 1) -> int:
    """페이지 번호 필드를 1 이상의 정수로 안전하게 파싱한다."""
    if type(raw_page) is int and raw_page >= 1:
        return raw_page
    if isinstance(raw_page, str) and (stripped := raw_page.strip()).isascii() and stripped.isdigit():
        return max(1, int(stripped))
    return default


def _load_content_index(content_dir: Path) -> dict[str, Any]:  # ---- 1
    index_path = content_dir / "index.json"
    if not content_dir.is_dir() or not index_path.is_file():
        raise ClassificationError(
            f"유효한 content_data 도서 디렉터리(index.json 포함)가 아닙니다: {content_dir}"
        )
    try:
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClassificationError(f"{index_path}: index.json을 읽을 수 없습니다: {exc}") from exc

    if (
        not isinstance(index_data, dict)
        or not isinstance(index_data.get("pages"), list)
        or not index_data["pages"]
        or not all(isinstance(page, dict) for page in index_data["pages"])
    ):
        raise ClassificationError(f"{index_path}: 유효한 pages 목록이 없습니다.")
    raw_toc = index_data.get("toc")
    if raw_toc is not None and (
        not isinstance(raw_toc, list) or not all(isinstance(entry, dict) for entry in raw_toc)
    ):
        raise ClassificationError(f"{index_path}: toc 필드는 객체 목록이어야 합니다.")
    return index_data


def _resolve_practice_dir(c_dir: Path, content_name: str, practice_root: PathLike | None) -> Path:
    content_data_root = next((p for p in c_dir.parents if p.name == "content_data"), None)
    rel_subpath = (
        Path(*(normalize_nfc(part) for part in c_dir.relative_to(content_data_root).parts))
        if content_data_root is not None
        else Path(content_name)
    )
    if practice_root is not None:
        return Path(practice_root).resolve() / rel_subpath
    base_parent = content_data_root.parent if content_data_root is not None else c_dir.parent
    return base_parent / "practice" / rel_subpath


def _resolve_toc_entries(index_data: dict[str, Any], content_name: str) -> list[dict[str, Any]]:
    raw_toc = index_data.get("toc")
    if isinstance(raw_toc, list) and raw_toc:
        return raw_toc
    return [
        build_default_toc_entry(
            content_name,
            _parse_page_number(p.get("page"), default=idx + 1),
            default_section_suffix="페이지_실습",
            md_file=p.get("md_file"),
        )
        for idx, p in enumerate(index_data["pages"])
    ]


def _group_toc_by_chapter_and_section(
    index_data: dict[str, Any],
    content_name: str,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    toc_entries = _resolve_toc_entries(index_data, content_name)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    chapter_order: dict[str, int] = {}
    section_order: dict[tuple[str, str], int] = {}
    chapter_sec_counts: dict[str, int] = {}

    for entry in toc_entries:
        page_num = _parse_page_number(entry.get("page"), default=1)
        raw_ch = normalize_nfc(str(entry.get("chapter") or "")).strip() or DEFAULT_TOC_CHAPTER
        raw_sec = (
            normalize_nfc(str(entry.get("section") or "")).strip()
            or f"{page_num:02d}_페이지_실습"
        )
        if raw_ch not in chapter_order:
            chapter_order[raw_ch] = len(chapter_order) + 1
        ch_slug = _slugify(raw_ch, chapter_order[raw_ch])

        sec_key = (ch_slug, raw_sec)
        if sec_key not in section_order:
            chapter_sec_counts[ch_slug] = chapter_sec_counts.get(ch_slug, 0) + 1
            section_order[sec_key] = chapter_sec_counts[ch_slug]
        sec_slug = _slugify(raw_sec, section_order[sec_key])

        grouped.setdefault((ch_slug, sec_slug), []).append(entry)
    return grouped


def _read_safe_markdown_file(c_dir: Path, md_name: str) -> str | None:  # ---- 2
    """c_dir 내부의 안전한 마크다운 파일이면 본문 문자열을 반환하고, 경로 탈출이나 미존재 시 None을 반환한다."""
    norm_md = normalize_nfc(md_name)
    safe_name = Path(norm_md).name if norm_md.isprintable() and "\\" not in norm_md else ""
    if not safe_name or safe_name != norm_md or safe_name in (".", ".."):
        return None
    md_path = c_dir / safe_name
    return md_path.read_text(encoding="utf-8") if md_path.is_file() else None


def _extract_markdown_highlights(
    c_dir: Path,
    md_files: tuple[str, ...],
) -> tuple[list[str], str, str]:
    body_snippets: list[str] = []
    extracted_dot = ""
    extracted_code = ""

    for md_name in md_files:
        md_text = _read_safe_markdown_file(c_dir, md_name)
        if md_text is None:
            continue
        if not extracted_dot and (dot_m := _DOT_BLOCK_RE.search(md_text)):
            extracted_dot = dot_m.group(1).strip()
        if not extracted_code and (code_m := _CODE_BLOCK_RE.search(md_text)):
            extracted_code = code_m.group(2).strip()
        body_snippets.extend(
            s
            for line in md_text.splitlines()
            if (s := line.strip()) and not s.startswith(("#", "!", "<!--", "```", ">"))
        )
    return body_snippets, extracted_dot, extracted_code


def _split_concepts_and_pitfalls(body_snippets: Sequence[str]) -> tuple[list[str], list[str]]:
    """본문 스니펫 목록을 일반 핵심 개념 문장과 PITFALL:/CAVEAT: 주의사항 항목으로 분리한다."""
    concepts: list[str] = []
    pitfalls: list[str] = []
    for line in body_snippets:
        if match := _PITFALL_LINE_RE.match(line):
            pitfalls.append(match.group(1).strip())
        else:
            concepts.append(line)
    return concepts, pitfalls


@dataclass(frozen=True, slots=True)
class _KataBuildContext:
    """단일 도서 콘텐츠의 실습(Kata) 패키지 생성을 위한 읽기 전용 빌드 컨텍스트."""

    content_dir: Path
    practice_content_dir: Path
    content_name: str
    lang_ext: str
    overwrite: bool
    dot_bin: str


def _build_kata_dot_code(
    content_name: str,
    ch_slug: str,
    sec_slug: str,
    pages: tuple[int, ...],
    extracted_dot: str,
) -> str:
    graph_id = f"Kata_{_DOT_ID_UNSAFE_RE.sub('_', sec_slug).strip('_') or 'Section'}"
    safe_content_name = sanitize_dot_label(content_name)
    safe_ch_slug = sanitize_dot_label(ch_slug)
    safe_sec_slug = sanitize_dot_label(sec_slug)
    return extracted_dot or (
        f"digraph {graph_id} {{\n"
        f'    rankdir=LR;\n'
        f'    node [shape=record, fontname="Menlo", fontsize=11];\n'
        f'    concept [label="{{{safe_content_name} | {safe_ch_slug}}}"];\n'
        f'    kata [label="{{{safe_sec_slug} | Pages {pages[0]}..{pages[-1]}}}"];\n'
        f"    concept -> kata;\n"
        f"}}"
    )


def _build_pitfalls_markdown(
    ctx: _KataBuildContext,
    ch_slug: str,
    sec_slug: str,
    pages_label: str,
    pitfall_notes: Sequence[str],
) -> str:
    """실습 과제의 실패 조건·오답 패턴·성능/환경 유의사항·디버깅 체크리스트를 담은 PITFALLS.md를 생성한다."""
    failure_items = (
        "\n".join(f"- {note}" for note in pitfall_notes)
        if pitfall_notes
        else (
            "- 빈 입력(Empty Input), 경계값(0, 음수, 최댓값), NULL/None 참조가 주어질 때의 예외 및 반환값을 확인하세요.\n"
            "- 전제 조건(Precondition)이나 불변식(Invariant)이 깨지는 입력에서 조기 실패(Fail-Fast)하는지 점검하세요."
        )
    )
    return (
        f"# ⚠️ {ctx.content_name} — {ch_slug} / {sec_slug} Pitfalls ({pages_label})\n\n"
        f"## 1. 언제 제대로 동작하지 않는가 (Failure Modes & Edge Cases)\n\n"
        f"{failure_items}\n\n"
        f"## 2. 자주 하는 실수 (Common Mistakes & Anti-Patterns)\n\n"
        f"- 원문({pages_label})의 핵심 제약 조건을 생략하고 해피 패스(Happy Path)만 구현하는 실수를 피하세요.\n"
        f"- 타입 변환, 오프셋 계산(Off-by-one), 리소스 해제 누락 여부를 함께 확인하세요.\n\n"
        f"## 3. 성능·메모리·환경 유의사항 (Performance & Environment Caveats)\n\n"
        f"- 대상 언어(`.{ctx.lang_ext}`) 컴파일러/런타임 버전 및 운영체제·커널 환경에 따른 동작 차이를 점검하세요.\n"
        f"- 불필요한 복사, 대용량 입력 시 시간·공간 복잡도 급증 구간이 없는지 확인하세요.\n\n"
        f"## 4. 디버깅 체크리스트 (Verification Checklist)\n\n"
        f"- [ ] `main.{ctx.lang_ext}` 실행 결과가 `README.md`의 목표 출력과 정확히 일치하는가?\n"
        f"- [ ] `test.{ctx.lang_ext}` 단위 테스트에 경계값·예외 케이스를 추가해 통과했는가?\n"
        f"- [ ] 원문({pages_label})의 수식·다이어그램·코드 제약을 모두 반영했는가?\n"
    )


def _build_single_kata_package(  # ---- 3
    ctx: _KataBuildContext,
    ch_slug: str,
    sec_slug: str,
    entries: list[dict[str, Any]],
) -> Path:
    pages = tuple(_parse_page_number(e.get("page"), default=1) for e in entries)
    md_files = tuple(
        normalize_nfc(str(e.get("md_file") or "")).strip() or f"{ctx.content_name}-{p}page.md"
        for e, p in zip(entries, pages, strict=True)
    )
    body_snippets, extracted_dot, extracted_code = _extract_markdown_highlights(
        ctx.content_dir, md_files
    )
    concept_lines, pitfall_notes = _split_concepts_and_pitfalls(body_snippets)

    concept_summary = (
        "\n".join(concept_lines[:6])
        if concept_lines
        else f"{ctx.content_name} {ch_slug} {sec_slug} 핵심 개념 요약"
    )
    dot_code = _build_kata_dot_code(ctx.content_name, ch_slug, sec_slug, pages, extracted_dot)

    kata_dir = ctx.practice_content_dir / ch_slug / sec_slug
    svg_filename = f"{sec_slug}-diag1.svg"
    render_dot_to_svg(dot_code, kata_dir / "assets" / svg_filename, dot_bin=ctx.dot_bin)

    pages_label = f"{pages[0]}p" if len(pages) == 1 else f"{pages[0]}p ~ {pages[-1]}p"
    ref_code, main_code, test_code = _default_main_and_test_templates(
        ctx.content_name, ch_slug, sec_slug, pages_label, extracted_code, ctx.lang_ext
    )

    page_links = "\n".join(
        f"- [{md_f}]({Path(os.path.relpath(ctx.content_dir / md_f, kata_dir)).as_posix()})"
        for md_f in md_files
    )
    readme_text = (
        f"# {ctx.content_name} — {ch_slug} / {sec_slug}\n\n"
        f"## 1. 원문 페이지 링크 ({pages_label})\n\n{page_links}\n\n"
        f"## 2. 핵심 개념 요약 및 DOT 다이어그램\n\n{concept_summary}\n\n"
        f"![DOT 다이어그램](assets/{svg_filename})\n\n```dot\n{dot_code}\n```\n\n"
        f"## 3. 따라 칠 목표 코드 및 예상 출력\n\n"
        f"```{ctx.lang_ext}\n{ref_code}\n```\n\n```text\nPASS: {sec_slug}\n```\n"
    )
    pitfalls_text = _build_pitfalls_markdown(ctx, ch_slug, sec_slug, pages_label, pitfall_notes)

    for file_path, file_content, is_readonly in (
        (kata_dir / "README.md", readme_text, True),
        (kata_dir / "PITFALLS.md", pitfalls_text, True),
        (kata_dir / ".skeleton" / f"main.{ctx.lang_ext}", main_code, True),
        (kata_dir / ".skeleton" / f"test.{ctx.lang_ext}", test_code, True),
        (kata_dir / f"main.{ctx.lang_ext}", main_code, False),
        (kata_dir / f"test.{ctx.lang_ext}", test_code, False),
    ):
        if ctx.overwrite or not file_path.is_file():
            atomic_write_text(file_path, file_content, readonly=is_readonly)

    return kata_dir


def build_practice_katas(  # ---- 4
    content_data_dir: PathLike,
    *,
    practice_root: PathLike | None = None,
    lang: str | None = None,
    overwrite: bool = False,
    dot_bin: str = "dot",
) -> PracticeBuildResult:
    """content_data/<콘텐츠명>/ 중간 결과물을 읽어 practice/<콘텐츠명>/<장>/<절>/ 실습 패키지를 생성한다."""
    c_dir = Path(content_data_dir).resolve()
    index_data = _load_content_index(c_dir)
    content_name = normalize_nfc(c_dir.name)
    ctx = _KataBuildContext(
        content_dir=c_dir,
        practice_content_dir=_resolve_practice_dir(c_dir, content_name, practice_root),
        content_name=content_name,
        lang_ext=resolve_language_extension(c_dir, explicit_lang=lang),
        overwrite=overwrite,
        dot_bin=dot_bin,
    )
    grouped = _group_toc_by_chapter_and_section(index_data, content_name)

    kata_dirs: list[Path] = []
    chapters_seen: set[str] = set()
    for (ch_slug, sec_slug), entries in grouped.items():
        chapters_seen.add(ch_slug)
        kata_dirs.append(_build_single_kata_package(ctx, ch_slug, sec_slug, entries))

    return PracticeBuildResult(
        content_name=ctx.content_name,
        practice_content_dir=ctx.practice_content_dir,
        lang_ext=ctx.lang_ext,
        chapters_count=len(chapters_seen),
        kata_dirs=tuple(kata_dirs),
    )


_KATA_LANG_EXTS: Final[tuple[str, ...]] = (
    "py",
    "cpp",
    "c",
    "java",
    "sql",
    "js",
    "ts",
    "sh",
    "go",
    "rs",
)
_IGNORED_KATA_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {"assets", "build", "bin", "node_modules"}
)


def _is_valid_kata_subpath(practice_root: Path, candidate: Path) -> bool:
    if not candidate.is_dir() or candidate.is_symlink():
        return False
    parts = candidate.relative_to(practice_root).parts
    return not any(p.startswith((".", "__")) or p in _IGNORED_KATA_DIR_NAMES for p in parts)


def _has_kata_code_or_pitfalls(candidate: Path) -> bool:
    if (candidate / "PITFALLS.md").is_file():
        return True
    return any(
        (candidate / f"main.{ext}").is_file() or (candidate / f"test.{ext}").is_file()
        for ext in _KATA_LANG_EXTS
    )


def _discover_leaf_kata_dirs(practice_root: Path) -> list[Path]:
    """practice_root 하위의 모든 리프 실습(Kata) 디렉터리를 정렬하여 반환한다."""
    if not practice_root.is_dir():
        return []
    all_dirs = [
        d
        for d in (practice_root, *sorted(practice_root.rglob("*")))
        if d == practice_root or _is_valid_kata_subpath(practice_root, d)
    ]
    marked = {d for d in all_dirs if _has_kata_code_or_pitfalls(d)}
    pruned = [d for d in all_dirs if not any(p in marked for p in d.parents)]
    if pruned == [practice_root]:
        return [practice_root] if any(practice_root.iterdir()) else []
    parent_dirs = {d.parent for d in pruned if d != practice_root}
    return [d for d in pruned if d not in parent_dirs]


def _infer_kata_slugs_and_lang(
    kata_dir: Path,
    practice_root: Path,
    explicit_lang: str | None,
) -> tuple[str, str, str, str]:
    parts = (
        kata_dir.relative_to(practice_root).parts
        if kata_dir != practice_root
        else (kata_dir.name,)
    )
    sec_slug = normalize_nfc(parts[-1])
    ch_slug = normalize_nfc(parts[-2]) if len(parts) >= 2 else "01_Chapter"
    content_name = normalize_nfc(parts[-3]) if len(parts) >= 3 else normalize_nfc(practice_root.name)
    detected_ext = next(
        (ext for ext in _KATA_LANG_EXTS if (kata_dir / f"main.{ext}").is_file()),
        None,
    )
    lang_ext = detected_ext or resolve_language_extension(kata_dir, explicit_lang=explicit_lang)
    return content_name, ch_slug, sec_slug, lang_ext


def _resolve_existing_kata_code(
    k_dir: Path,
    stem: str,
    fallback: str,
    overwrite: bool,
) -> tuple[bool, str]:
    found = next((k_dir / f"{stem}.{e}" for e in _KATA_LANG_EXTS if (k_dir / f"{stem}.{e}").is_file()), None)
    if found is None:
        return False, fallback
    text = fallback if overwrite else found.read_text(encoding="utf-8", errors="replace")
    return True, text


def _backfill_single_kata_dir(
    k_dir: Path,
    practice_root: Path,
    explicit_lang: str | None,
    overwrite: bool,
) -> bool:
    content_name, ch_slug, sec_slug, lang_ext = _infer_kata_slugs_and_lang(
        k_dir, practice_root, explicit_lang
    )
    ctx = _KataBuildContext(
        content_dir=k_dir,
        practice_content_dir=k_dir.parent,
        content_name=content_name,
        lang_ext=lang_ext,
        overwrite=overwrite,
        dot_bin="dot",
    )
    tpl_lang = (
        lang_ext
        if lang_ext in _COMMENT_PREFIX_BY_LANG
        else resolve_language_extension(k_dir, explicit_lang=explicit_lang)
    )
    ref_code, main_code, test_code = _default_main_and_test_templates(
        content_name, ch_slug, sec_slug, "실습 과제", "", tpl_lang
    )
    readme_text = (
        f"# {content_name} — {ch_slug} / {sec_slug}\n\n"
        f"## 1. 핵심 개념 요약\n\n{content_name} `{ch_slug}/{sec_slug}` 실습 과제입니다.\n\n"
        f"## 2. 따라 칠 목표 코드 및 예상 출력\n\n"
        f"```{lang_ext}\n{ref_code}\n```\n\n```text\nPASS: {sec_slug}\n```\n"
    )
    pitfalls_text = _build_pitfalls_markdown(ctx, ch_slug, sec_slug, "실습 과제", ())
    has_main, skel_main = _resolve_existing_kata_code(k_dir, "main", main_code, overwrite)
    has_test, skel_test = _resolve_existing_kata_code(k_dir, "test", test_code, overwrite)
    skel_dir = k_dir / ".skeleton"
    has_skel_m = any((skel_dir / f"main.{e}").is_file() for e in _KATA_LANG_EXTS)
    has_skel_t = any((skel_dir / f"test.{e}").is_file() for e in _KATA_LANG_EXTS)

    candidates = (
        (k_dir / "README.md", readme_text, (k_dir / "README.md").is_file(), True),
        (k_dir / "PITFALLS.md", pitfalls_text, (k_dir / "PITFALLS.md").is_file(), True),
        (skel_dir / f"main.{lang_ext}", skel_main, has_skel_m, True),
        (skel_dir / f"test.{lang_ext}", skel_test, has_skel_t, True),
        (k_dir / f"main.{lang_ext}", main_code, has_main, False),
        (k_dir / f"test.{lang_ext}", test_code, has_test, False),
    )
    wrote_any = False
    for file_path, content, exists, is_readonly in candidates:
        if overwrite or not exists:
            atomic_write_text(file_path, content, readonly=is_readonly)
            wrote_any = True
    return wrote_any


def backfill_practice_pitfalls(  # ---- 5
    target_dir: PathLike,
    *,
    lang: str | None = None,
    overwrite: bool = False,
) -> tuple[Path, ...]:
    """기존 practice/ 하위 리프 실습 폴더들에 누락된 4대 표준 파일(PITFALLS.md 등)을 일괄 보강한다."""
    root = Path(target_dir).resolve()
    practice_root = root / "practice" if (root / "practice").is_dir() else root
    kata_dirs = [
        d
        for d in _discover_leaf_kata_dirs(practice_root)
        if (d / "README.md").is_file() or _has_kata_code_or_pitfalls(d)
    ]
    return tuple(
        k_dir
        for k_dir in kata_dirs
        if _backfill_single_kata_dir(k_dir, practice_root, lang, overwrite)
    )


def _check_single_kata_dir(kata_dir: Path, practice_root: Path) -> list[str]:
    """단일 리프 실습 폴더에 4대 표준 파일(README.md, PITFALLS.md, main.<ext>, test.<ext>)이 모두 있는지 검사한다."""
    rel = kata_dir.relative_to(practice_root).as_posix()
    prefix = "" if rel == "." else f"{normalize_nfc(rel)}/"
    missing = [
        f"{prefix}{doc_name}"
        for doc_name in ("README.md", "PITFALLS.md")
        if not (kata_dir / doc_name).is_file()
    ]
    if not any((kata_dir / f"main.{ext}").is_file() for ext in _KATA_LANG_EXTS):
        missing.append(f"{prefix}main.<ext>")
    if not any((kata_dir / f"test.{ext}").is_file() for ext in _KATA_LANG_EXTS):
        missing.append(f"{prefix}test.<ext>")
    return missing


def verify_practice_katas(target_dir: PathLike) -> tuple[bool, tuple[str, ...]]:  # ---- 6
    """practice/ 하위 모든 실습(Kata) 폴더가 4대 표준 파일(README.md, PITFALLS.md, main.*, test.*)을 갖췄는지 검증한다."""
    root = Path(target_dir).resolve()
    practice_root = root / "practice" if (root / "practice").is_dir() else root
    kata_dirs = _discover_leaf_kata_dirs(practice_root)
    if not kata_dirs:
        return False, (f"practice_dir:{practice_root}",)

    missing: list[str] = []
    for k_dir in kata_dirs:
        missing.extend(_check_single_kata_dir(k_dir, practice_root))
    return len(missing) == 0, tuple(missing)



# ======== 노트 ========
# 1 content_data 색인 검증
#   설명: index.json의 pages(비어 있지 않은 객체 목록)와 toc(있으면 객체 목록)를 확인한다. content_data는 폐기된 PDF->Markdown 변환의 산출물이다.
#   관련: backlog/decisions/decision-1 - 이 모듈은 폐기된 PDF 파이프라인의 실습(practice) 단계에 속하며(backlog/decisions/decision-1) 제거될 수 있다.
# 2 경로 탈출 방지 읽기
#   설명: 파일명이 디렉터리 부분을 가지거나 '.', '..'면 None을 돌려 content_dir 밖을 읽지 못하게 한다.
#   개념: path traversal
# 3 kata 패키지 하나 생성
#   설명: 한 절(section)의 페이지 마크다운에서 개념·함정(PITFALL/CAVEAT)·DOT·코드 블록을 뽑아 DOT 다이어그램(SVG)과 main/test 템플릿을 만든다.
#   관련: backlog/decisions/decision-1 - 이 모듈은 폐기된 PDF 파이프라인의 실습(practice) 단계에 속하며(backlog/decisions/decision-1) 제거될 수 있다.
# 4 practice 생성 진입점
#   설명: content_data/<도서>/index.json의 목차를 장/절로 묶어 practice/<도서>/<장>/<절>/ 패키지를 만든다. `doctree practice` 명령이 부른다.
#   관련: src/doctree/doctree_cli/command_dispatcher.py (cmd_practice), backlog/decisions/decision-1 - 이 모듈은 폐기된 PDF 파이프라인의 실습(practice) 단계에 속하며(backlog/decisions/decision-1) 제거될 수 있다.
# 5 함정 노트 보충
#   설명: 이미 만들어진 practice의 말단 kata 폴더를 찾아 빠진 pitfalls를 다시 채운다(overwrite 옵션이 있다).
#   관련: backlog/decisions/decision-1 - 이 모듈은 폐기된 PDF 파이프라인의 실습(practice) 단계에 속하며(backlog/decisions/decision-1) 제거될 수 있다.
# 6 practice 검증(--check)
#   설명: 표준 실습 파일의 존재 여부만 검사해 누락 목록을 돌려준다. kata 폴더가 하나도 없으면 실패다.
#   관련: backlog/decisions/decision-1 - 이 모듈은 폐기된 PDF 파이프라인의 실습(practice) 단계에 속하며(backlog/decisions/decision-1) 제거될 수 있다.
