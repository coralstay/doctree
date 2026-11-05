---
name: invariant-grilling-and-fuzzing
description: >-
  Use this skill when the user asks to evaluate whether fuzzing is needed,
  identify high-ROI domain boundaries for property-based fuzzing (Hypothesis),
  or run a feature-freeze refactoring and grilling workflow grounded in
  Code Complete 2, Refactoring (2nd Ed.), and Good Code, Bad Code with
  100% line/branch coverage, C901 <= 7, and zero dead code.
---

# Feature-Freeze Invariant Grilling & Targeted Property-Based Fuzzing

기능 동결(Feature Freeze) 상태에서 무차별 바이너리 퍼징 대신 **고효율(High-ROI) 순수 도메인 경계**를 선별하여 `hypothesis` 속성 기반 퍼징(Property-Based Fuzzing)으로 적대적 입력을 주입하고, 《Code Complete 2》·《Refactoring 2판》·《Good Code, Bad Code》의 설계 불변 조건을 100% 달성할 때까지 그릴링(Grilling)과 방어적 리팩터링을 반복하는 표준 워크플로다.

---

## Phase 1. 퍼징 필요성 판단 및 경계 식별 사고 흐름 (Triage Framework)

프로젝트에 퍼징을 도입할 때는 먼저 **런타임 성격과 결함 표면(Attack/Failure Surface)**을 분석해 퍼징 방식을 결정한다.

1. **무차별 바이너리 퍼징(`Atheris` / `AFL++`)이 불필요한 영역**:
   - 메모리 안전 언어(순수 Python) 위에서 동작하며 외부 네트워크 API(`googleapiclient`, `urllib`)나 서브프로세스(`dot`)가 경계에서 격리·모킹되는 어댑터 계층.
