# ---
# title: workspace_sync_engine
# description: 드라이브 스냅샷과 로컬 contents/ 를 SHA-256으로 3-Way 비교하고 도서를 가져오는(pull) 동기화 엔진
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""구글 드라이브 스냅샷(SHA-256) ⟷ 로컬 워크스페이스 contents/(SHA-256) 고속 스캔·3-Way 비교 및 도서 가져오기(pull) 엔진."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from doctree.runtime_env.boundary_errors import (
    ClassificationError,
    DriveListError,
    SnapshotStorageError,
)
from doctree.runtime_env.workspace_settings import (
    PathLike,
    atomic_write_bytes,
    atomic_write_text,
    compute_sha256,
    discover_local_pdfs,
    format_record_path,
    normalize_nfc,
)

__all__ = [
    "LocalPdfEntry",
    "MatchedPdfGroup",
    "SyncComparisonResult",
    "compare_drive_and_local",
    "discover_workspace_content_dirs",
    "format_sync_report",
    "load_local_contents_cache",
    "pull_drive_pdf_to_project",
    "resolve_drive_record_for_pull",
    "save_local_contents_cache",
    "scan_workspace_contents",
]

Record = Mapping[str, Any]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class LocalPdfEntry:
    """로컬 워크스페이스 프로젝트의 contents/ 하위 PDF 메타데이터 및 SHA-256 레코드."""

    relative_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class MatchedPdfGroup:
    """드라이브 스냅샷과 로컬 contents/ 양쪽 모두에 존재하는 동일 SHA-256 도서 묶음."""

    sha256: str
    drive_records: tuple[dict, ...]
    local_entries: tuple[LocalPdfEntry, ...]


@dataclass(frozen=True, slots=True)
class SyncComparisonResult:
    """드라이브 ⟷ 로컬 contents/ 3-Way 정합성 비교 결과."""

    drive_only: tuple[dict, ...]
    local_only: tuple[LocalPdfEntry, ...]
    matched: tuple[MatchedPdfGroup, ...]
    cache_hits: int
    recomputed: int


def _parse_local_cache_entry(key: Any, val: Any) -> tuple[str, dict[str, Any]] | None:
    """로컬 해시 캐시의 단일 키-값 쌍이 유효하면 (NFC 정규화 키, 캐시 레코드)를 반환한다."""
    if not isinstance(key, str) or not key.strip() or not isinstance(val, dict):
        return None
    sha = str(val.get("sha256", "")).lower()
    size = val.get("size_bytes")
    mtime_ns = val.get("mtime_ns")
    if (
        not _SHA256_RE.match(sha)
        or type(size) is not int
        or size < 0
        or type(mtime_ns) is not int
        or mtime_ns < 0
    ):
        return None
    return normalize_nfc(key), {
        "size_bytes": size,
        "mtime_ns": mtime_ns,
        "sha256": sha,
    }


