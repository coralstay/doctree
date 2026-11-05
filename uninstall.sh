#!/usr/bin/env bash
# doctree CLI 전역 심볼릭 링크 제거 스크립트
set -euo pipefail

BIN_DIR="${DOCTREE_BIN_DIR:-${HOME}/.local/bin}"
TARGET="${BIN_DIR}/doctree"

if [ -L "${TARGET}" ] || [ -f "${TARGET}" ]; then
  rm -f "${TARGET}"
  echo "🗑️ doctree 전역 CLI 링크 제거 완료: ${TARGET}"
else
  echo "ℹ️ 제거할 doctree 전역 CLI 링크가 없습니다: ${TARGET}"
fi
