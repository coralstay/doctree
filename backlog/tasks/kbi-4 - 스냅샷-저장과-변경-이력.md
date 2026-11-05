---
id: KBI-4
title: 스냅샷 저장과 변경 이력
status: Done
assignee: []
created_date: '2026-10-21 09:37'
updated_date: '2026-10-27 15:58'
labels:
  - ledger
dependencies:
  - KBI-2
references:
  - decision-1
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
실행할 때마다 목록 스냅샷을 저장하고 직전 스냅샷과 비교해 변경 이력을 누적한다.

## 비즈니스 규칙
- B1 (AC #2) 같은 책 판정은 파일 ID 기준이다. 이름이나 경로가 바뀌어도 같은 책이다.
- B2 (AC #2) 이름 변경과 폴더 이동이 동시에 일어나면 두 변경을 모두 기록한다.
- B3 (AC #3) 변경 이력은 누적되며 기존 기록을 지우거나 고치지 않는다.
- B4 (AC #1) 비교 기준은 이번 실행 전에 존재하던 가장 최근 스냅샷이다(같은 날 재실행 포함).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 실행 시 ledger/snapshots/YYYY-MM-DD.json을 저장하고, 같은 날 재실행하면 덮어쓴다
- [x] #2 직전 스냅샷과 비교해 추가 · 삭제 · 이름 변경 · 폴더 이동 · 내용 변경(MD5)을 구분한다
- [x] #3 ledger/변경이력.md 맨 위에 실행 시각과 변경 블록을 추가하고, 변경이 없으면 '변경 없음'을 기록한다
- [x] #4 직전 스냅샷이 없으면 '최초 등록 N권'으로 기록한다
<!-- AC:END -->
