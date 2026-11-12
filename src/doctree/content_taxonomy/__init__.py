# ---
# title: content_taxonomy/__init__
# description: content_taxonomy 도메인의 공개 API를 지연 import로 노출하는 패키지 초기화
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""content_taxonomy 도메인: 고정 분류 트리 검증, 중복·파일명 린터 및 실습 Kata 빌더."""
from __future__ import annotations

from types import MappingProxyType

from doctree.runtime_env.workspace_settings import resolve_lazy_attribute

_EXPORTS = MappingProxyType({  # ---- 1
    "Category": "category_classifier",
    "ClassificationCatalog": "category_classifier",
    "ClassificationError": "category_classifier",
    "ClassificationResult": "category_classifier",
    "ClassifiedContent": "category_classifier",
    "ContentAssignment": "category_classifier",
    "Subcategory": "category_classifier",
    "classify_records": "category_classifier",
    "load_catalog": "category_classifier",
    "CheckWarning": "duplicate_and_naming_linter",
    "WarningKind": "duplicate_and_naming_linter",
    "run_checks": "duplicate_and_naming_linter",
    "PracticeBuildResult": "practice_kata_builder",
    "SUPPORTED_LANG_MAP": "practice_kata_builder",
    "backfill_practice_pitfalls": "practice_kata_builder",
    "build_practice_katas": "practice_kata_builder",
    "resolve_language_extension": "practice_kata_builder",
    "verify_practice_katas": "practice_kata_builder",
    "SNIPPET_EXT_LANG_MAP": "snippet_registry",
    "SnippetRecord": "snippet_registry",
    "SnippetStatus": "snippet_registry",
    "export_snippets_blog_posts": "snippet_registry",
    "export_snippets_markdown": "snippet_registry",
    "format_snippets_report": "snippet_registry",
    "load_snippet_registry": "snippet_registry",
    "save_snippet_registry": "snippet_registry",
    "scan_project_snippets": "snippet_registry",
    "sync_snippet_registry": "snippet_registry",
    "verify_snippet": "snippet_registry",
})

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    return resolve_lazy_attribute(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})




# ======== 노트 ========
# 1 지연 로딩 수출표
#   설명: 공개 이름 -> 하위 모듈 표. 이름에 처음 접근할 때 해당 모듈만 로드한다.
#   관련: src/doctree/runtime_env/workspace_settings.py (resolve_lazy_attribute)
#   개념: PEP 562
