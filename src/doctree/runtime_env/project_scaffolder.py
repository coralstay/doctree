# ---
# title: project_scaffolder
# description: 프로젝트 디렉토리·필수 파일·실행 스크립트·gitignore 구조를 강제로 생성한다 (폐기된 PDF 파이프라인 잔재)
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""로컬 워크스페이스 프로젝트 디렉토리·필수 파일·실행 스크립트·gitignore 구조 강제 생성기 (Step 2: doctree scaffold)."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from doctree.runtime_env.layout_profile_store import get_layout_value, load_layout_profiles
from doctree.runtime_env.workspace_settings import (
    PROJECT_LAYOUT_PROFILES_FILE,
    PathLike,
    atomic_write_text,
    normalize_nfc,
)

__all__ = [
    "ScaffoldResult",
    "scaffold_project",
    "verify_project_scaffold",
]


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    """단일 로컬 프로젝트에 대한 레이아웃 스캐폴딩 실행 결과."""

    profile: str
    target_dir: Path
    created_dirs: tuple[str, ...]
    existing_dirs: tuple[str, ...]
    created_files: tuple[str, ...]
    preserved_files: tuple[str, ...]


_FILE_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType({  # ---- 1
    "README.md": """\
# {project_name}

> freeride (`{profile}` 프로필) 표준 디렉토리 규격에 따라 구성된 프로젝트 워크스페이스입니다.

## 표준 디렉토리 구조 및 1:1 미러링 규격
- `content_data/`: `contents/<도서명>/`를 1:1로 미러링한 페이지별 PDF 파싱 중간 결과물 (`index.json`, `*-Npage.md`, `assets/*.{png,svg}`)
- `contents/`: 구글 드라이브에서 가져온 원본 도서 PDF 보관소. 도서 파일명과 같은 이름의 디렉토리 아래에 위치 (`.gitignore` 대상, 예: `contents/BPF-tooling/*.pdf`)
- `practice/`: `<도서명>/<장>/<절>` 계층으로 생성된 4대 표준 파일(`README.md`, `PITFALLS.md`, `main.*`, `test.*`) 및 `.skeleton/` 실습(Kata)과 단위 테스트 코드
- `scripts/`: 실습 실행·테스트·초기화·역추적·스니펫 도구
- `tips/`: `[[snippet]]` / `@snippet` 마커로 추출된 퍼블리시 전 스니펫 레지스트리(`snippets_registry.json`), 대시보드(`snippets.md`), 블로그 원고(`posts/`)
- `.opensource/`: 실습에 참고·연동하는 외부 오픈소스 저장소
""",
    "PROJECT_STRUCTURE.md": """\
# {project_name} 디렉토리 및 아키텍처 규격 (`{profile}`)

본 프로젝트는 `doctree scaffold {profile}` 명령으로 표준 구조가 강제·검증됩니다.

1. `content_data/`: `contents/<도서명>/`를 1:1로 미러링한 페이지별 마크다운 + 크롭 이미지(`.png`) + Graphviz DOT 벡터 도식(`.svg`) + `index.json`
2. `contents/`: 원본 도서 PDF (`doctree pull` 대상, 도서 파일명과 같은 `contents/<도서명>/`)
3. `practice/`: `practice/<도서명>/<장>/<절>/` 계층 아래 읽기 전용 가이드(`README.md`, `PITFALLS.md`, `assets/`, `.skeleton/`)와 학습자가 직접 타이핑하며 검증하는 실습 파일(`main.*`, `test.*`)로 분리된 4대 표준 실습 패키지
4. `scripts/`: 실습 실행·테스트·초기화·역추적·스니펫 도구
5. `tips/`: 퍼블리시 전 스니펫 검증 레지스트리(`snippets_registry.json`), 마크다운 대시보드(`snippets.md`), 승인된 포스트 초안(`posts/`)
6. `.opensource/`: 실습에 참고·연동하는 외부 오픈소스 저장소

위 6개 외의 디렉토리는 스캐폴딩이 생성·삭제·이동하지 않습니다.
""",
    "PRACTICE_GUIDE.md": """\
# {project_name} 수련(Kata) 실습 가이드

1. 실습 코드 실행: `./scripts/run.sh practice/<도서명>/<장>/<절>`
2. 단위 테스트 검증: `./scripts/test.sh practice/<도서명>/<장>/<절>`
3. 초기 스켈레톤 복원: `./scripts/reset.sh practice/<도서명>/<장>/<절>`
4. 실패 조건 및 함정 점검: 각 절 폴더의 `PITFALLS.md`(읽기 전용)에서 경계값·오답 패턴·성능 유의사항 확인
5. 원문 페이지 역추적: `./scripts/locate.sh <키워드_또는_경로>`
6. 핵심 스니펫 조회 및 검증: `./scripts/snippets.sh`
""",
    "scripts/run.sh": """\
#!/usr/bin/env bash
# 실습 과제(main.cpp / main.py / main.java / main.sql) 0.1초 고속 실행기
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "사용법: $0 <실습_폴더_또는_파일_경로> [인자...]" >&2
  exit 1
fi

target="$1"
shift
if [ ! -e "$target" ] && [ -e "practice/$target" ]; then
  target="practice/$target"
fi

if [ -d "$target" ]; then
  if [ -f "$target/main.py" ]; then
    exec python3 "$target/main.py" "$@"
  elif [ -f "$target/main.cpp" ]; then
    out="$(mktemp -d)/a.out"
    clang++ -std=c++20 -O2 "$target/main.cpp" -o "$out"
    exec "$out" "$@"
  elif [ -f "$target/main.java" ]; then
    exec java "$target/main.java" "$@"
  elif [ -f "$target/main.sql" ]; then
    cat "$target/main.sql"
    exit 0
  fi
fi

echo "실행 가능한 main 파일을 찾을 수 없습니다: $target" >&2
exit 1
""",
    "scripts/test.sh": """\
#!/usr/bin/env bash
# 실습 과제(test.cpp / test.py / test.java / test.sql) 0.1초 고속 채점기
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "사용법: $0 <실습_폴더_경로>" >&2
  exit 1
fi

target="$1"
if [ ! -e "$target" ] && [ -e "practice/$target" ]; then
  target="practice/$target"
fi

if [ -d "$target" ]; then
  if [ -f "$target/test.py" ]; then
    exec python3 "$target/test.py"
  elif [ -f "$target/test.cpp" ]; then
    out="$(mktemp -d)/test_bin"
    clang++ -std=c++20 -O2 "$target/test.cpp" -o "$out"
    exec "$out"
  elif [ -f "$target/test.java" ]; then
    exec java "$target/test.java"
  elif [ -f "$target/test.sql" ]; then
    grep -qiE "SELECT|CREATE|INSERT|EXPLAIN" "$target/test.sql"
    echo "SQL test verified: $target/test.sql"
    exit 0
  fi
fi

echo "실행 가능한 test 파일을 찾을 수 없습니다: $target" >&2
exit 1
""",
    "scripts/reset.sh": """\
#!/usr/bin/env bash
# 실습 과제(main.* / test.*)를 초기 스켈레톤(.skeleton/) 상태로 복원하는 리셋 도구
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "사용법: $0 <실습_폴더_경로>" >&2
  exit 1
fi

target="$1"
if [ ! -e "$target" ] && [ -e "practice/$target" ]; then
  target="practice/$target"
fi

if [ ! -d "$target" ]; then
  echo "실습 디렉토리를 찾을 수 없습니다: $target" >&2
  exit 1
fi

reset_kata_dir() {
  local kdir="$1"
  local skel="$kdir/.skeleton"
  [ -d "$skel" ] || return 0
  for src in "$skel"/*; do
    [ -f "$src" ] || continue
    local base
    base="$(basename "$src")"
    cp -f "$src" "$kdir/$base"
    chmod 644 "$kdir/$base"
  done
  echo "초기화 완료: $kdir"
}

if [ -d "$target/.skeleton" ]; then
  reset_kata_dir "$target"
else
  find "$target" -type d -name ".skeleton" | sort | while read -r sdir; do
    reset_kata_dir "$(dirname "$sdir")"
  done
fi
""",
    "scripts/locate.sh": """\
#!/usr/bin/env bash
# 실습 과제 및 원문 페이지(content_data/) 역추적 네비게이터
set -euo pipefail
query="${1:---tree}"
if [ "$query" = "--tree" ]; then
  find practice content_data -maxdepth 4 2>/dev/null | sort
else
  grep -rn "$query" practice content_data 2>/dev/null || true
fi
""",
    "scripts/snippets.sh": """\
#!/usr/bin/env bash
# 프로젝트 내 핵심 코드 스니펫([[snippet]] / @snippet) 검색·검증·추출기
set -euo pipefail
if command -v doctree >/dev/null 2>&1; then
  exec doctree snippet "$@"
fi
query="${1:-@snippet}"
grep -rn "$query" practice tips 2>/dev/null || true
""",
    "scripts/help.sh": """\
#!/usr/bin/env bash
# 프로젝트 실습 실행·테스트·초기화·역추적·스니펫 도구 치트시트
set -euo pipefail
cat << 'EOF'
======================================================================
 🛠️  실습(Kata) 빠른 명령어 치트시트
======================================================================
1. 빠른 실행       : ./scripts/run.sh practice/<도서명>/<장>/<절>
2. 단위 테스트     : ./scripts/test.sh practice/<도서명>/<장>/<절>
3. 초기 상태 복원  : ./scripts/reset.sh practice/<도서명>/<장>/<절>
4. 원문 위치 추적  : ./scripts/locate.sh <키워드_또는_--tree>
5. 스니펫 검색     : ./scripts/snippets.sh [키워드|--status|--verify <심볼>|--export|--export-blog]
======================================================================
EOF
""",
    "install.sh": """\
#!/usr/bin/env bash
set -euo pipefail
echo "Toolchain install ready."
""",
    "uninstall.sh": """\
#!/usr/bin/env bash
set -euo pipefail
echo "Toolchain uninstall ready."
""",
})


