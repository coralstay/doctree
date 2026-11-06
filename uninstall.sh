#!/usr/bin/env bash
# ---
# title: uninstall
# description: doctree CLI 전역 심볼릭 링크를 제거하는 스크립트
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
set -euo pipefail

BIN_DIR="${DOCTREE_BIN_DIR:-${HOME}/.local/bin}"
TARGET="${BIN_DIR}/doctree"  # ---- 1

if [ -L "${TARGET}" ] || [ -f "${TARGET}" ]; then  # ---- 2
  rm -f "${TARGET}"
  echo "🗑️ doctree 전역 CLI 링크 제거 완료: ${TARGET}"
else
  echo "ℹ️ 제거할 doctree 전역 CLI 링크가 없습니다: ${TARGET}"
fi


# ======== 노트 ========
# 1 제거 대상은 링크 하나
#   설명: install.sh가 만든 BIN_DIR/doctree만 지운다. 저장소와 .venv는 그대로 둔다.
#   관련: install.sh
# 2 링크 또는 파일만 삭제
#   설명: 대상이 심볼릭 링크이거나 일반 파일일 때만 rm -f 한다. 디렉터리면 아무것도 하지 않고 안내만 출력한다.
