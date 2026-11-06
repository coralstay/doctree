# ---
# title: duplicate_and_naming_linter
# description: 드라이브 정리용 중복 파일과 파일명 규칙 위반을 경고로 점검한다
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""드라이브 정리용 점검: 중복·파일명 경고 (KBI-5).

모든 점검은 레코드 목록만 읽는 순수 함수다. 드라이브 API를 호출하지 않고,
입력 레코드를 수정하지 않는다 (B2).
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from doctree.runtime_env.workspace_settings import format_record_path, normalize_nfc

__all__ = [
    "CheckWarning",
    "WarningKind",
    "find_exact_duplicates",
    "find_filename_issues",
    "find_same_title_different_content",
    "run_checks",
    "title_key",
]

Record = dict

_PDF_SUFFIX = re.compile(r"\.pdf\Z", re.IGNORECASE)
_COPY_OF_PREFIX = re.compile(r"^Copy of ")
_IGNORED = re.compile(r"[-\s]+")


class WarningKind(StrEnum):
    """점검 경고 종류 식별자 (StrEnum이므로 일반 문자열 비교와 100% 호환)."""

    EXACT_DUPLICATE = "exact_duplicate"
    SAME_TITLE_DIFFERENT_CONTENT = "same_title_different_content"
    MISSING_PDF_EXTENSION = "missing_pdf_extension"
    COPY_OF_PREFIX = "copy_of_prefix"
    SURROUNDING_WHITESPACE = "surrounding_whitespace"


def title_key(name: str) -> str:  # ---- 1
    """파일명(또는 제목)을 중복 점검용 제목 키로 정규화한다.

    순서: NFC 정규화 → 끝의 '.pdf'(대소문자 무시) 제거 → 앞의 'Copy of ' 제거
    → 하이픈·공백 제거 → 소문자화.
    """
    normalized_title = normalize_nfc(name)
    normalized_title = _PDF_SUFFIX.sub("", normalized_title)
    normalized_title = _COPY_OF_PREFIX.sub("", normalized_title)
    normalized_title = _IGNORED.sub("", normalized_title)
    return normalized_title.lower()


@dataclass(frozen=True, slots=True)
class CheckWarning:
    """점검 경고 하나. kind는 안정적인 식별 문자열(WarningKind), files는 관련 레코드, message는 한국어 설명."""

    kind: WarningKind | str
    files: list[Record]
    message: str


def _warning_sort_key(warning: CheckWarning) -> tuple[str, str]:
    first_record = warning.files[0]
    return (format_record_path(first_record), first_record.get("id") or "")


def _sort_records(records: Iterable[Record]) -> list[Record]:
    return sorted(records, key=lambda rec: (format_record_path(rec), rec.get("id") or ""))


def _format_record_paths(records: Iterable[Record]) -> str:
    return ", ".join(format_record_path(rec) for rec in records)


def _normalized_sha256(record: Record) -> str:
    return str(record.get("sha256") or "").strip().lower()


def find_exact_duplicates(records: Iterable[Record]) -> list[CheckWarning]:  # ---- 2
    """SHA-256이 같은 파일 묶음마다 '완전 중복' 경고. SHA-256이 빈 파일은 묶지 않는다 (B1: 이름 무관)."""
    groups: dict[str, list[Record]] = defaultdict(list)
    for rec in records:
        sha256 = _normalized_sha256(rec)
        if sha256:
            groups[sha256].append(rec)
    warnings = []
    for members in groups.values():
        if len(members) < 2:
            continue
        files = _sort_records(members)
        warnings.append(CheckWarning(
            kind=WarningKind.EXACT_DUPLICATE,
            files=files,
            message=f"완전 중복 {len(files)}개 (SHA-256 동일): {_format_record_paths(files)}",
        ))
    return sorted(warnings, key=_warning_sort_key)