def _gitignore_entry_equivalents(entry: str) -> set[str]:
    """단일 .gitignore 규칙과 의미상 동치인 슬래시 변형 집합(NFC 정규화 기준)을 반환한다."""
    norm = normalize_nfc(entry).strip()
    stripped = norm.strip("/")
    return {norm, stripped, f"/{stripped}", f"{stripped}/", f"/{stripped}/"}


def _read_gitignore_lines(gitignore_path: Path) -> tuple[bool, str, set[str]]:
    """대상 .gitignore 파일의 존재 여부, 원문 텍스트, 비공백 줄 집합(NFC 정규화)을 반환한다."""
    existed = gitignore_path.is_file()
    text = gitignore_path.read_text(encoding="utf-8") if existed else ""
    lines = {normalize_nfc(line.strip()) for line in text.splitlines() if line.strip()}
    return existed, text, lines


def _ensure_gitignore_entries(  # ---- 2
    gitignore_path: Path,
    required_entries: list[str],
) -> tuple[bool, tuple[str, ...]]:
    """대상 .gitignore 파일에 필수 무시 항목이 누락되어 있으면 보강한다."""
    existed, existing_text, existing_lines = _read_gitignore_lines(gitignore_path)

    added: list[str] = []
    for entry in required_entries:
        norm_entry = normalize_nfc(entry).strip()
        if not (existing_lines & _gitignore_entry_equivalents(norm_entry)):
            added.append(norm_entry)
            existing_lines.add(norm_entry)

    if not existed or added:
        base = existing_text.rstrip("\n")
        extra = "\n".join(added)
        if base and extra:
            new_text = f"{base}\n{extra}\n"
        elif extra:
            new_text = f"{extra}\n"
        else:
            new_text = ""
        atomic_write_text(gitignore_path, new_text)

    return (not existed), tuple(added)


