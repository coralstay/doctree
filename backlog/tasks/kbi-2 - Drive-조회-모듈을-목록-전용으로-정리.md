---
id: KBI-2
title: Drive 조회 모듈을 목록 전용으로 정리
status: Done
assignee: []
created_date: '2026-10-21 09:37'
updated_date: '2026-10-27 15:58'
labels:
  - drive
dependencies:
  - KBI-1
references:
  - decision-1
  - decision-2
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
대장에 필요한 메타데이터만 반환하도록 google_drive 모듈을 줄이고, 파일명에 '/'가 들어간 경우의 경로 깨짐을 고친다.

## 비즈니스 규칙
- B1 (AC #3) 경로는 '폴더 경로'와 '파일명'으로 따로 보존하며, 파일명 속 '/'를 폴더 구분자로 해석하지 않는다.
- B2 (AC #5) 확장자가 없어도 mimeType이 application/pdf면 도서로 본다.
- B3 (AC #4) 휴지통(trashed) 파일은 목록에서 제외한다.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 목록 레코드가 id · name · folder_path · size_bytes · md5 · modified_time · web_view_link를 갖는다
- [x] #2 다운로드 · Changes API · 로컬 MD5 대조 코드를 제거한다
- [x] #3 파일명에 '/'가 포함돼도(예: A/B-테스트) folder_path와 name이 올바르게 분리된다
- [x] #4 GOOGLE_DRIVE_FOLDER_ID가 지정되면 그 폴더의 하위 트리만, 비어 있으면 드라이브 전체를 조회한다
- [x] #5 mimeType이 application/pdf이거나 이름이 .pdf로 끝나는 파일만 도서로 수집한다
<!-- AC:END -->