def load_local_contents_cache(cache_path: PathLike | None) -> dict[str, dict]:
    """로컬 contents/ 해시 캐시(snapshots/.local_contents_cache.json)를 로드한다."""
    if not cache_path:
        return {}
    p = Path(cache_path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, val in raw.items():
        if (parsed := _parse_local_cache_entry(key, val)) is not None:
            out[parsed[0]] = parsed[1]
    return out


def save_local_contents_cache(cache_path: PathLike, cache: Mapping[str, dict]) -> Path:
    """로컬 contents/ 해시 캐시를 임시 파일(.tmp) + replace로 원자적으로 저장한다."""
    target = Path(cache_path)
    payload = {normalize_nfc(k): dict(v) for k, v in sorted(cache.items())}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        return atomic_write_text(target, text)
    except OSError as exc:
        raise SnapshotStorageError(f"{target}: 로컬 해시 캐시 저장 실패: {exc}") from exc


def discover_workspace_content_dirs(workspace_dir: PathLike) -> list[Path]:
    """워크스페이스 하위 프로젝트들의 contents/ 디렉터리 목록을 정렬해 반환한다."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return []
    return sorted(
        p / "contents"
        for p in ws.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "contents").is_dir()
    )


def scan_workspace_contents(  # ---- 1
    workspace_dir: PathLike,
    *,
    cache_path: PathLike | None = None,
) -> tuple[list[LocalPdfEntry], int, int]:
    """워크스페이스 하위 프로젝트들의 contents/ 폴더를 고속 스캔하여 (LocalPdfEntry 목록, 캐시 적중 수, 재계산 수)를 반환한다."""
    ws = Path(workspace_dir).resolve()
    content_roots = discover_workspace_content_dirs(ws)
    if not content_roots:
        return [], 0, 0

    pdf_paths = discover_local_pdfs(content_roots)
    prior_cache = load_local_contents_cache(cache_path)
    next_cache: dict[str, dict] = {}
    entries: list[LocalPdfEntry] = []
    cache_hits = 0
    recomputed = 0

    for pdf_path in pdf_paths:
        stat = pdf_path.stat()
        rel_path = normalize_nfc(pdf_path.resolve().relative_to(ws).as_posix())
        size_bytes = int(stat.st_size)
        mtime_ns = int(stat.st_mtime_ns)

        cached = prior_cache.get(rel_path)
        if (
            cached is not None
            and cached["size_bytes"] == size_bytes
            and cached["mtime_ns"] == mtime_ns
        ):
            sha256 = cached["sha256"]
            cache_hits += 1
        else:
            sha256 = compute_sha256(pdf_path)
            recomputed += 1

        entry = LocalPdfEntry(
            relative_path=rel_path,
            sha256=sha256,
        )
        entries.append(entry)
        next_cache[rel_path] = {
            "size_bytes": size_bytes,
            "mtime_ns": mtime_ns,
            "sha256": sha256,
        }

    entries.sort(key=lambda e: e.relative_path)
    if cache_path:
        save_local_contents_cache(cache_path, next_cache)
    return entries, cache_hits, recomputed


def _normalize_drive_record(raw: Record) -> dict[str, Any]:
    """드라이브 스냅샷 레코드의 파일명과 폴더 경로를 NFC로 정규화한 사본을 반환한다."""
    return {
        **dict(raw),
        "name": normalize_nfc(raw.get("name")),
        "folder_path": normalize_nfc(raw.get("folder_path")),
    }


def _drive_record_sort_key(record: Record) -> tuple[str, str, str]:
    """드라이브 레코드를 (폴더 경로, 파일명, 파일 ID) 순으로 정렬하는 키."""
    return (record["folder_path"], record["name"], str(record.get("id", "")))


def _group_drive_by_sha(drive_records: Iterable[Record]) -> tuple[dict[str, list[dict]], list[dict]]:
    drive_by_sha: dict[str, list[dict]] = {}
    drive_without_sha: list[dict] = []
    for raw in drive_records:
        rec = _normalize_drive_record(raw)
        sha = str(rec.get("sha256") or "").strip().lower()
        (drive_by_sha.setdefault(sha, []) if sha else drive_without_sha).append(rec)
    return drive_by_sha, drive_without_sha


def _group_local_by_sha(
    local_entries: Iterable[LocalPdfEntry],
) -> tuple[dict[str, list[LocalPdfEntry]], list[LocalPdfEntry]]:
    local_by_sha: dict[str, list[LocalPdfEntry]] = {}
    local_without_sha: list[LocalPdfEntry] = []
    for entry in local_entries:
        norm_entry = LocalPdfEntry(
            relative_path=normalize_nfc(entry.relative_path),
            sha256=entry.sha256.strip().lower(),
        )
        target = local_by_sha.setdefault(norm_entry.sha256, []) if norm_entry.sha256 else local_without_sha
        target.append(norm_entry)
    return local_by_sha, local_without_sha


def compare_drive_and_local(  # ---- 2
    drive_records: Iterable[Record],
    local_entries: Iterable[LocalPdfEntry],
    *,
    cache_hits: int = 0,
    recomputed: int = 0,
) -> SyncComparisonResult:
    """드라이브 스냅샷 레코드와 로컬 PDF 엔트리를 SHA-256 해시 기준으로 3-Way 대조한다."""
    drive_by_sha, drive_without_sha = _group_drive_by_sha(drive_records)
    local_by_sha, local_without_sha = _group_local_by_sha(local_entries)

    drive_shas = set(drive_by_sha.keys())
    local_shas = set(local_by_sha.keys())

    drive_only: list[dict] = list(drive_without_sha)
    for sha in drive_shas - local_shas:
        drive_only.extend(drive_by_sha[sha])
    drive_only.sort(key=_drive_record_sort_key)

    local_only: list[LocalPdfEntry] = list(local_without_sha)
    for sha in local_shas - drive_shas:
        local_only.extend(local_by_sha[sha])
    local_only.sort(key=lambda e: (e.relative_path, e.sha256))

    matched: list[MatchedPdfGroup] = []
    for sha in drive_shas & local_shas:
        d_recs = sorted(drive_by_sha[sha], key=_drive_record_sort_key)
        l_ents = sorted(local_by_sha[sha], key=lambda e: e.relative_path)
        matched.append(
            MatchedPdfGroup(
                sha256=sha,
                drive_records=tuple(d_recs),
                local_entries=tuple(l_ents),
            )
        )
    matched.sort(key=lambda g: (g.drive_records[0]["name"], g.sha256))

    return SyncComparisonResult(
        drive_only=tuple(drive_only),
        local_only=tuple(local_only),
        matched=tuple(matched),
        cache_hits=cache_hits,
        recomputed=recomputed,
    )


def _format_matched_group(group: MatchedPdfGroup) -> str:
    d_name = group.drive_records[0]["name"]
    locs = ", ".join(e.relative_path for e in group.local_entries)
    return f"- {d_name} ⟷ {locs} (sha256={group.sha256[:12]}...)"


def format_sync_report(result: SyncComparisonResult, total_drive: int, total_local: int) -> str:
    """3-Way 동기화 비교 결과를 터미널 출력용 문자열로 포맷한다."""
    lines = [
        (
            f"🔄 3-Way 동기화 현황: 드라이브 스냅샷 {total_drive}권 · "
            f"로컬 {total_local}개 (캐시 적중 {result.cache_hits} · 재계산 {result.recomputed}) ➔ "
            f"양쪽 일치 {len(result.matched)}권 · 드라이브 전용 {len(result.drive_only)}권 · 로컬 전용 {len(result.local_only)}개"
        ),
        "",
        f"[양쪽 일치 (Matched: {len(result.matched)}권)]",
    ]
    if result.matched:
        lines.extend(_format_matched_group(group) for group in result.matched)
    else:
        lines.append("양쪽 일치하는 도서가 없습니다.")

    lines.extend(["", f"[드라이브에만 있는 것 (Drive Only: {len(result.drive_only)}권)]"])
    if result.drive_only:
        lines.extend(
            f"- {rec['name']} (id={rec.get('id', '')}, path={format_record_path(rec)})"
            for rec in result.drive_only
        )
    else:
        lines.append("드라이브 전용 도서가 없습니다.")

    lines.extend(["", f"[로컬에만 있는 것 (Local Only: {len(result.local_only)}개)]"])
    if result.local_only:
        lines.extend(
            f"- {entry.relative_path} (sha256={entry.sha256[:12]}...)"
            for entry in result.local_only
        )
    else:
        lines.append("로컬 전용 도서가 없습니다.")

    return "\n".join(lines) + "\n"


def _raise_if_ambiguous_or_return_single(
    matches: list[dict],
    ambiguous_message: str,
) -> dict | None:
    """매칭된 레코드가 1건이면 반환하고, 2건 이상이면 ClassificationError를 발생시킨다."""
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ClassificationError(ambiguous_message)
    return None


def resolve_drive_record_for_pull(records: Iterable[Record], query: str) -> dict:  # ---- 3
    """사용자 질의(file_id, sha256, 파일명 또는 제목 키워드)로 스냅샷에서 단일 도서 레코드를 찾는다."""
    norm_q = normalize_nfc(query).strip()
    if not norm_q:
        raise ClassificationError("가져올 도서 식별자(파일 ID, SHA-256 또는 도서명)를 지정하세요.")

    recs = [_normalize_drive_record(r) for r in records]
    q_lower = norm_q.lower()

    for rec in recs:
        if str(rec.get("id", "")) == norm_q:
            return rec

    sha_matches = [
        r for r in recs if str(r.get("sha256") or "").strip().lower() == q_lower
    ]
    if (
        matched := _raise_if_ambiguous_or_return_single(
            sha_matches,
            f"동일한 SHA-256 해시의 도서가 {len(sha_matches)}권 있습니다. 파일 ID로 지정하세요: "
            f"{', '.join(str(r.get('id', '')) for r in sha_matches)}",
        )
    ) is not None:
        return matched

    q_pdf = q_lower if q_lower.endswith(".pdf") else f"{q_lower}.pdf"
    exact_name_matches = [
        r for r in recs if r["name"].lower() in (q_lower, q_pdf)
    ]
    if (
        matched := _raise_if_ambiguous_or_return_single(
            exact_name_matches,
            f"동일한 이름의 도서가 {len(exact_name_matches)}권 있습니다. 파일 ID로 지정하세요: "
            f"{', '.join(str(r.get('id', '')) for r in exact_name_matches)}",
        )
    ) is not None:
        return matched

    sub_matches = [r for r in recs if q_lower in r["name"].lower()]
    candidates = ", ".join(f"{r['name']}({r.get('id', '')})" for r in sub_matches[:5])
    if (
        matched := _raise_if_ambiguous_or_return_single(
            sub_matches,
            f"검색어 {norm_q!r}와 일치하는 도서가 {len(sub_matches)}권 있습니다. 더 구체적으로 지정하세요: {candidates}",
        )
    ) is not None:
        return matched

    raise ClassificationError(f"스냅샷에서 {norm_q!r} 도서를 찾을 수 없습니다.")


_UNSAFE_FILENAME_CHARS_TABLE: Mapping[int, str] = MappingProxyType(
    str.maketrans(
        {
            **dict.fromkeys((*range(32), *range(127, 160), 0x2028, 0x2029), " "),
            "/": "-",
            "\\": "-",
        }
    )
)
_LEADING_DOT_OR_SPACE_RE = re.compile(r"^[\s.]+")
_TRIVIAL_STEM_CHARS_RE = re.compile(r"[\s.\-]+")


def _clean_filename_candidate(raw: str | None) -> str:
    if raw is None:
        return ""
    translated = normalize_nfc(raw).translate(_UNSAFE_FILENAME_CHARS_TABLE)
    return "".join(ch if ch.isprintable() else " " for ch in translated).strip()


def _sanitize_pdf_filename(output_name: str | None, fallback_name: str) -> str:  # ---- 4
    """출력 파일명에서 경로 구분자와 위험 문자를 제거하고 .pdf 확장자를 보장한다."""
    raw_filename = _clean_filename_candidate(output_name) or _clean_filename_candidate(fallback_name)
    safe_stem = normalize_nfc(_LEADING_DOT_OR_SPACE_RE.sub("", raw_filename))
    stem_body = safe_stem[:-4] if safe_stem.lower().endswith(".pdf") else safe_stem
    if not _TRIVIAL_STEM_CHARS_RE.sub("", stem_body) or safe_stem.lower() == "pdf":
        raise ClassificationError(f"유효하지 않은 출력 파일명입니다: {output_name or fallback_name!r}")
    return safe_stem if safe_stem.lower().endswith(".pdf") else f"{safe_stem}.pdf"


def _parse_safe_path_segments(raw_path: str, error_label: str) -> tuple[str, ...]:  # ---- 5
    """슬래시(/)로 구분된 상대 경로 문자열의 모든 세그먼트가 안전한지 검증해 튜플로 반환한다."""
    norm = normalize_nfc(raw_path).strip().strip("/")
    if not norm or not norm.isprintable() or "\\" in norm:
        raise ClassificationError(f"유효하지 않은 {error_label}입니다: {raw_path!r}")
    parts = tuple(norm.split("/"))
    if any(not p or not p.strip() or p in (".", "..") for p in parts):
        raise ClassificationError(f"유효하지 않은 {error_label}입니다: {raw_path!r}")
    return parts


def _resolve_pull_target_dir(  # ---- 6
    ws: Path,
    project: str,
    subdir: str | None = None,
    *,
    book_dir_name: str | None = None,
) -> Path:
    """대상 프로젝트 경로와 --subdir로부터 contents/ 하위 대상 폴더를 결정한다.

    하위 경로(<project>/<경로> 또는 --subdir)를 지정하지 않으면 도서 파일명(확장자 제외)과
    같은 이름의 디렉토리(contents/<도서명>/)를 대상 폴더로 사용한다.
    """
    proj_parts = _parse_safe_path_segments(project, "대상 프로젝트 이름")
    project_root = proj_parts[0]
    inline_sub = proj_parts[2:] if len(proj_parts) > 1 and proj_parts[1] == "contents" else proj_parts[1:]
    extra_sub: tuple[str, ...] = ()
    if subdir is not None:
        sub_parts = _parse_safe_path_segments(subdir, "하위 경로(subdir)")
        extra_sub = sub_parts[1:] if sub_parts[0] == "contents" else sub_parts
    if not inline_sub and not extra_sub and book_dir_name:
        extra_sub = (book_dir_name,)
    return ws.joinpath(project_root, "contents", *inline_sub, *extra_sub)


def pull_drive_pdf_to_project(  # ---- 7
    drive: Any,
    records: Iterable[Record],
    query: str,
    project: str,
    *,
    workspace_dir: PathLike,
    subdir: str | None = None,
    **options: Any,
) -> tuple[dict, Path]:
    """스냅샷에서 도서를 찾아 로컬 프로젝트의 contents/<도서명>/(또는 지정한 하위 경로) 폴더로 내려받고 해시 캐시를 갱신한다."""
    rec = resolve_drive_record_for_pull(records, query)
    ws = Path(workspace_dir).resolve()
    output_name: str | None = options.get("output_name")
    cache_path: PathLike | None = options.get("cache_path")
    safe_filename = _sanitize_pdf_filename(output_name, rec["name"])
    target_contents_dir = _resolve_pull_target_dir(
        ws,
        project,
        subdir=subdir,
        book_dir_name=safe_filename[:-4],
    )

    dest_path = target_contents_dir / safe_filename
    file_id = str(rec["id"])
    expected_sha256 = str(rec.get("sha256") or "").strip().lower()

    if not getattr(drive, "service", None):
        raise DriveListError("구글 드라이브 서비스가 활성화되지 않았습니다 (자격 증명 파일 확인 필요).")
    try:
        raw_bytes = drive.service.files().get_media(fileId=file_id).execute()
        data = bytes(raw_bytes)
    except Exception as exc:
        raise DriveListError(f"구글 드라이브 파일 다운로드 실패 ({file_id}): {exc}") from exc

    if expected_sha256:
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha256:  # ---- 8
            raise DriveListError(
                f"다운로드한 파일의 SHA-256 해시가 스냅샷과 일치하지 않습니다 ({actual_sha} != {expected_sha256})."
            )

    atomic_write_bytes(dest_path, data)

    if cache_path:
        scan_workspace_contents(ws, cache_path=cache_path)

    return rec, dest_path


# ======== 노트 ========
# 1 해시 캐시 고속 스캔
#   설명: 상대 경로 + size + mtime_ns가 같으면 저장된 SHA-256을 재사용하고 아니면 다시 계산한다. 수백 권 PDF를 매번 해시하지 않기 위한 장치이며, 캐시는 스캔마다 현재 파일 기준으로 새로 쓴다.
#   관련: snapshots/.local_contents_cache.json (git 제외)
#   개념: 캐시 무효화(size+mtime)
# 2 SHA-256 3-Way 대조
#   설명: 파일명이 아니라 해시로 드라이브 전용 / 로컬 전용 / 양쪽 일치를 가른다. 해시가 없는 항목은 항상 한쪽 전용으로 분류해 오탐 일치를 만들지 않는다.
#   개념: 3-Way 비교
# 3 도서 식별 우선순위
#   설명: 파일 ID -> SHA-256 -> 정확한 파일명 -> 키워드 순으로 찾고, 단계마다 2건 이상이면 ClassificationError로 파일 ID 지정을 요구한다. 모호한 질의로 엉뚱한 책을 받지 않기 위함이다.
# 4 출력 파일명 정화
#   설명: 경로 구분자와 위험 문자를 제거하고, 앞의 점·공백과 의미 없는 이름('.pdf', '-')을 거부한다. 그 결과로만 .pdf 확장자를 보장한다.
# 5 안전한 상대 경로 세그먼트
#   설명: 빈 세그먼트, '.', '..', 역슬래시, 비출력 문자를 거부해 --subdir로 워크스페이스 밖에 쓰지 못하게 한다.
#   개념: path traversal
# 6 pull 대상 폴더 결정
#   설명: 하위 경로를 따로 주지 않으면 도서 파일명과 같은 contents/<도서명>/ 디렉터리를 쓴다.
# 7 pull 진입점
#   설명: 스냅샷에서 레코드를 찾아 내려받은 뒤 스냅샷의 SHA-256과 대조하고, 일치할 때만 원자적으로 저장한 다음 해시 캐시를 갱신한다.
# 8 무결성 대조
#   설명: 다운로드 바이트의 SHA-256이 스냅샷 값과 다르면 파일을 쓰지 않고 중단한다(손상·변조 방지).