2. **타깃형 속성 기반 퍼징(`Hypothesis`)이 필수적인 4대 핵심 도메인 경계**:
   - 수동 예제 기반 테스트(Example-Based Test)만으로는 조합 폭발을 다룰 수 없는 **순수 함수(Pure Functions) 및 상태 전이 경계**를 식별한다:
     - **경계 ① 비신뢰 바이트·스트림 파서 및 코드/도식 생성기**: 손상된 압축 스트림, 깨진 이스케이프 시퀀스, 불균형 괄호, 제어문자·특수문자가 섞인 입력.
     - **경계 ② 유니코드(NFC/NFD) 정규화 및 파일명·경로 살균기**: macOS(NFD 자모 분리) ⟷ Linux/Drive(NFC 완성형) 충돌, 제어문자, 경로 탈출(`..`, `.`, `/`, `\`, 빈 스템).
     - **경계 ③ 스냅샷·상태 Diff 엔진의 대수적 불변식**: 임의의 두 상태 간 비교에서 성립해야 하는 반사성·대칭성·상호 배타성·보존 법칙.
     - **경계 ④ 분류·집계 파이프라인의 집합 보존 및 순서 불변성**: 입력 레코드 치환(Permutation) 및 미등록 항목 혼입 시의 카운트 보존과 고정 트리 출력 순서 불변성.

---

## Phase 2. 기능 동결 및 아키텍처 불변 조건 (Architectural Invariants)

리팩터링과 버그 수정 전 과정에서 다음 3대 불변 조건을 절대 위반하지 않는다:

1. **외부 계약 동결**:
   - CLI 명령어·옵션 계약, 터미널 출력 포맷, `config/*.yaml` 스키마 및 `AGENTS.md` 도메인 규칙 변경 절대 금지.
   - `src/`와 `tests/` 간 1:1 모듈 대칭 구조 유지(별도 임시 테스트 파일을 만들지 않고 해당 도메인 모듈의 `test_<module>.py`에 통합).
2. **상태 불변성 및 메모리 슬롯 규율** (《Good Code, Bad Code》 Ch.4·6, 《Refactoring 2판》 Ch.9):
   - 전 모듈(`src/**/*.py`) 내 가변 전역 컬렉션(`list`, `dict`, `set`) **0건** 유지 (`MappingProxyType`, `tuple`, `frozenset`으로 불변화).
   - 모든 도메인·예외·헬퍼 클래스(`StrEnum`, `TypedDict` 제외)에 `__slots__` 또는 `@dataclass(frozen=True, slots=True)` **100% 적용**.
   - 원본 필드에서 계산 가능한 파생 상태는 슬롯에서 제거하고 `@property`로 전환.
3. **《Code Complete 2》 정량적 소프트웨어 메트릭 상한** (《Code Complete 2》 Ch.7·10·19·24, 《Refactoring 2판》 Ch.6.8):
   - **순환 복잡도(`C901`) 및 분기 수(`PLR0912`)**: 모든 프로덕션 함수 및 테스트 함수/헬퍼의 McCabe 순환 복잡도 **<= 7**, 분기 수 **<= 7** 유지.
   - **루틴 매개변수 개수(`PLR0913`)**: 모든 함수·메서드의 매개변수 개수 **<= 7** 유지 (초과 시 불변 `@dataclass(frozen=True, slots=True)` 매개변수 객체 도입 — *Introduce Parameter Object*).
   - **루틴 길이 및 문장 수(`PLR0915`)**: 모든 함수·메서드의 문장 수 **<= 50** 및 도메인 루틴 길이 **<= 65줄** 유지.
   - **제어문 중첩 깊이(Nesting Depth)**: 루프·조건문(`if`/`for`/`while`/`try`) 중첩 깊이 **<= 3** 유지 (보호절 `continue`/`return` 조기 반환으로 평탄화).
   - **변수 스팬(Span)·수명(Live Time) 및 결합도(Fan-Out)**: 지역 변수는 첫 사용 직전에 선언해 평균 스팬 **<= 2줄**, 평균 수명 **<= 10줄** 이내로 국소화하고, 모듈 간 `from doctree...` 임포트는 `runtime_env` 기반 계층으로만 단방향 수렴(교차 결합 **0건**).

---

## Phase 3. 4대 핵심 경계 속성 기반 퍼징(Hypothesis) 및 방어 프로토콜

각 경계에 대해 `hypothesis` 전략(`st.binary()`, `st.text()`, `st.lists()`, `st.permutations()`, `st.composite`)을 설계하여 그릴링하고, 반례(Falsifying Example)가 발견되면 즉시 프로덕션 코드를 수정한다.

### ① PDF 바이트·스트림 파서 및 DOT 생성기 (`pdf_threat_audit`)
- **검증 속성**:
  - 깨진 8진수 이스케이프(`\777`), 불균형 괄호, 손상된 `zlib` 스트림, 임의의 바이너리 바이트열이나 제어문자가 입력되어도 `IndexError`, `re.error`, `UnicodeDecodeError`, `ValueError` 없이 안전하게 처리될 것.
  - 임의의 문자열이 입력되어도 `build_dot_diagram`의 Graphviz `record` 구분자(`|`, `{`, `}`, `<`, `>`), 따옴표·백틱, C0/C1 제어문자(`0x00..0x1f`, `0x7f..0x9f` 예: `\x85` NEL) 및 유니코드 줄 구분자(`\u2028`, `\u2029`)가 이스케이프되어 레코드 필드와 단일 행 구조가 절대 깨지지 않을 것.

### ② 한글 유니코드(NFC/NFD) 및 파일명·경로 살균기 (`runtime_env`, `drive_sync`, `content_taxonomy`)
- **검증 속성**:
  - 임의의 유니코드(자모 분리 NFD, 결합 문자, C0/C1 제어문자, 유니코드 공백 `\u00a0`, 특수문자)에 대해 `normalize_nfc`, `_sanitize_pdf_filename`, `_slugify`의 **멱등성(`f(f(x)) == f(x)`)** 및 **NFD/NFC 동치성(`f(NFD(x)) == f(NFC(x))`)**을 보장할 것.
  - `_sanitize_pdf_filename`과 `_validate_safe_relative_path`가 어떤 경로 탈출 입력(`..`, `.`, `/`, `\`, 비출력 문자 `not isprintable()`, 공백/점/하이픈만으로 된 스템)도 허용하지 않고 타깃 디렉터리 내부만 가리킬 것.

### ③ 스냅샷 Diff 대수적 불변식 (`drive_sync/snapshot_diff_tracker.py`)
- **검증 속성**:
  - **반사성(Reflexivity)**: 임의의 유효 스냅샷 `S`(및 NFD 변형본)에 대해 `diff_snapshots(S, S).is_empty is True`.
  - **대칭성(Symmetry)**: `diff_snapshots(A, B)`와 `diff_snapshots(B, A)`의 `added`/`removed`가 정확히 역전되고, `renamed`·`moved`·`content_changed`의 `(id, old, new)`와 `(id, new, old)` 집합이 정확히 대칭을 이룰 것.
  - **상호 배타성(Mutual Exclusivity)**: `added` ID 집합, `removed` ID 집합, 속성 변경(`renamed`/`moved`/`content_changed`) ID 집합은 상호 배타적(교집합 공집합)이며 `len(A) + len(added) - len(removed) == len(B)`를 만족할 것.

### ④ 분류 트리 보존 법칙 (`content_taxonomy/category_classifier.py`)
- **검증 속성**:
  - 입력 레코드 순서 치환(Permutation) 및 임의의 미등록 파일 포함 시에도 `분류된 파일 수 합계 + 미분류 수 == 전체 입력 레코드 수` 보존 법칙을 만족할 것.
  - 레코드 순서가 어떻게 뒤섞여도 `category_tree.yaml` 대분야·세부분야 출력 순서와 `format_classification_summary` 렌더링 결과가 100% 동일할 것.

---

## Phase 4. 10-Pass 다차원 엔지니어링 프로토콜 및 적용 기법 총람

단순 정적 린트나 단일 패스 리팩터링을 넘어, 아래 **10-Pass 엔지니어링 기법**을 순차 적용해 아키텍처·보안·성능·테스트 오라클을 전방위로 검증한다.

| Pass | 엔지니어링 기법 (Engineering Technique) | 이론적 근거 및 대표 문헌 | 적용 대상 파일 (`src/doctree/` & `tests/`) | 해결한 결함·위험 및 보장 불변식 |
| :--- | :--- | :--- | :--- | :--- |
| **Pass 1** | **AST 기반 아키텍처 적합성 테스트 (Architectural Fitness Functions) & 계층형 의존성 단방향 강제 (Acyclic Directed Dependencies)** | 《Building Evolutionary Architectures》, 《Good Code, Bad Code》 Ch.2·4, 《Code Complete 2》 Ch.7.4·7.5·19 | `tests/runtime_env/test_workspace_settings.py`, `src/doctree/**/__init__.py` | `ast.parse`로 전 모듈을 정적 순회하여 ① `runtime_env` 최하위 기반 계층 역참조 금지, ② 도메인 패키지 간 교차 결합 금지, ③ `__all__` 공개 심볼 외 내부 구현 임포트 금지, ④ `src/doctree` ⟷ `tests/` 1:1 모듈 대칭, ⑤ 전 루틴 매개변수 개수 **<= 7**, 제어문 중첩 깊이 **<= 3**, 도메인 루틴 길이 **<= 65줄** 상한을 기계적으로 강제 |
| **Pass 2** | **불변 값 객체(Immutable Value Objects), 슬롯 메모리 레이아웃(`__slots__`) 및 파생 상태 제거 (Replace Derived Variable with Query)** | 《Refactoring 2판》 Ch.9, 《Good Code, Bad Code》 Ch.4·6 | `src/doctree/**/*.py` 전 도메인 데이터클래스 및 어댑터 클래스 (`PageVerificationRecord.status` 포함) | 가변 전역 컬렉션(`list`, `dict`, `set`) **0건**(`MappingProxyType`, `tuple`, `frozenset` 치환), 모든 클래스 `slots=True`/`__slots__` 100% 적용으로 런타임 속성 오타 주입 차단 및 계산 가능한 파생 상태(`@property`: `figures_detected`, `status`, `total_pages`, `passed_pages`, `warned_pages`, `katas_count`) 정규화 |
| **Pass 3** | **방어적 프로그래밍(Defensive Programming) & 유니코드 정준 동치(NFC Canonicalization) 정규화** | 《Code Complete 2》 Ch.8, Unicode Standard Annex #15 (UAX #15) | `runtime_env/workspace_settings.py`, `content_taxonomy/category_classifier.py`, `content_taxonomy/duplicate_and_naming_linter.py`, `drive_sync/workspace_sync_engine.py` | macOS(NFD 자모 분리) ⟷ Linux/Drive(NFC 완성형) 경계에서 `normalize_nfc`를 선행 적용하여 NFD/NFC 중복 `file_id` 우회 등록을 차단하고, `classify_records` 반환 시 `dict(record)` 방어적 복사로 호출자 사후 변경(Caller Mutation) 오염을 원천 봉쇄 |
| **Pass 4** | **중복 검증 로직 단일화 (Consolidate Duplicate Conditional Fragments), 매개변수 객체 도입 (Introduce Parameter Object) & 경계 데코레이터 평탄화** | 《Code Complete 2》 Ch.7·18·19, 《Refactoring 2판》 Ch.6.8·8·10 | `runtime_env/layout_profile_store.py`, `content_taxonomy/category_classifier.py`, `content_taxonomy/practice_kata_builder.py` (`_KataBuildContext`), `drive_sync/workspace_sync_engine.py`, `doctree_cli/command_dispatcher.py` | `validate_unique_item`·`validate_mapping_keys`·`validate_nonempty_str` 공통 검증기로 스키마/ID 중복 검사를 단일화하고, `_KataBuildContext` 불변 매개변수 객체 도입 및 `_boundary_guarded` 데코레이터·`topic_parsers` 라우팅으로 모든 함수 `C901 <= 7`, `PLR0912 (max-branches <= 7)`, `PLR0913 (max-args <= 7)`, `PLR0915 (max-statements <= 50)` 100% 달성 |
| **Pass 5** | **비신뢰 입력 경계 하드닝 (Untrusted Input Hardening): 압축 폭탄(Decompression Bomb)·메모리 고갈·터미널/코드 인젝션 방어** | OWASP Secure Coding Practices, CERT Secure Coding, 《Good Code, Bad Code》 Ch.5 | `pdf_threat_audit/pdf_page_ingestor.py`, `runtime_env/boundary_errors.py`, `runtime_env/layout_profile_store.py`, `doctree_cli/terminal_tree_renderer.py`, `content_taxonomy/practice_kata_builder.py` | ① `_extract_stream_payload`에서 `zlib.decompressobj().decompress(raw, 16 MiB)` 상한으로 PDF `FlateDecode` 압축 폭탄(Zip Bomb) 메모리 고갈 차단, ② `execute_json_http_get` 응답 본문 `10 MiB` 상한 적용, ③ `_UniqueKeySafeLoader`로 YAML 역직렬화 공격 및 중복 키 덮어쓰기 차단, ④ C0/C1(`0x85` NEL 포함) 및 유니코드 포맷 제어문자(`Cf`) 제거로 터미널 ANSI/코드/DOT 인젝션 차단 |
| **Pass 6** | **POSIX 원자적 파일 교체 (Atomic File Replacement via Write-Fsync-Rename) & 비동기 시그널 크래시 안전성 (`BaseException` Cleanup)** | POSIX `rename(2)` Atomicity, 《Code Complete 2》 Ch.8 (Exception Safety) | `runtime_env/workspace_settings.py` (`atomic_write_bytes`, `atomic_write_text`), `drive_sync/snapshot_diff_tracker.py`, `runtime_env/layout_profile_store.py`, `pdf_threat_audit/threat_intel_client.py`, `drive_sync/workspace_sync_engine.py`, `runtime_env/project_scaffolder.py`, `content_taxonomy/practice_kata_builder.py` | 임시 파일(`.tmp`)에 기록 후 `os.fsync` 완료 뒤에만 `Path.replace`로 원자적 교체를 수행하고, `except BaseException:` 블록에서 `KeyboardInterrupt`·`SystemExit`·`OSError` 발생 시에도 고아 `.tmp` 파일을 즉시 삭제하며 기존 정상 스냅샷/설정/캐시/카타 파일을 100% 보존 |
| **Pass 7** | **데드 경로 및 정규식 재컴파일 핫스팟 제거 (Hotspot Precompilation & Subprocess Timeout Isolation)** | 《Code Complete 2》 Ch.25·26, 《Refactoring 2판》 Ch.8 | `pdf_threat_audit/pdf_page_ingestor.py`, `pdf_threat_audit/pdf_byte_inspector.py`, `drive_sync/workspace_sync_engine.py` | 정규식(`_DOT_GRAPH_KEYWORD_RE`, `DANGEROUS_PDF_TAGS` 등)을 모듈 로드 시 1회 사전 컴파일(`Final`)하고, `inspect_pdf_bytes`에서 1024바이트 헤더·64KiB 꼬리 창 슬라이싱 및 `(size, mtime_ns)` 로컬 해시 캐시로 불필요한 디스크 I/O 제거, `subprocess.SubprocessError`(`TimeoutExpired`) 안전 폴백 보장 |
| **Pass 8** | **상태 기계 기반 속성 퍼징 (Stateful Model-Based Property Testing via `RuleBasedStateMachine`)** | QuickCheck / Hypothesis Stateful Testing, 《Good Code, Bad Code》 Ch.10 | `tests/runtime_env/test_layout_profile_store.py` (`LayoutProfileStoreStateMachine`), `tests/drive_sync/test_snapshot_diff_tracker.py` (`SnapshotTimelineStateMachine`), `tests/drive_sync/test_workspace_sync_engine.py` (`WorkspaceSyncStateMachine`) | 단일 함수 호출이 아닌 임의의 연산 시퀀스(`set`/`add`/`remove`/`load`, 다일차 `record_run`/`diff_snapshots`/쓰기 장애 주입, 로컬 스캔 캐시·`pull_drive_pdf_to_project`·3-Way 비교)를 상태 기계로 구동하며 인메모리 참조 모델(Oracle)과 디스크 상태·불변식을 매 단계 대조 검증 |
| **Pass 9** | **메타모픽 테스팅 (Metamorphic Testing Relations)** | Tsong Yueh Chen et al. (*Metamorphic Testing: A New Approach for Generating Next Test Cases*) | `tests/content_taxonomy/test_category_classifier.py`, `tests/content_taxonomy/test_duplicate_and_naming_linter.py`, `tests/content_taxonomy/test_practice_kata_builder.py`, `tests/drive_sync/test_workspace_sync_engine.py`, `tests/pdf_threat_audit/test_pdf_byte_inspector.py`, `tests/pdf_threat_audit/test_pdf_page_ingestor.py` | 입력 변환에 따른 출력 변화 관계를 수학적으로 검증: ① 미분류 `file_id` 배정 승격 시 `unclassified` 정확히 1 감소, ② 동일 SHA-256 복제 시 `EXACT_DUPLICATE`만 증가하고 `SAME_TITLE_DIFFERENT_CONTENT` 불변, ③ `drive_only` 해시 로컬 승격 시 `matched`로 이동, ④ PDF 주석 패딩 및 마크다운 장식 요소(`#`, `>`, `!`, `<!--`, ```` ``` ````, 빈 줄) 추가 시 추출 결과 불변, ⑤ 마크다운 숫자/토큰 삭제 시 5대 검증 `PASS` → `WARN` 전이 |
| **Pass 10** | **뮤테이션 분석 및 경계값 그릴링 (Mutation Analysis & Off-by-One Boundary-Value Grilling)** | DeMillo, Lipton & Sayward (*Hints on Test Data Selection*), 《Code Complete 2》 Ch.22 | `tests/pdf_threat_audit/test_pdf_byte_inspector.py`, `tests/doctree_cli/test_terminal_tree_renderer.py`, `tests/drive_sync/test_workspace_sync_engine.py` | 관계 연산자(`<` vs `<=`) 및 창 크기 뮤테이션을 사살하기 위해 7B/8B 파일 크기, 1024B 헤더 창 경계(offset 1019 vs 1020), 65536B 꼬리 창 경계, `print_account_dashboard` 스토리지 게이지 바(0%, 4.9%, 5.0%, 100%, 125%), `resolve_drive_record_for_pull` 쿼리 해석 4단계 우선순위(`id` > `sha256` > 정확 이름 > 부분 이름) 충돌 케이스를 정밀 타격 |

---

## Phase 5. 최종 품질 게이트 (Hard Quality Gate)

모든 수정 후 반드시 아래 3대 검증 명령을 실행하여 단 1건의 경고나 미달도 없음을 확인한다:

```bash
# 1. 정적 린트, 순환 복잡도(C901 <= 7) 및 루틴 메트릭 상한(PLR0912/PLR0913 <= 7, PLR0915 <= 50) 전 규칙 검사
.venv/bin/ruff check src tests

# 2. 미사용 데드 코드·슬롯 검사 (@given, @rule, @invariant, @initialize 데코레이터 허용 확인)
.venv/bin/vulture

# 3. Hypothesis 속성·상태 기계·메타모픽 퍼징 포함 전체 테스트 및 라인·분기 커버리지 100.00% 검증
.venv/bin/pytest --cov=doctree --cov-branch --cov-report=term-missing
```

