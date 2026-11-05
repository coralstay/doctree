#!/usr/bin/env bash
# doctree CLI 전역 설치 스크립트 (.venv 패키지 설치 + ~/.local/bin/doctree 심볼릭 링크 등록)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${REPO_DIR}/.venv"
BIN_DIR="${DOCTREE_BIN_DIR:-${HOME}/.local/bin}"

if [ ! -x "${VENV_DIR}/bin/python" ]; then
  echo "📦 가상환경(.venv)을 생성합니다: ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
fi

echo "🔧 doctree 패키지를 편집 가능 모드(-e)로 설치합니다..."
"${VENV_DIR}/bin/pip" install --quiet --no-build-isolation --no-deps -e "${REPO_DIR}" 2>/dev/null \
  || "${VENV_DIR}/bin/pip" install --quiet -e "${REPO_DIR}"

mkdir -p "${BIN_DIR}"
ln -sf "${VENV_DIR}/bin/doctree" "${BIN_DIR}/doctree"

echo "✅ doctree CLI 전역 설치 완료: ${BIN_DIR}/doctree ➔ ${VENV_DIR}/bin/doctree"
echo "💡 이제 가상환경 활성화 없이 어디서든 'doctree --help' 또는 'doctree help'를 실행할 수 있습니다."
