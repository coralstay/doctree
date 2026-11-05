---
id: KBI-1
title: 파이프라인 컴포넌트 삭제 및 doctree로 이름 변경
status: Done
assignee: []
created_date: '2026-10-21 09:36'
updated_date: '2026-10-21 09:51'
labels:
  - cleanup
dependencies: []
references:
  - decision-1
  - decision-2
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
PDF→Markdown 변환 파이프라인을 폐기하고 도서 목록 대장 CLI로 전환하기 위해, 쓰지 않는 컴포넌트와 의존성을 걷어내고 패키지/CLI 이름을 바꾼다.

## 비즈니스 규칙
- B1 (AC #3) 인증은 기존 credentials.json / token.json 위치·방식을 그대로 써서 재로그인이 필요 없다.
- B2 (AC #4) 대장 산출물(ledger/)은 저장소에 커밋되지 않는다.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 src/converter.py · validator.py · summarizer.py · auth_login.py · main.py와 관련 테스트를 삭제하고, settings의 data/ 단계 폴더 설정을 제거한다
- [x] #2 pdfplumber · langdetect · openai · httpx · aiofiles · pytest-asyncio 의존성과 .env.example/settings의 OpenAI·유사도 항목을 제거한다
- [x] #3 패키지명은 doctree, CLI는 doctree이며 auth · status 명령이 동작하고 기존 gdrive-md/book2md/pdf2md 진입점은 없다
- [x] #4 .gitignore에 ledger/가 추가돼 있다
- [x] #5 남은 테스트가 모두 통과한다
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
파이프라인(converter·validator·summarizer·auth_login·main)과 해당 테스트를 삭제하고, 변환·요약 의존성 6개와 OpenAI·유사도 설정을 제거했다. 패키지/CLI 이름을 doctree로 바꾸고 auth·status 두 명령만 남겼으며(src/cli.py 재작성), ledger/를 .gitignore에 추가했다. 검증: pytest 33 passed, 'doctree status'가 기존 token.json으로 재로그인 없이 실제 Drive 계정·스토리지(62.41 GB / 5120 GB)를 출력. GitHub 저장소도 coralstay/doctree(private)로 이름 변경. 남은 것: google_drive.py의 다운로드·Changes API 코드는 KBI-2, ui.py의 미사용 print_drive_tree·import 정리는 KBI-2/6에서 처리. 로컬 .env의 OPENAI_*·SIMILARITY_THRESHOLD 줄은 훅이 접근을 막아 남아 있으나 더 이상 읽히지 않음.
<!-- SECTION:FINAL_SUMMARY:END -->
