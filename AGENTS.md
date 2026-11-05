# 콘텐츠(contents) 분류 및 스냅샷 규칙

- `config/category_tree.yaml`이 분야·세부분야 이름, 고정 ID, 출력 순서의 유일한 기준이다. 사용자가 분류 체계 변경을 명시적으로 요청하지 않으면 노드를 추가·삭제·이동하거나 이름·ID·순서를 변경하지 않는다.
- 콘텐츠 추가와 분류 배정은 `config/content_category_map.yaml`에서 관리한다. `file_id`는 Google Drive 파일 ID이고 `category_id`는 등록된 세부분야 ID여야 한다. `title`은 사람이 읽기 위한 설명이며 배정 키가 아니다.
- 프로젝트 유형별(`handcrafted`, `toolchain`) 디렉터리·필수 파일·gitignore 구조 규격은 `config/project_layout_profiles.yaml`에서 관리하며, 다른 컴포넌트들은 `doctree layout get|set|add|remove` 명령으로 입출력한다.
- Drive 폴더·파일명·AI 추측으로 기존 배정을 바꾸지 않는다. AI 분류 추천도 등록된 세부분야 ID 중에서만 제시한다. 미등록 파일은 검토 대기로 남기고 임의로 새 분야를 만들지 않는다.
- 분류는 `doctree.content_taxonomy.category_classifier.load_catalog`와 `classify_records`를 거친다. 중복 키·노드 ID·파일 배정, 없는 세부분야 및 대분야 직접 배정은 오류다. 폴더 기반 분류나 별도 분류표를 구현하지 않는다.
- 터미널 출력은 YAML 목록 순서와 빈 분야·세부분야를 유지한다. 미분류 목록은 정식 분류 트리 밖에 표시한다.
- 엄격 모드(`--strict`)에서는 미분류가 하나라도 있으면 실패하고 스냅샷 저장을 중단한다. 검증에 실패한 경우 기존 스냅샷을 보존한다.
- 파일로 저장하는 산출물은 `snapshots/YYYY-MM-DD.json` 스냅샷뿐이며, 별도의 마크다운 대장(`도서목록.md`, `변경이력.md`, `분류검토.md`)이나 `ledger/` 디렉터리는 두지 않는다. 테스트에서는 실제 인증 파일·토큰·Drive API를 사용하지 않는다.
