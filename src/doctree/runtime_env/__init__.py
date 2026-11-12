# ---
# title: runtime_env/__init__
# description: runtime_env 도메인의 공개 API를 지연 import로 노출하는 패키지 초기화
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""runtime_env 도메인: 런타임 환경 변수·경로 설정, 레이아웃 프로필 저장소, 스캐폴더 및 경계 예외 계층."""
from __future__ import annotations

from types import MappingProxyType

from doctree.runtime_env.workspace_settings import resolve_lazy_attribute

_EXPORTS = MappingProxyType({  # ---- 1
    "BoundaryKind": "boundary_errors",
    "DoctreeBoundaryError": "boundary_errors",
    "DriveAuthError": "boundary_errors",
    "ExternalBoundary": "boundary_errors",
    "SecurityAuditError": "boundary_errors",
    "SnapshotStorageError": "boundary_errors",
    "coerce_boundary_error": "boundary_errors",
    "add_layout_items": "layout_profile_store",
    "format_layout_value": "layout_profile_store",
    "get_layout_value": "layout_profile_store",
    "load_layout_profiles": "layout_profile_store",
    "remove_layout_items": "layout_profile_store",
    "set_layout_field": "layout_profile_store",
    "ScaffoldResult": "project_scaffolder",
    "scaffold_project": "project_scaffolder",
    "verify_project_scaffold": "project_scaffolder",
    "BASE_DIR": "workspace_settings",
    "CATEGORY_TREE_FILE": "workspace_settings",
    "CONTENT_CATEGORY_MAP_FILE": "workspace_settings",
    "PROJECT_LAYOUT_PROFILES_FILE": "workspace_settings",
    "SNAPSHOTS_DIR": "workspace_settings",
    "WORKSPACE_DIR": "workspace_settings",
    "WorkspaceSettings": "workspace_settings",
    "atomic_write_text": "workspace_settings",
    "compute_sha256": "workspace_settings",
    "discover_local_pdfs": "workspace_settings",
    "format_record_path": "workspace_settings",
    "normalize_nfc": "workspace_settings",
    "render_dot_to_svg": "workspace_settings",
    "sanitize_dot_label": "workspace_settings",
})

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    return resolve_lazy_attribute(__name__, globals(), _EXPORTS, name)  # ---- 2


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})




# ======== 노트 ========
# 1 지연 로딩 수출표
#   설명: 공개 이름 -> 하위 모듈 이름 표. 패키지를 import해도 하위 모듈은 이름에 처음 접근할 때만 로드한다.
#   관련: src/doctree/runtime_env/workspace_settings.py (resolve_lazy_attribute)
#   개념: PEP 562 모듈 __getattr__, 지연 import
# 2 __getattr__ 위임
#   설명: 해석 로직은 workspace_settings.resolve_lazy_attribute 한 곳에 있고, 다른 도메인 __init__도 같은 함수를 쓴다.
