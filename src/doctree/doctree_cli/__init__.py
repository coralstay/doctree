# ---
# title: doctree_cli/__init__
# description: doctree_cli 도메인의 공개 API를 지연 import로 노출하는 패키지 초기화
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""doctree_cli 도메인: CLI 명령어 디스패처 및 터미널 트리 렌더러."""
from __future__ import annotations

from types import MappingProxyType

from doctree.runtime_env.workspace_settings import resolve_lazy_attribute

_EXPORTS = MappingProxyType({
    "DoctreeHelpFormatter": "command_dispatcher",
    "build_drive": "command_dispatcher",
    "build_parser": "command_dispatcher",
    "cmd_audit": "command_dispatcher",
    "cmd_auth": "command_dispatcher",
    "cmd_classify": "command_dispatcher",
    "cmd_help": "command_dispatcher",
    "cmd_layout": "command_dispatcher",
    "cmd_parse": "command_dispatcher",
    "cmd_practice": "command_dispatcher",
    "cmd_pull": "command_dispatcher",
    "cmd_scaffold": "command_dispatcher",
    "cmd_snippet": "command_dispatcher",
    "cmd_status": "command_dispatcher",
    "cmd_sync": "command_dispatcher",
    "cmd_update": "command_dispatcher",
    "main": "command_dispatcher",
    "now_kst": "command_dispatcher",
    "console": "terminal_tree_renderer",
    "format_classification_summary": "terminal_tree_renderer",
    "format_local_audit_reasons": "terminal_tree_renderer",
    "print_account_dashboard": "terminal_tree_renderer",
})

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    return resolve_lazy_attribute(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


