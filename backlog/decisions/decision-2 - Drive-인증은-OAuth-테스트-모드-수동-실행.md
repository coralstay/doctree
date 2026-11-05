---
id: decision-2
title: Drive 인증은 OAuth 테스트 모드 + 수동 실행
date: '2026-10-21 09:39'
status: accepted
---
## Context

Drive 접근에는 OAuth 데스크톱 클라이언트(GCP 프로젝트 BookNotes)를 쓴다. 동의 화면을 프로덕션으로 게시하려면 Branding에 홈페이지·개인정보처리방침 URL이 필요해서 개인 용도로는 부담이 크다. 테스트 모드에서는 갱신 토큰이 7일 뒤 만료된다. Claude 전용 Google Drive 커넥터는 프로그램이 쓸 수 없어서 제외했다.

## Decision

OAuth 동의 화면은 테스트 모드로 두고 본인 계정을 테스트 유저로 등록한다. `doctree update`는 사람이 직접 실행하며, 주기 자동 실행은 하지 않는다. 범위는 `drive.readonly`만 쓴다.

## Consequences

- 토큰이 7일마다 만료되므로 `token.json`을 지우고 `doctree auth`로 다시 로그인해야 한다.
- 무인 주기 실행이 필요해지면 서비스 계정(또는 프로덕션 게시)으로 바꾸는 별도 결정이 필요하다.
- `credentials.json`과 `token.json`은 `.gitignore`로 커밋에서 제외한다.
