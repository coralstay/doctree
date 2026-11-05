"""런타임 환경 변수, 워크스페이스 경로 설정 및 공통 파일시스템·프로세스 기반 유틸리티."""
from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

__all__ = [
    "BASE_DIR",
    "CATEGORY_TREE_FILE",
    "CONTENT_CATEGORY_MAP_FILE",
    "DEFAULT_TOC_CHAPTER",
    "GOOGLE_DRIVE_FOLDER_ID",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "LOCAL_CONTENTS_CACHE_FILE",
    "PROJECT_LAYOUT_PROFILES_FILE",
    "SNAPSHOTS_DIR",
    "VIRUSTOTAL_API_KEY",
    "VT_CACHE_FILE",
    "WORKSPACE_DIR",
    "PathLike",
    "WorkspaceSettings",
    "atomic_write_bytes",
    "atomic_write_text",
    "build_default_toc_entry",
    "compute_sha256",
    "discover_local_pdfs",
    "format_record_path",
    "normalize_nfc",
    "render_dot_to_svg",
    "resolve_lazy_attribute",
    "sanitize_dot_label",
]

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PathLike = str | Path
BASE_DIR: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_TOC_CHAPTER: Final[str] = "01_기본_개념"
_FALLBACK_SVG: Final[str] = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="80">\n'
    '  <text x="10" y="40" font-family="Menlo" font-size="11">DOT Fallback SVG</text>\n'
    "</svg>\n"
)
_DOT_RECORD_ESCAPE_TABLE: Final[Mapping[int, str]] = MappingProxyType(
    str.maketrans(
        {
            **dict.fromkeys((*range(32), *range(127, 160), 0x2028, 0x2029), " "),
            '"': "'",
            "{": "(",
            "}": ")",
            "<": "[",
            ">": "]",
            "|": "/",
            "\\": "/",
            "`": "'",
        }
    )
)


def normalize_nfc(value: Any = "") -> str:
    """임의 입력을 문자열로 변환하고 유니코드 완성형(NFC)으로 정규화한다 (None만 빈 문자열로 치환)."""
    text = "" if value is None else str(value)
    return unicodedata.normalize("NFC", text)


def sanitize_dot_label(value: Any = "") -> str:
    """Graphviz DOT record 레이블 내부 구분자(|, {, }, <, >)·따옴표·비출력 제어문자를 안전하게 치환한다."""
    translated = normalize_nfc(value).translate(_DOT_RECORD_ESCAPE_TABLE)
    return "".join(ch if ch.isprintable() else " " for ch in translated)


def resolve_lazy_attribute(
    package_name: str,
    package_globals: dict[str, Any],
    exports: Mapping[str, str],
    name: str,
) -> Any:
    """PEP 562 패키지 지연 로딩(__getattr__) 공통 해석기: 최초 접근 시 하위 모듈을 로드하고 캐싱한다."""
    submodule_name = exports.get(name)
    if submodule_name is None:
        raise AttributeError(f"module {package_name!r} has no attribute {name!r}")
    module = importlib.import_module(f"{package_name}.{submodule_name}")
    value = getattr(module, name)
    package_globals[name] = value
    return value


def format_record_path(record: Mapping[str, Any]) -> str:
    """레코드의 folder_path와 name을 결합하여 정규화된 '폴더/파일명' 또는 '파일명'을 반환한다."""
    raw_folder = normalize_nfc(record.get("folder_path")).strip()
    folder = raw_folder.strip("/") if raw_folder.replace("/", "").strip() else ""
    name = normalize_nfc(record.get("name"))
    return f"{folder}/{name}" if folder else name


