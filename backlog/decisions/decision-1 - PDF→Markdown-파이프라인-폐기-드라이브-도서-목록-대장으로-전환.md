---
id: decision-1
title: 'PDF→Markdown 파이프라인 폐기, 드라이브 도서 목록 대장으로 전환'
date: '2026-10-21 09:39'
status: accepted
---
## Context

도서 PDF를 구글 드라이브에서 받아 Marker/MinerU로 변환하고, 유사도 검증과 LLM 요약을 거쳐 Markdown으로 만드는 5단계 파이프라인을 만들어 두었다. 그러나 드라이브의 도서는 약 233권, 67 GB이고, 실제로 필요한 것은 "어떤 책이 어디에 얼마나 있고 어느 분야인지"를 관리하는 목록이었다. 2026-10-21에 목록을 조회해 분야 분류 md를 수동으로 만들어 보니 이 결과물만으로도 충분히 쓸모가 있었다.

## Decision

변환·검증·요약 파이프라인(converter, validator, summarizer, OpenAI 연동)을 삭제한다. 프로젝트를 Drive 메타데이터만 읽어 도서 목록 대장(제목·용량·드라이브 경로·링크·분야)을 만드는 CLI `doctree`로 다시 쓴다. 저장소 이름은 `doctree`(GitHub private)로 바꾼다. 함께 관리할 항목은 변경 이력(스냅샷 비교), 분야 분류, 중복 감지, 파일명 점검, 스토리지 현황이다. 독서 상태와 본문 요약은 범위에서 뺀다.

이름은 knowledge-base-index → booktree를 거쳐 2026-10-21 `doctree`로 확정했다(PDF 외 문서로 넓혀도 어울리는 이름).

## Consequences

- PDF를 내려받지 않으므로 실행이 빠르고 OpenAI 비용이 들지 않는다.
- 책 내용 요약, 페이지 수처럼 본문이 필요한 정보는 대장에 없다.
- 대장(`ledger/`)에는 개인 드라이브의 구조가 담기므로 git에 커밋하지 않는다.