def find_same_title_different_content(records: Iterable[Record]) -> list[CheckWarning]:  # ---- 3
    """제목 키가 같지만 SHA-256 값이 2종 이상인 묶음마다 '동명 다른 파일' 경고."""
    groups: dict[str, list[Record]] = defaultdict(list)
    for rec in records:
        if key := title_key(rec.get("name") or ""):
            groups[key].append(rec)
    warnings = []
    for members in groups.values():
        if len(members) < 2 or len({_normalized_sha256(m) for m in members}) < 2:
            continue
        files = _sort_records(members)
        warnings.append(CheckWarning(
            kind=WarningKind.SAME_TITLE_DIFFERENT_CONTENT,
            files=files,
            message=f"동명 다른 파일 {len(files)}개 (제목 같고 SHA-256 다름): {_format_record_paths(files)}",
        ))
    return sorted(warnings, key=_warning_sort_key)


_FILENAME_KINDS = (
    WarningKind.MISSING_PDF_EXTENSION,
    WarningKind.COPY_OF_PREFIX,
    WarningKind.SURROUNDING_WHITESPACE,
)


def find_filename_issues(records: Iterable[Record]) -> list[CheckWarning]:  # ---- 4
    """파일별 파일명 점검: .pdf 확장자 누락, 'Copy of ' 접두어, 앞뒤 공백."""
    found: dict[WarningKind, list[CheckWarning]] = {k: [] for k in _FILENAME_KINDS}
    for rec in records:
        name = normalize_nfc(rec.get("name"))
        path = format_record_path(rec)
        if not name.lower().endswith(".pdf"):
            found[WarningKind.MISSING_PDF_EXTENSION].append(CheckWarning(
                kind=WarningKind.MISSING_PDF_EXTENSION, files=[rec],
                message=f"파일명에 .pdf 확장자가 없음: {path}"))
        if name.startswith("Copy of "):
            found[WarningKind.COPY_OF_PREFIX].append(CheckWarning(
                kind=WarningKind.COPY_OF_PREFIX, files=[rec],
                message=f"파일명에 'Copy of ' 접두어가 있음: {path}"))
        if name != name.strip():
            found[WarningKind.SURROUNDING_WHITESPACE].append(CheckWarning(
                kind=WarningKind.SURROUNDING_WHITESPACE, files=[rec],
                message=f"파일명 앞뒤에 공백이 있음: {path!r}"))
    warnings: list[CheckWarning] = []
    for kind in _FILENAME_KINDS:
        warnings.extend(sorted(found[kind], key=_warning_sort_key))
    return warnings


def run_checks(records: Iterable[Record]) -> list[CheckWarning]:  # ---- 5
    """모든 점검을 실행해 종류 순(완전 중복 → 동명 다른 파일 → 파일명 점검) → 경로 순 경고 목록을 반환한다."""
    records = list(records)
    return (
        find_exact_duplicates(records)
        + find_same_title_different_content(records)
        + find_filename_issues(records)
    )


# ======== 노트 ========
# 1 제목 키 정규화 순서
#   설명: NFC -> 끝 '.pdf' 제거 -> 앞 'Copy of ' 제거 -> 하이픈·공백 제거 -> 소문자. 순서가 바뀌면 같은 제목이 다른 키가 될 수 있다.
# 2 완전 중복(B1)
#   설명: SHA-256이 같은 파일 묶음. 이름은 보지 않으며 해시가 빈 레코드는 묶지 않는다(빈 해시끼리 중복으로 오탐 방지).
# 3 동명 다른 파일
#   설명: 제목 키가 같은데 해시가 2종 이상인 묶음. 같은 책의 다른 판본이나 손상본 후보를 찾는다.
# 4 파일명 점검
#   설명: .pdf 확장자 누락, 'Copy of ' 접두어, 앞뒤 공백 세 가지를 종류별로 모아 경로 순으로 돌려준다.
# 5 점검 실행 순서
#   설명: 완전 중복 -> 동명 다른 파일 -> 파일명 순으로 이어 붙인다. CLI 출력 순서가 이 순서에 의존한다.
#   관련: KBI-5
