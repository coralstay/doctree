# ---
# title: snapshot_diff_tracker
# description: 드라이브 목록 스냅샷을 저장하고 직전 스냅샷과 비교한다
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""목록 스냅샷 저장과 직전 스냅샷 비교 (KBI-4).

snapshots/
  YYYY-MM-DD.json   실행 시점의 PDF 목록 (같은 날 재실행 시 덮어씀)
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from doctree.runtime_env.boundary_errors import DoctreeBoundaryError, SnapshotStorageError
from doctree.runtime_env.workspace_settings import PathLike, atomic_write_text, normalize_nfc

__all__ = [
    "KST",
    "Change",
    "Changes",
    "RunResult",
    "change_summary",
    "diff_snapshots",
    "load_latest_snapshot",
    "load_snapshot_records",
    "record_run",
    "save_snapshot",
    "snapshot_date",
]

Record = Mapping[str, Any]

KST = timezone(timedelta(hours=9))  # ---- 1
_SNAPSHOT_NAME = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})\.json\Z")


def snapshot_date(now: datetime) -> date:  # ---- 2
    """시간대(tzinfo)가 있는 시각은 항상 한국 표준시(KST, UTC+9) 날짜로 변환한다."""
    if not isinstance(now, datetime):
        raise TypeError("now는 datetime 인스턴스여야 합니다.")
    if now.tzinfo is not None:
        return now.astimezone(KST).date()
    return now.date()


def save_snapshot(records: Iterable[Record], snapshots_dir: PathLike, now: datetime) -> Path:  # ---- 3
    """records를 snapshots_dir/<now의 KST 날짜>.json으로 저장하고 경로를 돌려준다.

    같은 날짜 파일이 있으면 덮어쓴다. 레코드는 id 순으로, 키는 사전순으로 정렬해
    같은 입력이면 같은 바이트가 나오게 한다(UTF-8, ensure_ascii=False).
    """
    recs = sorted((dict(r) for r in records), key=lambda r: str(r.get("id", "")))
    payload = {"taken_at": now.isoformat(), "count": len(recs), "records": recs}
    path = Path(snapshots_dir) / f"{snapshot_date(now).isoformat()}.json"
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return atomic_write_text(path, text)


def load_snapshot_records(  # ---- 4
    path: PathLike,
    *,
    error_cls: type[DoctreeBoundaryError] = SnapshotStorageError,
    wrap_io_errors: bool = False,
) -> list[dict[str, Any]]:
    """단일 스냅샷 JSON 파일을 읽고 records 배열 및 count 정합성을 검증해 반환한다."""
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if wrap_io_errors:
            raise error_cls(f"{target}: 스냅샷 파일을 읽을 수 없습니다: {exc}") from exc
        raise
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise error_cls(f"{target}: 스냅샷에 유효한 records 배열이 없습니다.")
    records = list(data["records"])
    if not all(isinstance(record, dict) for record in records):
        raise error_cls(f"{target}: 스냅샷 records 항목은 모두 객체여야 합니다.")
    if "count" in data and (type(data["count"]) is not int or data["count"] != len(records)):
        raise error_cls(f"{target}: 스냅샷 count와 records 개수가 일치하지 않습니다.")
    return records


def load_latest_snapshot(snapshots_dir: PathLike) -> tuple[date, list[dict]] | None:  # ---- 5
    """snapshots_dir에서 날짜가 가장 최근인 스냅샷의 (날짜, records)를 돌려준다.

    없으면 None. 'YYYY-MM-DD.json' 형식(유효한 날짜)이 아닌 파일은 무시한다.
    오늘 날짜 파일도 후보이므로, 이번 실행분을 저장하기 전에 호출해야 한다(B4).
    """
    snapshots_path = Path(snapshots_dir)
    if not snapshots_path.is_dir():
        return None
    candidates: list[tuple[date, Path]] = []
    for candidate_file in snapshots_path.iterdir():
        match = _SNAPSHOT_NAME.match(candidate_file.name)
        if not match or not candidate_file.is_file():
            continue
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        candidates.append((day, candidate_file))
    if not candidates:
        return None
    day, path = max(candidates)
    return day, load_snapshot_records(path, error_cls=SnapshotStorageError, wrap_io_errors=True)


