#!/usr/bin/env bash
# ---
# title: install
# description: 'doctree CLI 전역 설치 스크립트: .venv에 패키지를 설치하고 ~/.local/bin/doctree 링크를 등록한다'
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${REPO_DIR}/.venv"  # ---- 1
BIN_DIR="${DOCTREE_BIN_DIR:-${HOME}/.local/bin}"

if [ ! -x "${VENV_DIR}/bin/python" ]; then
  echo "📦 가상환경(.venv)을 생성합니다: ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
fi

echo "🔧 doctree 패키지를 편집 가능 모드(-e)로 설치합니다..."
"${VENV_DIR}/bin/pip" install --quiet --no-build-isolation --no-deps -e "${REPO_DIR}" 2>/dev/null \
  || "${VENV_DIR}/bin/pip" install --quiet -e "${REPO_DIR}"  # ---- 2

mkdir -p "${BIN_DIR}"
ln -sf "${VENV_DIR}/bin/doctree" "${BIN_DIR}/doctree"  # ---- 3

echo "✅ doctree CLI 전역 설치 완료: ${BIN_DIR}/doctree ➔ ${VENV_DIR}/bin/doctree"
echo "💡 이제 가상환경 활성화 없이 어디서든 'doctree --help' 또는 'doctree help'를 실행할 수 있습니다."


# ======== 노트 ========
# 1 가상환경 위치
#   설명: 설치는 저장소 안 .venv에 한정된다. 전역 파이썬 환경을 건드리지 않는다.
#   관련: .gitignore
# 2 편집 가능 설치와 폴백
#   설명: 먼저 빌드 격리와 의존성 설치 없이(--no-build-isolation --no-deps) 시도하고, 실패하면 일반 설치로 넘어간다(`||`). 편집 모드(-e)라 소스를 고치면 재설치 없이 반영된다.
#   개념: editable install
# 3 전역 링크 등록
#   설명: ~/.local/bin/doctree(또는 DOCTREE_BIN_DIR)에 심볼릭 링크를 만들어 가상환경 활성화 없이 실행하게 한다. -f라 기존 링크를 덮어쓰므로 여러 번 실행해도 같은 결과다.
#   관련: uninstall.sh
#   개념: 멱등
