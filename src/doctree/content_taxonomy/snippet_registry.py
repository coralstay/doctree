"""실습 코드(practice/) 내 [[snippet]] / @snippet 추출, SHA-256 인간 검증 레지스트리 및 마크다운·블로그 내보내기 엔진."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from doctree.runtime_env.boundary_errors import ClassificationError
from doctree.runtime_env.workspace_settings import (
    PathLike,
    atomic_write_text,
    normalize_nfc,
)

__all__ = [
    "SNIPPET_EXT_LANG_MAP",
    "SnippetRecord",
    "SnippetStatus",
    "export_snippets_blog_posts",
    "export_snippets_markdown",
    "format_snippets_report",
    "load_snippet_registry",
    "save_snippet_registry",
    "scan_project_snippets",
    "sync_snippet_registry",
    "verify_snippet",
]


class SnippetStatus(StrEnum):
    """스니펫의 인간 검증(Human-in-the-Loop) 및 해시 정합성 상태."""

    VERIFIED = "VERIFIED"
    PENDING_REVIEW = "PENDING_REVIEW"
    OUTDATED = "OUTDATED"


SNIPPET_EXT_LANG_MAP: Final[Mapping[str, str]] = MappingProxyType(
    {
        ".py": "python",
        ".cpp": "cpp",
        ".cc": "cpp",
        ".c": "c",
        ".h": "cpp",
        ".hpp": "cpp",
        ".java": "java",
        ".sql": "sql",
    }
)

_SNIPPET_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\[\[\s*snippet(?:\s*\(\s*[\"']([^\"']*)[\"']\s*\))?\s*\]\]"
    r"|@snippet(?:\s*\(\s*[\"']([^\"']*)[\"']\s*\)|\s+([^\r\n]+))?)"
)
_INLINE_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"\[\[\s*snippet(?:\s*\(\s*[\"'][^\"']*[\"']\s*\))?\s*\]\]\s*"
)
_DECL_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:def|class|struct|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)"
    r"|([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?(?:->\s*[^\s{]+\s*)?\{"
    r"|(?:TABLE|VIEW|INDEX|FUNCTION|PROCEDURE|INTO|FROM|AS)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_RESERVED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"if", "for", "while", "switch", "catch", "return", "sizeof", "main"}
)
_SLUG_UNSAFE_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9가-힣_\-]+")


@dataclass(frozen=True, slots=True)
class SnippetRecord:
    """practice/ 소스 파일에서 추출된 단일 [[snippet]] / @snippet 코드 블록 및 검증 메타데이터."""

    symbol: str
    description: str
    file_path: str
    line_number: int
    language: str
    code: str
    code_sha256: str
    status: SnippetStatus = SnippetStatus.PENDING_REVIEW
    verified_at: str = ""
    reviewer: str = ""

    @property
    def registry_key(self) -> str:
        """레지스트리 내 고유 식별 키(file_path::symbol)."""
        return f"{self.file_path}::{self.symbol}"


def _parse_marker_description(line: str) -> str | None:
    """한 줄에서 [[snippet(...)]] 또는 @snippet(...) 마커를 찾아 설명 문자열(없으면 빈 문자열)을 반환한다."""
    match = _SNIPPET_MARKER_RE.search(line)
    if match is None:
        return None
    raw_desc = next(filter(None, match.groups()), "")
    return normalize_nfc(raw_desc).removesuffix("*/").strip()


def _extract_symbol_name(code: str, fallback: str) -> str:
    """코드 블록에서 대표 심볼(함수·클래스·구조체·SQL 대상) 이름을 추출한다."""
    for match in _DECL_SYMBOL_RE.finditer(code):
        candidate = next(filter(None, match.groups()), "")
        if candidate.lower() not in _RESERVED_KEYWORDS:
            return normalize_nfc(candidate)
    cleaned = _SLUG_UNSAFE_RE.sub("_", normalize_nfc(fallback).strip()).strip("_-")
    return cleaned or "snippet"


def _slice_snippet_code(lines: Sequence[str], start_idx: int, end_idx: int) -> str:
    """마커 위치(start_idx)부터 다음 마커 직전(end_idx)까지의 유효 코드 블록을 추출한다."""
    first_line = _INLINE_ATTR_RE.sub("", lines[start_idx]).strip()
    inline_code = (
        [first_line]
        if first_line and not first_line.startswith(("#", "//", "--", "/*", "*", "@"))
        else []
    )
    body_lines = [
        line
        for line in lines[start_idx + 1 : end_idx]
        if not line.strip().startswith(("# content=", "// content=", "-- content="))
    ]
    joined = "\n".join([*inline_code, *body_lines]).strip()
    return joined or lines[start_idx].strip()


def _extract_snippets_from_file(file_path: Path, project_root: Path) -> list[SnippetRecord]:
    """단일 소스 파일에서 모든 [[snippet]] / @snippet 블록을 추출한다."""
    lang = SNIPPET_EXT_LANG_MAP.get(file_path.suffix.lower())
    if lang is None:
        return []
    text = normalize_nfc(file_path.read_text(encoding="utf-8", errors="replace")).replace("\x00", "")
    lines = text.splitlines()
    markers = [
        (idx, desc)
        for idx, line in enumerate(lines)
        if (desc := _parse_marker_description(line)) is not None
    ]
    if not markers:
        return []

    rel_posix = normalize_nfc(file_path.resolve().relative_to(project_root.resolve()).as_posix())
    records: list[SnippetRecord] = []
    for pos, (line_idx, desc) in enumerate(markers):
        next_idx = markers[pos + 1][0] if pos + 1 < len(markers) else len(lines)
        code = _slice_snippet_code(lines, line_idx, next_idx)
        fallback_sym = desc or f"{file_path.stem}_L{line_idx + 1}"
        symbol = _extract_symbol_name(code, fallback_sym)
        code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
        records.append(
            SnippetRecord(
                symbol=symbol,
                description=desc or symbol,
                file_path=rel_posix,
                line_number=line_idx + 1,
                language=lang,
                code=code,
                code_sha256=code_sha,
            )
        )
    return records


def load_snippet_registry(registry_path: PathLike) -> dict[str, dict[str, Any]]:
    """tips/snippets_registry.json에서 기존 검증 레코드 사전을 로드한다."""
    p = Path(registry_path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("snippets"), dict):
        return {}
    return {
        normalize_nfc(k): dict(v)
        for k, v in raw["snippets"].items()
        if isinstance(v, dict)
    }


def _reconcile_record_with_registry(
    record: SnippetRecord,
    registry: Mapping[str, Mapping[str, Any]],
) -> SnippetRecord:
    """추출된 스니펫의 현재 코드 해시와 레지스트리의 인간 검증 해시를 대조해 상태를 결정한다."""
    entry = registry.get(record.registry_key) or registry.get(record.symbol)
    if not entry:
        return record
    verified_sha = str(entry.get("verified_sha256") or "").strip().lower()
    if not verified_sha:
        return record
    status = (
        SnippetStatus.VERIFIED
        if verified_sha == record.code_sha256
        else SnippetStatus.OUTDATED
    )
    return SnippetRecord(
        symbol=record.symbol,
        description=record.description,
        file_path=record.file_path,
        line_number=record.line_number,
        language=record.language,
        code=record.code,
        code_sha256=record.code_sha256,
        status=status,
        verified_at=normalize_nfc(entry.get("verified_at")),
        reviewer=normalize_nfc(entry.get("reviewer")),
    )


def save_snippet_registry(
    registry_path: PathLike,
    records: Sequence[SnippetRecord],
    prior_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> Path:
    """스니펫 검증 레지스트리(tips/snippets_registry.json)를 원자적으로 저장한다."""
    prior = prior_registry or {}
    snippets_map: dict[str, dict[str, Any]] = {}
    for rec in records:
        prev_entry = prior.get(rec.registry_key) or prior.get(rec.symbol) or {}
        prev_verified_sha = str(prev_entry.get("verified_sha256") or "").strip().lower()
        verified_sha = (
            rec.code_sha256
            if rec.status == SnippetStatus.VERIFIED
            else (prev_verified_sha if rec.status == SnippetStatus.OUTDATED else "")
        )
        snippets_map[rec.registry_key] = {
            "symbol": rec.symbol,
            "description": rec.description,
            "file_path": rec.file_path,
            "line_number": rec.line_number,
            "language": rec.language,
            "code_sha256": rec.code_sha256,
            "verified_sha256": verified_sha,
            "status": str(rec.status),
            "verified_at": rec.verified_at,
            "reviewer": rec.reviewer,
        }
    payload = {"schema_version": 1, "snippets": snippets_map}
    return atomic_write_text(
        Path(registry_path),
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _resolve_project_and_search_root(project_dir: PathLike) -> tuple[Path, Path]:
    """입력 경로로부터 (프로젝트 루트, 스니펫 탐색 루트)를 결정한다."""
    resolved = Path(project_dir).resolve()
    if resolved.name == "practice":
        return resolved.parent, resolved
    if (resolved / "practice").is_dir():
        return resolved, resolved / "practice"
    return resolved, resolved


def scan_project_snippets(project_dir: PathLike) -> tuple[SnippetRecord, ...]:
    """프로젝트의 practice/ 하위 소스 파일을 스캔하고 tips/snippets_registry.json과 상태를 대조한다."""
    proj_root, search_root = _resolve_project_and_search_root(project_dir)
    if not search_root.is_dir():
        return ()

    registry = load_snippet_registry(proj_root / "tips" / "snippets_registry.json")
    discovered: list[SnippetRecord] = []
    for candidate in sorted(search_root.rglob("*")):
        if not candidate.is_file() or candidate.suffix.lower() not in SNIPPET_EXT_LANG_MAP:
            continue
        if any(p.startswith(".") for p in candidate.relative_to(search_root).parts):
            continue
        discovered.extend(
            _reconcile_record_with_registry(raw_rec, registry)
            for raw_rec in _extract_snippets_from_file(candidate, proj_root)
        )
    return tuple(discovered)


def sync_snippet_registry(project_dir: PathLike) -> tuple[SnippetRecord, ...]:
    """프로젝트 내 스니펫을 스캔하여 tips/snippets_registry.json을 최신 상태로 갱신한다."""
    proj_root, _ = _resolve_project_and_search_root(project_dir)
    registry_path = proj_root / "tips" / "snippets_registry.json"
    prior = load_snippet_registry(registry_path)
    records = scan_project_snippets(proj_root)
    if records or registry_path.is_file():
        save_snippet_registry(registry_path, records, prior_registry=prior)
    return records


def verify_snippet(
    project_dir: PathLike,
    target_symbol: str,
    *,
    reviewer: str = "human",
    verified_at: str = "2026-10-30",
) -> tuple[SnippetRecord, ...]:
    """특정 심볼(또는 설명/키)의 스니펫을 VERIFIED 상태로 인간 검증 승인하고 레지스트리에 기록한다."""
    norm_target = normalize_nfc(target_symbol).strip()
    if not norm_target:
        raise ClassificationError("검증할 스니펫 심볼 또는 설명을 지정하세요.")

    proj_root, _ = _resolve_project_and_search_root(project_dir)
    registry_path = proj_root / "tips" / "snippets_registry.json"
    prior = load_snippet_registry(registry_path)
    records = scan_project_snippets(proj_root)

    norm_reviewer = normalize_nfc(reviewer).strip() or "human"
    norm_date = normalize_nfc(verified_at).strip() or "2026-10-30"
    updated_all: list[SnippetRecord] = []
    verified_matches: list[SnippetRecord] = []

    for rec in records:
        if norm_target in (rec.symbol, rec.description, rec.registry_key, rec.file_path):
            verified_rec = SnippetRecord(
                symbol=rec.symbol,
                description=rec.description,
                file_path=rec.file_path,
                line_number=rec.line_number,
                language=rec.language,
                code=rec.code,
                code_sha256=rec.code_sha256,
                status=SnippetStatus.VERIFIED,
                verified_at=norm_date,
                reviewer=norm_reviewer,
            )
            updated_all.append(verified_rec)
            verified_matches.append(verified_rec)
        else:
            updated_all.append(rec)

    if not verified_matches:
        raise ClassificationError(f"검증할 스니펫을 찾을 수 없습니다: {norm_target!r}")

    save_snippet_registry(registry_path, updated_all, prior_registry=prior)
    return tuple(verified_matches)


def _count_by_status(records: Sequence[SnippetRecord]) -> tuple[int, int, int]:
    """(VERIFIED 수, PENDING_REVIEW 수, OUTDATED 수) 튜플을 반환한다."""
    return (
        sum(r.status == SnippetStatus.VERIFIED for r in records),
        sum(r.status == SnippetStatus.PENDING_REVIEW for r in records),
        sum(r.status == SnippetStatus.OUTDATED for r in records),
    )


def format_snippets_report(
    records: Sequence[SnippetRecord],
    *,
    query: str | None = None,
) -> str:
    """스니펫 목록 및 검증 상태 집계를 터미널 출력용 문자열로 포맷한다."""
    norm_q = normalize_nfc(query).strip().lower() if query else ""
    filtered = [
        r
        for r in records
        if not norm_q or norm_q in f"{r.symbol}\n{r.description}\n{r.file_path}\n{r.code}".lower()
    ]
    verified_cnt, pending_cnt, outdated_cnt = _count_by_status(filtered)

    lines = [
        (
            f"🧩 스니펫 현황: 총 {len(filtered)}개 "
            f"(VERIFIED {verified_cnt} · PENDING_REVIEW {pending_cnt} · OUTDATED {outdated_cnt})"
        )
    ]
    for rec in filtered:
        meta = f" · reviewer={rec.reviewer} ({rec.verified_at})" if rec.reviewer else ""
        lines.append(
            f"- [{rec.status}] {rec.symbol} — {rec.description} "
            f"({rec.file_path}:{rec.line_number}, sha256={rec.code_sha256[:12]}...{meta})"
        )
    return "\n".join(lines) + "\n"


def export_snippets_markdown(
    project_dir: PathLike,
    out_path: PathLike | None = None,
) -> Path:
    """프로젝트 스니펫 전체를 마크다운 대시보드(tips/snippets.md)로 내보낸다."""
    proj_root, _ = _resolve_project_and_search_root(project_dir)
    records = sync_snippet_registry(proj_root)
    target = Path(out_path).resolve() if out_path else proj_root / "tips" / "snippets.md"
    verified_cnt, pending_cnt, outdated_cnt = _count_by_status(records)

    lines = [
        f"# 🧩 {normalize_nfc(proj_root.name)} 코드 스니펫 레지스트리",
        "",
        f"- 전체: {len(records)}개 (`VERIFIED` {verified_cnt} · `PENDING_REVIEW` {pending_cnt} · `OUTDATED` {outdated_cnt})",
        "",
    ]
    for rec in records:
        lines.extend(
            [
                f"## [{rec.status}] `{rec.symbol}` — {rec.description}",
                "",
                f"- 파일 위치: `{rec.file_path}:{rec.line_number}`",
                f"- 코드 SHA-256: `{rec.code_sha256}`",
                f"- 검증 정보: `{rec.reviewer or '미검증'}` ({rec.verified_at or '-'})",
                "",
                f"```{rec.language}",
                rec.code,
                "```",
                "",
            ]
        )
    return atomic_write_text(target, "\n".join(lines))


def export_snippets_blog_posts(
    project_dir: PathLike,
    posts_dir: PathLike | None = None,
    *,
    date_prefix: str = "2026-10-30",
) -> tuple[Path, ...]:
    """검증된(VERIFIED) 스니펫들을 기술 블로그 포스트용 마크다운(tips/posts/YYYY-MM-DD-<slug>.md)으로 내보낸다."""
    proj_root, _ = _resolve_project_and_search_root(project_dir)
    records = sync_snippet_registry(proj_root)
    if not records:
        raise ClassificationError(f"블로그 포스트로 내보낼 스니펫이 없습니다: {proj_root}")

    target_dir = Path(posts_dir).resolve() if posts_dir else proj_root / "tips" / "posts"
    verified = [r for r in records if r.status == SnippetStatus.VERIFIED]
    selected: Sequence[SnippetRecord] = verified or records
    norm_date = normalize_nfc(date_prefix).strip() or "2026-10-30"

    written: list[Path] = []
    for rec in selected:
        slug = _SLUG_UNSAFE_RE.sub("-", rec.symbol).strip("-_").lower() or "snippet"
        post_path = target_dir / f"{norm_date}-{slug}.md"
        post_text = (
            "---\n"
            f'title: "{rec.symbol} — {rec.description}"\n'
            f'date: "{norm_date}"\n'
            f'symbol: "{rec.symbol}"\n'
            f'status: "{rec.status}"\n'
            f'source: "{rec.file_path}:{rec.line_number}"\n'
            f'sha256: "{rec.code_sha256}"\n'
            f'reviewer: "{rec.reviewer or "pending"}"\n'
            "---\n\n"
            f"# {rec.symbol} — {rec.description}\n\n"
            f"- 원본 실습 파일: `{rec.file_path}:{rec.line_number}`\n"
            f"- 검증 상태: `{rec.status}` (SHA-256: `{rec.code_sha256[:16]}...`)\n\n"
            f"```{rec.language}\n{rec.code}\n```\n"
        )
        written.append(atomic_write_text(post_path, post_text))
    return tuple(written)