@dataclass(frozen=True, slots=True)
class Change:
    """같은 id 파일의 한 가지 속성 변경. name은 현재(새) 파일명, old/new는 바뀐 값."""

    id: str
    name: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class Changes:  # ---- 6
    """직전 스냅샷 대비 변경 묶음.

    같은 책 판정은 id만 쓴다(B1). 한 파일에 이름 변경과 폴더 이동이 동시에 일어나면
    renamed와 moved 양쪽에 각각 기록된다(B2).
    """

    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    renamed: list[Change] = field(default_factory=list)
    moved: list[Change] = field(default_factory=list)
    content_changed: list[Change] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.renamed or self.moved or self.content_changed)


def _record_sort_key(record: Record) -> tuple[str, str, str]:
    return (
        normalize_nfc(record.get("folder_path")),
        normalize_nfc(record.get("name")),
        str(record.get("id", "")),
    )


def _change_sort_key(change: Change) -> tuple[str, str]:
    return (change.name, change.id)


def _content_change(old_record: Record, new_record: Record) -> tuple[str, str] | None:  # ---- 7
    """내용 변경이면 (old, new) 표시값을, 아니면 None.

    SHA-256이 양쪽 다 있으면 SHA-256으로(공백 제거·소문자 정규화 기준), 한쪽이라도 비어 있으면 크기(size_bytes)로 판단한다.
    """
    old_sha256 = str(old_record.get("sha256") or "").strip().lower()
    new_sha256 = str(new_record.get("sha256") or "").strip().lower()
    if old_sha256 and new_sha256:
        return (old_sha256, new_sha256) if old_sha256 != new_sha256 else None
    old_size = old_record.get("size_bytes")
    new_size = new_record.get("size_bytes")
    return (str(old_size), str(new_size)) if old_size != new_size else None


def _index_records_by_id(records: Iterable[Record]) -> dict[str, dict[str, Any]]:  # ---- 8
    """스냅샷 레코드 목록에서 ID 유효성 및 중복 여부를 검증하고 {file_id: dict} 사전을 반환한다."""
    indexed: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise SnapshotStorageError("스냅샷 레코드는 매핑 객체여야 합니다.")
        raw_id = record.get("id")
        file_id = normalize_nfc(raw_id).strip() if isinstance(raw_id, str) else ""
        if not file_id:
            raise SnapshotStorageError("스냅샷 레코드에 유효한 id가 없습니다.")
        if file_id in indexed:
            raise SnapshotStorageError(f"스냅샷 내 중복 파일 ID가 존재합니다: {file_id!r}")
        indexed[file_id] = {**dict(record), "id": file_id}
    return indexed


def diff_snapshots(old_records: Iterable[Record], new_records: Iterable[Record]) -> Changes:  # ---- 9
    """두 레코드 목록을 파일 id 기준으로 비교한다 (한글 파일명·폴더 경로는 NFC 정규화 기준으로 대조)."""
    old_by_id = _index_records_by_id(old_records)
    new_by_id = _index_records_by_id(new_records)

    added = sorted((new_by_id[file_id] for file_id in new_by_id.keys() - old_by_id.keys()), key=_record_sort_key)
    removed = sorted((old_by_id[file_id] for file_id in old_by_id.keys() - new_by_id.keys()), key=_record_sort_key)
    renamed: list[Change] = []
    moved: list[Change] = []
    content_changed: list[Change] = []

    for file_id in new_by_id.keys() & old_by_id.keys():
        old_record = old_by_id[file_id]
        new_record = new_by_id[file_id]
        old_name = normalize_nfc(old_record.get("name"))
        new_name = normalize_nfc(new_record.get("name"))
        old_folder = normalize_nfc(old_record.get("folder_path"))
        new_folder = normalize_nfc(new_record.get("folder_path"))

        if old_name != new_name:
            renamed.append(Change(file_id, new_name, old_name, new_name))
        if old_folder != new_folder:
            moved.append(Change(file_id, new_name, old_folder, new_folder))
        content_diff = _content_change(old_record, new_record)
        if content_diff is not None:
            content_changed.append(Change(file_id, new_name, content_diff[0], content_diff[1]))

    return Changes(
        added=added,
        removed=removed,
        renamed=sorted(renamed, key=_change_sort_key),
        moved=sorted(moved, key=_change_sort_key),
        content_changed=sorted(content_changed, key=_change_sort_key),
    )


