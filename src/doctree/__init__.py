# ---
# title: doctree/__init__
# description: 'doctree 패키지 루트: 버전 상수만 노출한다'
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""doctree: 구글 드라이브 콘텐츠(PDF) 스냅샷·분류·보안 감사 패키지."""

__version__ = "0.1.0"  # ---- 1
__all__ = ["__version__"]




# ======== 노트 ========
# 1 버전 상수
#   설명: `doctree -V`가 출력하는 버전이다. pyproject.toml의 version과 같은 값을 따로 적어 두므로 올릴 때 두 곳을 함께 바꿔야 한다.
#   관련: pyproject.toml, src/doctree/doctree_cli/command_dispatcher.py (build_parser)