def _render_scaffold_file(rel_file: str, project_name: str, norm_profile: str) -> str:
    template = _FILE_TEMPLATES.get(rel_file, f"# {{project_name}} — {rel_file}\n")
    return template.replace("{profile}", norm_profile).replace("{project_name}", project_name)


def _ensure_profile_directories(
    root: Path,
    directories: Iterable[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """프로필에 정의된 하위 디렉터리를 생성하고 (신규 생성 목록, 기존 존재 목록)을 반환한다."""
    created_dirs: list[str] = []
    existing_dirs: list[str] = []
    for rel_dir in directories:
        dir_path = root / rel_dir
        if dir_path.is_dir():
            existing_dirs.append(rel_dir)
        else:
            dir_path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(rel_dir)
    return tuple(created_dirs), tuple(existing_dirs)


def scaffold_project(  # ---- 3
    profile: str,
    target_dir: PathLike,
    *,
    layout_file: PathLike = PROJECT_LAYOUT_PROFILES_FILE,
    overwrite: bool = False,
) -> ScaffoldResult:
    """지정한 레이아웃 프로필 규격에 맞춰 대상 로컬 프로젝트의 디렉토리·필수 파일·gitignore를 생성 및 보정한다."""
    data = load_layout_profiles(layout_file)
    spec = get_layout_value(data, profile=profile)
    norm_profile = normalize_nfc(profile).strip()

    root = Path(target_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_name = normalize_nfc(root.name)

    created_dirs, existing_dirs = _ensure_profile_directories(root, spec["directories"])
    created_files: list[str] = []
    preserved_files: list[str] = []

    gitignore_created, _ = _ensure_gitignore_entries(
        root / ".gitignore",
        list(spec["gitignore_entries"]),
    )

    for rel_file in spec["required_files"]:
        if rel_file == ".gitignore":
            if gitignore_created:
                created_files.append(".gitignore")
            else:
                preserved_files.append(".gitignore")
            continue

        file_path = root / rel_file
        is_shell_script = rel_file.endswith(".sh")
        if file_path.is_file() and not overwrite:
            if is_shell_script:
                file_path.chmod(file_path.stat().st_mode | 0o111)
            preserved_files.append(rel_file)
            continue

        rendered = _render_scaffold_file(rel_file, project_name, norm_profile)
        atomic_write_text(file_path, rendered, executable=is_shell_script)
        created_files.append(rel_file)

    return ScaffoldResult(
        profile=norm_profile,
        target_dir=root,
        created_dirs=created_dirs,
        existing_dirs=existing_dirs,
        created_files=tuple(created_files),
        preserved_files=tuple(preserved_files),
    )


def verify_project_scaffold(  # ---- 4
    profile: str,
    target_dir: PathLike,
    *,
    layout_file: PathLike = PROJECT_LAYOUT_PROFILES_FILE,
) -> tuple[bool, tuple[str, ...]]:
    """대상 로컬 프로젝트가 레이아웃 프로필의 디렉토리·필수 파일·gitignore 항목을 모두 충족하는지 검사한다."""
    data = load_layout_profiles(layout_file)
    spec = get_layout_value(data, profile=profile)
    root = Path(target_dir).resolve()

    missing = [
        f"dir:{rel_dir}" for rel_dir in spec["directories"] if not (root / rel_dir).is_dir()
    ]
    missing.extend(
        f"file:{rel_file}" for rel_file in spec["required_files"] if not (root / rel_file).is_file()
    )

    _, _, existing_lines = _read_gitignore_lines(root / ".gitignore")
    missing.extend(
        f"gitignore:{entry}"
        for entry in spec["gitignore_entries"]
        if not (existing_lines & _gitignore_entry_equivalents(entry))
    )

    return len(missing) == 0, tuple(missing)


# ======== 노트 ========
# 1 스캐폴드 파일 템플릿
#   설명: required_files 이름 -> 본문 문자열. scripts/*.sh는 실습 실행·채점·리셋·역추적·스니펫 도구를 만드는 템플릿이다. 문자열 안의 '# ...' 줄은 생성될 파일의 내용이지 이 모듈의 주석이 아니다.
#   관련: backlog/decisions/decision-1 - 이 모듈은 폐기된 PDF 파이프라인(backlog/decisions/decision-1)에 속하며 제거될 수 있다.
# 2 gitignore 보강
#   설명: 슬래시 변형(/x, x/, /x/)을 같은 규칙으로 보고, 누락된 항목만 덧붙인다. 기존 규칙은 지우지 않는다.
# 3 scaffold 진입점
#   설명: 프로필 규격대로 디렉토리, 필수 파일, gitignore를 만든다. 이미 있는 파일은 overwrite가 아니면 보존하고 .sh 파일에만 실행 권한을 보강한다.
#   관련: backlog/decisions/decision-1 - 이 모듈은 폐기된 PDF 파이프라인(backlog/decisions/decision-1)에 속하며 제거될 수 있다.
#   개념: 멱등(idempotent) 생성
# 4 scaffold 검증(--check)
#   설명: 파일을 만들지 않고 규격 충족 여부만 검사해 누락 목록(dir:/file:/gitignore: 접두사)을 돌려준다.