@dataclass(frozen=True, slots=True)
class RunResult:
    """record_run 실행 결과."""

    changes: Changes
    total: int
    first_run: bool
    snapshot_path: Path


def change_summary(run: RunResult) -> str:
    """최근 변경 한 줄 요약: '최초 등록 N권' / '변경 없음' / '추가 1 · 삭제 2 …'."""
    if run.first_run:
        return f"최초 등록 {run.total}권"
    ch = run.changes
    if ch.is_empty:
        return "변경 없음"
    parts = [
        ("추가", len(ch.added)),
        ("삭제", len(ch.removed)),
        ("이름 변경", len(ch.renamed)),
        ("폴더 이동", len(ch.moved)),
        ("내용 변경", len(ch.content_changed)),
    ]
    return " · ".join(f"{label} {n}" for label, n in parts if n)


def record_run(records: Iterable[Record], snapshots_dir: PathLike, now: datetime) -> RunResult:  # ---- 10
    """한 번의 실행을 기록한다: 직전 스냅샷 읽기 → 비교 → 오늘 스냅샷 저장."""
    recs = [dict(r) for r in records]
    prior = load_latest_snapshot(snapshots_dir)
    if prior is None:
        changes = Changes()
    else:
        _, old = prior
        changes = diff_snapshots(old, recs)
    first_run = prior is None
    snap = save_snapshot(recs, snapshots_dir, now)
    return RunResult(changes, len(recs), first_run, snap)


# ======== 노트 ========
# 1 스냅샷 날짜는 KST 기준
#   설명: 실행 시각을 UTC+9로 바꿔 날짜를 정한다. 서버 시간대와 무관하게 같은 날 재실행이 같은 파일을 덮어쓴다.
#   관련: tests/runtime_env/test_boundary_errors.py (KST 자정 경계)
#   개념: 시간대 경계
# 2 KST 날짜 변환
#   설명: tzinfo가 있는 시각만 KST로 바꾸고, naive 시각은 이미 현지 시각으로 보고 그대로 쓴다.
# 3 결정적 직렬화
#   설명: 레코드는 id 순, 키는 사전순, UTF-8 ensure_ascii=False로 써서 같은 입력이면 같은 바이트가 나온다. atomic_write_text로 쓰므로 도중 실패해도 기존 스냅샷이 보존된다.
#   관련: src/doctree/runtime_env/workspace_settings.py
#   개념: 결정적 출력, 원자적 쓰기
# 4 스냅샷 무결성 검증
#   설명: records가 리스트이고 항목이 모두 객체이며 count가 있으면 개수와 일치하는지 확인한다. type(...) is not int로 bool을 count로 받지 않는다.
# 5 최신 스냅샷 선택
#   설명: 파일명이 YYYY-MM-DD.json인 것만 후보로 삼고 날짜가 가장 큰 것을 고른다. 오늘 파일도 후보이므로 이번 실행분을 저장하기 전에 불러야 한다(B4).
# 6 변경 묶음의 규칙
#   설명: 같은 책 판정은 id만 쓴다(B1). 이름 변경과 폴더 이동이 동시에 일어나면 renamed와 moved 양쪽에 기록한다(B2).
# 7 내용 변경 판정
#   설명: 양쪽 sha256이 모두 있으면 해시로, 한쪽이라도 비면 size_bytes로 비교한다. 해시가 없는 레코드를 오탐 없이 다루기 위한 폴백이다.
# 8 id 검증과 색인
#   설명: id가 없거나 중복이면 SnapshotStorageError로 중단한다. 손상된 스냅샷으로 diff를 내면 잘못된 변경 이력이 남기 때문이다.
# 9 id 기준 diff
#   설명: 집합 연산으로 추가·삭제를 구하고 공통 id에서 이름·폴더·내용 변경을 따로 판정한다. 이름과 폴더는 NFC 정규화 후 비교한다(macOS NFD 파일명 대응).
#   개념: 유니코드 NFC
# 10 실행 기록 순서
#   설명: 직전 스냅샷 읽기 -> 비교 -> 오늘 스냅샷 저장 순서를 지켜야 오늘 파일이 비교 대상이 되는 일이 없다(B4).
