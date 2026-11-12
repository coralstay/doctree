# ---
# title: drive_sync/__init__
# description: drive_sync 도메인의 공개 API를 지연 import로 노출하는 패키지 초기화
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""drive_sync 도메인: 구글 드라이브 PDF 메타데이터 수집, 스냅샷 비교 및 로컬 동기화 엔진."""
from __future__ import annotations

from types import MappingProxyType

from doctree.runtime_env.workspace_settings import resolve_lazy_attribute

_EXPORTS = MappingProxyType({
    "DriveListError": "drive_pdf_fetcher",
    "DrivePdfRecord": "drive_pdf_fetcher",
    "GoogleDriveManager": "drive_pdf_fetcher",
    "Change": "snapshot_diff_tracker",
    "Changes": "snapshot_diff_tracker",
    "KST": "snapshot_diff_tracker",
    "RunResult": "snapshot_diff_tracker",
    "SnapshotStorageError": "snapshot_diff_tracker",
    "change_summary": "snapshot_diff_tracker",
    "diff_snapshots": "snapshot_diff_tracker",
    "load_latest_snapshot": "snapshot_diff_tracker",
    "load_snapshot_records": "snapshot_diff_tracker",
    "record_run": "snapshot_diff_tracker",
    "save_snapshot": "snapshot_diff_tracker",
    "snapshot_date": "snapshot_diff_tracker",
    "LocalPdfEntry": "workspace_sync_engine",
    "MatchedPdfGroup": "workspace_sync_engine",
    "SyncComparisonResult": "workspace_sync_engine",
    "compare_drive_and_local": "workspace_sync_engine",
    "discover_workspace_content_dirs": "workspace_sync_engine",
    "format_sync_report": "workspace_sync_engine",
    "pull_drive_pdf_to_project": "workspace_sync_engine",
    "scan_workspace_contents": "workspace_sync_engine",
})

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    return resolve_lazy_attribute(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


