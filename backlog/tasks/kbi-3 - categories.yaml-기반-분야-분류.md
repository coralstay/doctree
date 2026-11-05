---
id: KBI-3
title: categories.yaml 기반 분야 분류
status: Done
assignee: []
created_date: '2026-10-21 09:37'
updated_date: '2026-10-22 16:22'
labels:
  - classify
dependencies: []
references:
  - decision-1
  - decision-3
documentation:
  - 'local:ledger/도서목록.md (2026-10-22 원본)'
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
현재 만든 16분야·55세부분야 체계를 고정 분류표로 이관한다. 분류표와 파일 ID별 배정표를 분리하고,
로컬 스냅샷 검토 명령에서 검증한다. 2026-10-22 사용자 요청에 따라 제목 기반 매핑 대신 Drive 파일 ID를 사용한다.
Drive 조회 모듈 정리(KBI-2)와 독립적으로 기존 스냅샷의 id/name을 입력받는다.

## 비즈니스 규칙
- B1 (AC #4) 파일 ID가 같으면 폴더 이동·파일명 변경에도 배정이 유지된다. 제목은 설명이며 자동 배정에 사용하지 않는다.
- B2 (AC #3) 미등록 파일도 검토 보고서에서 빠지지 않고 별도 '미분류·검토 대기'에 들어간다. 엄격 모드에서는 미분류가 있으면 출력 교체 전에 중단한다.
- B3 (AC #1) 한 파일 ID는 정확히 하나의 등록된 세부분야에만 배정한다. 중복 배정, 중복 키·노드 ID, 없는 세부분야 또는 대분야 배정은 실행 오류다.
- B4 (AC #5) 분류 트리 이름·순서·ID는 설정이 기준이며, 빈 분야도 유지한다. 체계 변경은 명시적 요청이 있을 때만 한다.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 config/categories.yaml의 고정 분류표와 config/book_categories.yaml의 파일 ID별 배정을 엄격 검증해 로드한다
- [x] #2 기존 16분야·55세부분야와 234개 파일의 배정을 원본 그대로 이관하고 최신 로컬 스냅샷에서 미분류 0을 확인한다
- [x] #3 미등록 파일은 별도 검토 대기에 표시하고 --strict에서는 실패하며 기존 출력을 보존한다
- [x] #4 폴더 이동·파일명 변경, 동일 제목의 서로 다른 파일에도 ID별 배정을 유지한다
- [x] #5 doctree classify 보고서가 설정 순서와 빈 분야를 유지하며 AGENTS.md에 변경 규칙을 명시한다
<!-- AC:END -->

## Final Summary

고정 분류표·배정표와 검증기, 로컬 스냅샷 기반 `doctree classify` 및 원자적 보고서 저장을 추가했다.
전체 대장 생성과 Drive 조회 연계는 KBI-6에서 이 검증기를 사용한다.