def atomic_write_bytes(
    target: PathLike,
    data: bytes,
    *,
    executable: bool = False,
    readonly: bool = False,
) -> Path:
    """대상 파일 경로에 임시 파일(.<name>.tmp)을 거쳐 바이트 데이터를 원자적으로 기록한다."""
    path = Path(target)
    tmp_path = path.parent / f".{path.name}.tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(data)
        if executable:
            tmp_path.chmod(0o755)
        elif readonly:
            tmp_path.chmod(0o444)
        else:
            tmp_path.chmod(0o644)
        tmp_path.replace(path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return path


def atomic_write_text(
    target: PathLike,
    content: str,
    *,
    executable: bool = False,
    readonly: bool = False,
) -> Path:
    """대상 파일 경로에 임시 파일(.<name>.tmp)을 거쳐 UTF-8 텍스트를 원자적으로 기록한다."""
    return atomic_write_bytes(
        target,
        content.encode("utf-8"),
        executable=executable,
        readonly=readonly,
    )


def compute_sha256(path: PathLike, chunk_size: int = 1024 * 1024) -> str:
    """로컬 파일의 SHA-256 해시(64자리 소문자 hex)를 청크 단위로 계산한다."""
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk_size는 1 이상의 정수여야 합니다.")
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def discover_local_pdfs(roots: Iterable[PathLike]) -> list[Path]:
    """지정된 루트(파일 또는 디렉터리)에서 심볼릭 링크/경로 중복 없이 모든 로컬 PDF를 정렬해 반환한다."""
    pdfs: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        candidates = (
            [base]
            if base.is_file()
            else [p for p in sorted(base.rglob("*")) if p.is_file()]
        )
        for candidate in candidates:
            if candidate.suffix.lower() != ".pdf":
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                pdfs.append(candidate)
    return pdfs


def render_dot_to_svg(dot_source: str, svg_path: Path, *, dot_bin: str = "dot") -> bool:
    """Graphviz(dot -Tsvg)로 DOT 코드를 렌더링하여 벡터 SVG 파일로 원자적 저장한다 (실패 시 폴백 SVG 기록)."""
    try:
        proc = subprocess.run(
            [dot_bin, "-Tsvg"],
            input=dot_source,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and "<svg" in proc.stdout:
            atomic_write_text(svg_path, proc.stdout, readonly=True)
            return True
    except (OSError, subprocess.SubprocessError):
        pass

    atomic_write_text(svg_path, _FALLBACK_SVG, readonly=True)
    return True


def build_default_toc_entry(
    content_name: str,
    page_number: int,
    *,
    chapter_title: str = "",
    section_title: str = "",
    default_section_suffix: str = "페이지_핵심",
    md_file: str | None = None,
) -> dict[str, Any]:
    """페이지 메타데이터로부터 표준 TOC 엔트리 사전을 생성한다."""
    return {
        "page": page_number,
        "chapter": normalize_nfc(chapter_title).strip() or DEFAULT_TOC_CHAPTER,
        "section": normalize_nfc(section_title).strip() or f"{page_number:02d}_{default_section_suffix}",
        "md_file": normalize_nfc(md_file).strip() or f"{content_name}-{page_number}page.md",
    }


@dataclass(frozen=True, slots=True)
class WorkspaceSettings:
    """읽기 전용(Read-Only) 워크스페이스 설정 객체.

    전역 상태를 직접 수정하거나 importlib.reload()를 호출하는 대신,
    WorkspaceSettings.from_env(env=...)로 격리된 설정 인스턴스를 생성해 주입할 수 있다.
    - category_tree_file: 16개 대분야·55개 세부분야 고정 분류 트리 정의서 (config/category_tree.yaml)
    - content_category_map_file: Drive 파일 ID ↔ 세부분야 ID 매핑 배정표 (config/content_category_map.yaml)
    - project_layout_profiles_file: 프로젝트 유형별 폴더·파일 구조 규격 (config/project_layout_profiles.yaml)
    """

    base_dir: Path
    google_service_account_file: str
    google_drive_folder_id: str
    snapshots_dir: str
    category_tree_file: str
    content_category_map_file: str
    project_layout_profiles_file: str
    workspace_dir: Path
    virustotal_api_key: str
    vt_cache_file: str
    local_contents_cache_file: str

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        base_dir: Path | None = None,
    ) -> WorkspaceSettings:
        source = os.environ if env is None else env
        root = base_dir or BASE_DIR
        snapshots_dir = source.get("DOCTREE_SNAPSHOTS_DIR", str(root / "snapshots"))
        category_tree_file = source.get(
            "DOCTREE_CATEGORY_TREE_FILE",
            str(root / "config" / "category_tree.yaml"),
        )
        content_category_map_file = source.get(
            "DOCTREE_CONTENT_CATEGORY_MAP_FILE",
            str(root / "config" / "content_category_map.yaml"),
        )
        project_layout_profiles_file = source.get(
            "DOCTREE_PROJECT_LAYOUT_PROFILES_FILE",
            str(root / "config" / "project_layout_profiles.yaml"),
        )
        return cls(
            base_dir=root,
            google_service_account_file=source.get(
                "GOOGLE_SERVICE_ACCOUNT_FILE", str(root / "credentials.json")
            ),
            google_drive_folder_id=source.get("GOOGLE_DRIVE_FOLDER_ID", ""),
            snapshots_dir=snapshots_dir,
            category_tree_file=category_tree_file,
            content_category_map_file=content_category_map_file,
            project_layout_profiles_file=project_layout_profiles_file,
            workspace_dir=Path(source.get("FREERIDE_WORKSPACE_DIR", str(root.parent))),
            virustotal_api_key=source.get("VIRUSTOTAL_API_KEY", ""),
            vt_cache_file=source.get(
                "DOCTREE_VT_CACHE_FILE", str(Path(snapshots_dir) / ".vt_cache.json")
            ),
            local_contents_cache_file=source.get(
                "DOCTREE_LOCAL_CONTENTS_CACHE_FILE",
                str(Path(snapshots_dir) / ".local_contents_cache.json"),
            ),
        )


_DEFAULT = WorkspaceSettings.from_env()

GOOGLE_SERVICE_ACCOUNT_FILE: str = _DEFAULT.google_service_account_file
GOOGLE_DRIVE_FOLDER_ID: str = _DEFAULT.google_drive_folder_id
SNAPSHOTS_DIR: str = _DEFAULT.snapshots_dir
CATEGORY_TREE_FILE: str = _DEFAULT.category_tree_file
CONTENT_CATEGORY_MAP_FILE: str = _DEFAULT.content_category_map_file
PROJECT_LAYOUT_PROFILES_FILE: str = _DEFAULT.project_layout_profiles_file
WORKSPACE_DIR: Path = _DEFAULT.workspace_dir
VIRUSTOTAL_API_KEY: str = _DEFAULT.virustotal_api_key
VT_CACHE_FILE: str = _DEFAULT.vt_cache_file
LOCAL_CONTENTS_CACHE_FILE: str = _DEFAULT.local_contents_cache_file
