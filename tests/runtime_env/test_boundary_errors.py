# ---
# title: test_boundary_errors
# description: 경계 예외 계층과 시간대·유니코드·터미널 폭 등 추가 경계의 테스트
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""전 경계면 통합 예외 계층(boundary_errors) 및 추가 5대 경계(⑤ 시간대 · ⑥ 유니코드 NFD/NFC · ⑦ 런타임/패키징 · ⑧ 실제 저장소 데이터 계약 · ⑨ 터미널 폭) 검증."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import unicodedata
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error as urllib_error

import pytest
import yaml
from hypothesis import example, given, settings
from hypothesis import strategies as st
from rich.console import Console

from doctree.content_taxonomy.category_classifier import classify_records, load_catalog
from doctree.content_taxonomy.duplicate_and_naming_linter import (
    WarningKind,
    find_filename_issues,
    find_same_title_different_content,
)
from doctree.doctree_cli import command_dispatcher as cli
from doctree.drive_sync.snapshot_diff_tracker import (
    KST,
    SnapshotStorageError,
    diff_snapshots,
    load_latest_snapshot,
    save_snapshot,
    snapshot_date,
)
from doctree.pdf_threat_audit.threat_intel_client import audit_local_pdfs
from doctree.runtime_env.boundary_errors import (
    AUTH_MISSING_MESSAGE,
    BOUNDARY_TO_EXTERNAL,
    SNAPSHOT_PRESERVED_NOTICE,
    BoundaryKind,
    ClassificationError,
    DoctreeBoundaryError,
    DriveAuthError,
    DriveListError,
    ExternalBoundary,
    SecurityAuditError,
    coerce_boundary_error,
    execute_json_http_get,
)
from doctree.runtime_env.workspace_settings import BASE_DIR, WorkspaceSettings

ROOT = Path(__file__).resolve().parents[2]


class _FakeHttpResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ==============================================================================
# 1. 통합 경계 예외 계층 및 일관된 에러 메시지 포매터 검증
# ==============================================================================


def test_unified_boundary_error_hierarchy_and_consistent_formatting():
    """모든 경계 예외가 DoctreeBoundaryError와 표준 파이썬 예외(ValueError/RuntimeError/OSError)를 다중 상속하고 일관된 한국어 메시지를 만든다."""
    assert set(ExternalBoundary) == {
        ExternalBoundary.PROCESS_TERMINAL,
        ExternalBoundary.NETWORK_API,
        ExternalBoundary.FILESYSTEM,
        ExternalBoundary.SYSTEM_CLOCK,
    }
    assert set(BOUNDARY_TO_EXTERNAL.keys()) == set(BoundaryKind)

    auth_err = DriveAuthError()
    assert isinstance(auth_err, (DoctreeBoundaryError, RuntimeError))
    assert auth_err.boundary == BoundaryKind.AUTH
    assert auth_err.external_boundary == ExternalBoundary.NETWORK_API
    assert auth_err.format_error() == AUTH_MISSING_MESSAGE

    drive_err = DriveListError("네트워크 타임아웃", preserve_snapshot=True)
    assert isinstance(drive_err, (DoctreeBoundaryError, RuntimeError))
    assert drive_err.boundary == BoundaryKind.DRIVE_API
    assert drive_err.external_boundary == ExternalBoundary.NETWORK_API
    assert drive_err.preserve_snapshot is True
    assert drive_err.format_error() == "드라이브 목록 조회 실패: 네트워크 타임아웃"
    # 이미 접두어가 붙어 있는 경우 중복 접두어를 붙이지 않는다 (멱등성)
    already_prefixed = DriveListError("드라이브 목록 조회 실패: 중복 방지")
    assert already_prefixed.format_error() == "드라이브 목록 조회 실패: 중복 방지"

    cls_err = ClassificationError("미분류 콘텐츠 1권: 'b1'")
    assert isinstance(cls_err, (DoctreeBoundaryError, ValueError))
    assert cls_err.boundary == BoundaryKind.TAXONOMY
    assert cls_err.external_boundary == ExternalBoundary.FILESYSTEM
    assert cls_err.format_error() == "분류 검증 실패: 미분류 콘텐츠 1권: 'b1'"

    fs_err = SnapshotStorageError("권한 거부")
    assert isinstance(fs_err, (DoctreeBoundaryError, OSError))
    assert fs_err.boundary == BoundaryKind.FILESYSTEM
    assert fs_err.external_boundary == ExternalBoundary.FILESYSTEM
    assert fs_err.format_error() == "파일 입출력 실패: 권한 거부"

    sec_err = SecurityAuditError("VirusTotal 위협 감지 1건: bad.pdf", preserve_snapshot=True)
    assert isinstance(sec_err, (DoctreeBoundaryError, RuntimeError))
    assert sec_err.boundary == BoundaryKind.SECURITY_AUDIT
    assert sec_err.external_boundary == ExternalBoundary.FILESYSTEM
    assert sec_err.format_error() == "VirusTotal 위협 감지 1건: bad.pdf"

    ti_err = DoctreeBoundaryError("HTTP 429 쿼터 초과", boundary=BoundaryKind.THREAT_INTEL_API)
    assert ti_err.external_boundary == ExternalBoundary.NETWORK_API
    assert ti_err.format_error() == "위협 인텔리전스 API 요청 실패: HTTP 429 쿼터 초과"


def test_coerce_and_format_boundary_error_from_stdlib_exceptions():
    """JSONDecodeError, OSError, UnicodeError 및 일반 예외가 DoctreeBoundaryError로 일관되게 승격된다."""
    # 1) 기존 DoctreeBoundaryError에 preserve_snapshot=True 승격
    existing = ClassificationError("스키마 오류", preserve_snapshot=False)
    upgraded = coerce_boundary_error(existing, preserve_snapshot=True)
    assert upgraded is existing
    assert upgraded.preserve_snapshot is True

    # 2) JSONDecodeError -> ClassificationError (TAXONOMY)
    try:
        json.loads("{broken json")
    except json.JSONDecodeError as jde:
        coerced_json = coerce_boundary_error(jde, preserve_snapshot=True)
        assert isinstance(coerced_json, ClassificationError)
        assert coerced_json.preserve_snapshot is True
        assert "분류 검증 실패: JSON 파싱 오류:" in coerced_json.format_error()

    # 3) OSError / UnicodeError -> 지정한 default_boundary
    os_err = PermissionError("디스크 쓰기 권한 없음")
    assert coerce_boundary_error(os_err, default_boundary=BoundaryKind.FILESYSTEM).format_error() == (
        "파일 입출력 실패: 디스크 쓰기 권한 없음"
    )

    # 4) 그 외 일반 예외(RuntimeError 등)
    raw_err = RuntimeError("알 수 없는 런타임 오류")
    assert coerce_boundary_error(raw_err, default_boundary=BoundaryKind.DRIVE_API).format_error() == (
        "드라이브 목록 조회 실패: 알 수 없는 런타임 오류"
    )


def test_execute_json_http_get_unified_web_boundary_handler():
    """외부 웹 API GET 헬퍼가 정상 dict, 비-dict JSON 배열 오류, 404, 503, 소켓 예외를 일관되게 처리한다."""
    # 1) 200 정상 JSON 객체
    body, code, detail = execute_json_http_get(
        "https://example.com/api",
        {"Accept": "application/json"},
        service_name="TestAPI",
        urlopen=lambda _req, timeout=10.0: _FakeHttpResp(b'{"ok": true}') if timeout else _FakeHttpResp(b"{}"),
    )
    assert (body, code, detail) == ({"ok": True}, 200, "")

    # 2) 200이지만 JSON 배열(비객체) 응답인 경우
    body_arr, code_arr, detail_arr = execute_json_http_get(
        "https://example.com/api",
        {},
        service_name="TestAPI",
        urlopen=lambda _req, timeout=10.0: _FakeHttpResp(b"[1, 2, 3]") if timeout else _FakeHttpResp(b"[]"),
    )
    assert body_arr is None
    assert code_arr is None
    assert "TestAPI 응답 형식 오류: JSON 객체가 아닙니다." in detail_arr

    # 3) HTTP 404 미등록
    def _raise_404(_req, timeout=10.0):
        assert timeout > 0
        raise urllib_error.HTTPError("https://example.com", 404, "Not Found", {}, None)

    assert execute_json_http_get("https://example.com", {}, service_name="TestAPI", urlopen=_raise_404) == (
        None,
        404,
        "TestAPI 미등록 해시",
    )

    # 4) HTTP 503 서버 오류
    def _raise_503(_req, timeout=10.0):
        assert timeout > 0
        raise urllib_error.HTTPError("https://example.com", 503, "Unavailable", {}, None)

    assert execute_json_http_get("https://example.com", {}, service_name="TestAPI", urlopen=_raise_503) == (
        None,
        503,
        "HTTP 503",
    )

    # 5) 네트워크 연결 거부 예외
    def _raise_net(_req, timeout=10.0):
        assert timeout > 0
        raise OSError("connection refused")

    assert execute_json_http_get("https://example.com", {}, service_name="TestAPI", urlopen=_raise_net) == (
        None,
        None,
        "connection refused",
    )

    # 6) 비-HTTP/HTTPS URL 스킴(file:// 등) 차단
    body_scheme, code_scheme, detail_scheme = execute_json_http_get(
        "file:///etc/passwd",
        {},
        service_name="TestAPI",
    )
    assert (body_scheme, code_scheme) == (None, None)
    assert "지원하지 않는 URL 스킴입니다" in detail_scheme


def test_cli_commands_handle_corrupt_snapshot_and_audit_boundary_errors_cleanly(tmp_path):
    """cmd_audit와 cmd_update, cmd_classify가 손상된 스냅샷 파일이나 누락된 경로에서도 스택트레이스 없이 일관된 에러 메시지를 출력하고 종료 코드 1을 반환한다."""
    snaps_dir = tmp_path / "snapshots"
    snaps_dir.mkdir()
    corrupt_snap = snaps_dir / "2026-10-20.json"
    corrupt_snap.write_text("{corrupt json", encoding="utf-8")

    # 1) load_latest_snapshot이 손상된 JSON에서 SnapshotStorageError를 발생시키는지 확인
    with pytest.raises(SnapshotStorageError) as exc_info:
        load_latest_snapshot(snaps_dir)
    assert "스냅샷 파일을 읽을 수 없습니다" in str(exc_info.value)

    # 2) 스냅샷 JSON이 배열([])이어서 records 키가 없는 경우 SnapshotStorageError 발생 확인
    corrupt_snap.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SnapshotStorageError) as exc_info2:
        load_latest_snapshot(snaps_dir)
    assert "유효한 records 배열이 없습니다" in str(exc_info2.value)

    # 3) cmd_audit에서 손상된 최신 스냅샷 또는 존재하지 않는 --snapshot 지정 시 스택트레이스 없이 종료 코드 1 반환
    buf = io.StringIO()
    with patch.object(cli, "console", Console(file=buf, force_terminal= False, color_system=None)):
        code_audit_corrupt = cli.main(["audit", "--snapshots-dir", str(snaps_dir)])
        code_audit_missing = cli.main(["audit", "--snapshot", str(tmp_path / "no_such.json")])
    assert code_audit_corrupt == 1
    assert code_audit_missing == 1
    out = buf.getvalue()
    assert "❌ 파일 입출력 실패:" in out
    assert SNAPSHOT_PRESERVED_NOTICE not in out


# ==============================================================================
# 2. 추가 5대 경계면(Boundaries ⑤~⑨) 검증
# ==============================================================================


@pytest.mark.integration
def test_boundary_5_utc_to_kst_midnight_rollover_and_timezones(tmp_path):
    """[경계 ⑤ 시간·타임존 경계] UTC 14:59:59Z(KST 23:59:59)와 1초 뒤인 UTC 15:00:00Z(KST 익일 00:00:00)에서 스냅샷 날짜가 정확히 KST 자정 기준으로 롤오버된다."""
    snaps = tmp_path / "snapshots"
    rec = [{"id": "b1", "name": "종만북.pdf", "folder_path": "CS", "size_bytes": 10, "sha256": "s1"}]

    # UTC 2026-10-26 14:59:59Z == KST 2026-10-26 23:59:59+09:00 -> 2026-10-26.json
    utc_before_kst_midnight = datetime(2026, 10, 26, 14, 59, 59, tzinfo=UTC)
    assert snapshot_date(utc_before_kst_midnight) == date(2026, 10, 26)
    p1 = save_snapshot(rec, snaps, utc_before_kst_midnight)
    assert p1.name == "2026-10-26.json"

    # 1초 뒤: UTC 2026-10-26 15:00:00Z == KST 2026-10-27 00:00:00+09:00 -> 2026-10-27.json!
    utc_at_kst_midnight = datetime(2026, 10, 26, 15, 0, 0, tzinfo=UTC)
    assert snapshot_date(utc_at_kst_midnight) == date(2026, 10, 27)
    p2 = save_snapshot(rec, snaps, utc_at_kst_midnight)
    assert p2.name == "2026-10-27.json"

    # 미국 동부 시간(UTC-5) 2026-10-26 10:30:00-05:00 == KST 2026-10-27 00:30:00+09:00 -> 2026-10-27
    est = timezone(timedelta(hours=-5))
    est_time = datetime(2026, 10, 26, 10, 30, 0, tzinfo=est)
    assert snapshot_date(est_time) == date(2026, 10, 27)

    # cli.now_kst()가 항상 KST(UTC+9) 타임존을 반환하는지 검증
    assert cli.now_kst().tzinfo == KST


@pytest.mark.integration
def test_boundary_6_unicode_nfd_vs_nfc_normalization_across_diff_linter_and_audit(tmp_path):
    """[경계 ⑥ OS 유니코드 NFD↔NFC 경계] macOS 자모 분리(NFD) 한글과 표준 완성형(NFC) 한글이 섞여 들어와도 Diff·린터·로컬 PDF 감사에서 오탐 없이 NFC로 일관되게 처리된다."""
    nfd_name = unicodedata.normalize("NFD", "친절한-SQL-튜닝.pdf")
    nfc_name = unicodedata.normalize("NFC", "친절한-SQL-튜닝.pdf")
    nfd_folder = unicodedata.normalize("NFD", "데이터베이스/오라클")
    nfc_folder = unicodedata.normalize("NFC", "데이터베이스/오라클")
    assert nfd_name != nfc_name

    # 1) diff_snapshots: NFD와 NFC만 다르고 실제 글자가 같으면 이름 변경(renamed)·폴더 이동(moved) 오탐이 발생하지 않아야 한다
    old_nfd = [{"id": "book-1", "name": nfd_name, "folder_path": nfd_folder, "sha256": "same-sha", "size_bytes": 100}]
    new_nfc = [{"id": "book-1", "name": nfc_name, "folder_path": nfc_folder, "sha256": "same-sha", "size_bytes": 100}]
    changes = diff_snapshots(old_nfd, new_nfc)
    assert changes.is_empty, f"NFD vs NFC should not trigger diff: {changes}"

    # 2) duplicate_and_naming_linter: NFD 레코드가 입력되어도 경고 메시지(_path)와 파일명 검사가 모두 NFC로 정규화되어야 한다
    nfd_dup_1 = {"id": "1", "name": nfd_name, "folder_path": nfd_folder, "sha256": "sha-111"}
    nfc_dup_2 = {"id": "2", "name": nfc_name, "folder_path": nfc_folder, "sha256": "sha-222"}
    same_title_warnings = find_same_title_different_content([nfd_dup_1, nfc_dup_2])
    assert len(same_title_warnings) == 1
    assert unicodedata.is_normalized("NFC", same_title_warnings[0].message)

    nfd_no_ext = {"id": "3", "name": unicodedata.normalize("NFD", "확장자없는한글제목"), "folder_path": nfd_folder, "sha256": "sha-333"}
    fn_warnings = find_filename_issues([nfd_no_ext])
    assert [w.kind for w in fn_warnings] == [WarningKind.MISSING_PDF_EXTENSION]
    assert unicodedata.is_normalized("NFC", fn_warnings[0].message)

    # 3) audit_local_pdfs: 로컬 파일 경로가 NFD로 생성되어도 LocalPdfAudit.path는 NFC로 정규화된다
    pdf_file = tmp_path / nfd_name
    pdf_file.write_bytes(b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n")
    audits = audit_local_pdfs([pdf_file])
    assert len(audits) == 1
    assert unicodedata.is_normalized("NFC", audits[0].path)


@pytest.mark.integration
def test_boundary_7_module_entrypoint_zero_runtime_warnings_and_env_precedence(tmp_path):
    """[경계 ⑦ 프로세스 런타임·패키징 경계] python -W error -m 실행 시 PEP 562 지연 로딩으로 RuntimeWarning이 0건이며, 환경변수 우선순위와 외부 작업 디렉토리 실행이 일관되게 동작한다."""
    # 1) python -W error -m doctree.doctree_cli.command_dispatcher --help 실행 시 RuntimeWarning 0건 검증
    res = subprocess.run(
        [sys.executable, "-W", "error", "-m", "doctree.doctree_cli.command_dispatcher", "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"stderr={res.stderr}"
    assert "usage: doctree" in res.stdout

    # 2) WorkspaceSettings.from_env 우선순위 검증 (명시적 env > 기본값)
    custom_env = {
        "DOCTREE_SNAPSHOTS_DIR": "/custom/snaps",
        "VIRUSTOTAL_API_KEY": "custom-vt-key",
    }
    ws = WorkspaceSettings.from_env(env=custom_env, base_dir=tmp_path)
    assert ws.snapshots_dir == "/custom/snaps"
    assert ws.vt_cache_file == "/custom/snaps/.vt_cache.json"
    assert ws.virustotal_api_key == "custom-vt-key"
    assert ws.category_tree_file == str(tmp_path / "config" / "category_tree.yaml")
    assert ws.content_category_map_file == str(tmp_path / "config" / "content_category_map.yaml")
    assert ws.project_layout_profiles_file == str(tmp_path / "config" / "project_layout_profiles.yaml")


@pytest.mark.integration
def test_boundary_8_live_repository_data_contract_configs_and_snapshots():
    """[경계 ⑧ 실제 저장소 데이터 계약 경계] 로컬 config/{category_tree,content_category_map,project_layout_profiles}.yaml과 snapshots/*.json 전체가 스키마 오류 0건·미분류 0건으로 정합성을 유지한다."""
    categories_yaml = BASE_DIR / "config" / "category_tree.yaml"
    assignments_yaml = BASE_DIR / "config" / "content_category_map.yaml"
    layout_yaml = BASE_DIR / "config" / "project_layout_profiles.yaml"
    snapshots_dir = BASE_DIR / "snapshots"
    # 실제 배정표와 스냅샷은 개인 Drive 메타데이터라 .gitignore 대상이다. 로컬에 없으면(공개 클론 등) 건너뛴다.
    if not assignments_yaml.is_file() or not any(snapshots_dir.glob("*.json")):
        pytest.skip("로컬 전용 데이터(config/content_category_map.yaml, snapshots/*.json)가 없음")

    # 1) category_tree.yaml (16개 대분야 · 55개 세부분야) 및 content_category_map.yaml 로드 검증
    catalog = load_catalog(categories_yaml, assignments_yaml)
    assert len(catalog.categories) == 16
    assert sum(len(c.subcategories) for c in catalog.categories) == 55
    assert len(catalog.assignments) >= 232

    # 2) project_layout_profiles.yaml 스키마 검증 (handcrafted, toolchain 프로파일 정의)
    layout_data = yaml.safe_load(layout_yaml.read_text(encoding="utf-8"))
    assert layout_data["schema_version"] == 1
    assert set(layout_data["profiles"].keys()) == {"handcrafted", "toolchain"}
    for profile_name, profile in layout_data["profiles"].items():
        assert profile["content_dir"] == "contents", profile_name
        assert "/contents/" in profile["gitignore_entries"], profile_name

    # 3) snapshots/*.json 실제 스냅샷 파일들이 모두 유효한 JSON 스키마이며 strict=True 분류를 통과하는지 검증
    snapshots_dir = BASE_DIR / "snapshots"
    snapshot_files = sorted(
        p for p in snapshots_dir.glob("*.json") if not p.name.startswith(".")
    )
    assert len(snapshot_files) >= 1
    for snap_path in snapshot_files:
        payload = json.loads(snap_path.read_text(encoding="utf-8"))
        records = payload["records"]
        assert payload["count"] == len(records), f"{snap_path.name} count mismatch"
        result = classify_records(records, catalog, strict=True)
        assert len(result.unclassified) == 0, f"{snap_path.name} has unclassified items"


@pytest.mark.integration
def test_boundary_9_narrow_terminal_width_pipe_stream_never_hard_wraps_lines(tmp_path):
    """[경계 ⑨ 터미널 폭·파이프 스트림 경계] 터미널 폭이 극단적으로 좁은 환경(width=30)에서도 한글 경로와 에러 메시지가 중간에 강제 개행(Hard Wrap)되지 않고 한 줄로 출력된다."""
    narrow_buf = io.StringIO()
    narrow_console = Console(file=narrow_buf, width=30, force_terminal=False, color_system=None)

    long_pdf_name = "매우긴한글도서제목_데이터베이스와알고리즘실전가이드_개정판.pdf"
    long_folder = "컴퓨터과학/데이터베이스/성능튜닝/심화과정"
    category_tree = tmp_path / "category_tree.yaml"
    content_map = tmp_path / "content_category_map.yaml"
    snaps = tmp_path / "snapshots"
    category_tree.write_text(
        "schema_version: 1\ncategories:\n  - id: cs\n    name: 컴퓨터 과학\n    subcategories:\n      - id: cs.db\n        name: 데이터베이스\n",
        encoding="utf-8",
    )
    content_map.write_text(
        "schema_version: 1\ncontents:\n  - file_id: book-long\n    title: 긴제목\n    category_id: cs.db\n",
        encoding="utf-8",
    )

    drive = MagicMock()
    drive.service = MagicMock()
    drive.list_pdf_files.return_value = [
        {
            "id": "book-long",
            "name": long_pdf_name,
            "folder_path": long_folder,
            "size_bytes": 1024,
            "sha256": "a" * 64,
            "modified_time": "2026-10-27T00:00:00Z",
            "web_view_link": "",
        }
    ]

    with patch.object(cli, "console", narrow_console), \
            patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=drive):
        code = cli.main([
            "update",
            "--snapshots-dir", str(snaps),
            "--category-tree", str(category_tree),
            "--content-map", str(content_map),
        ])
    assert code == 0
    lines = narrow_buf.getvalue().splitlines()
    # width=30이어도 스냅샷 경로 줄과 요약 줄이 강제 줄바꿈으로 쪼개지지 않고 한 줄에 온전히 들어 있어야 한다
    assert any("🗂️ 스냅샷:" in line and str(snaps) in line for line in lines)
    assert any("✅ 총 1권 · 분류 1권 · 미분류 0권 · 경고 0건" in line for line in lines)


def test_process_terminal_signal_boundaries_and_packaging_hygiene():
    """[경계 ① OS 프로세스·터미널 시그널 및 배포 위생] Ctrl+C(KeyboardInterrupt -> 130), 파이프 조기 종료(BrokenPipeError -> 0), .gitignore·.env.example·AGENTS.md 정합성을 검증한다."""
    # 1) KeyboardInterrupt -> 종료 코드 130 및 깔끔한 중단 안내 메시지
    buf_kb = io.StringIO()
    with patch.object(cli, "console", Console(file=buf_kb, force_terminal=False, color_system=None)), \
            patch("doctree.doctree_cli.command_dispatcher.cmd_status", side_effect=KeyboardInterrupt):
        assert cli.main(["status"]) == 130
    assert "사용자에 의해 작업이 중단되었습니다 (Ctrl+C)." in buf_kb.getvalue()

    # 2) BrokenPipeError -> 종료 코드 0 무음 종료
    with patch("doctree.doctree_cli.command_dispatcher.cmd_status", side_effect=BrokenPipeError):
        assert cli.main(["status"]) == 0

    # 3) .gitignore에 커버리지·빌드·캐시 산출물 누락 방지 확인
    gitignore_lines = {line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for required_ignore in (".coverage", ".coverage.*", "htmlcov/", "build/", "dist/", ".vt_cache.json"):
        assert required_ignore in gitignore_lines

    # 4) .env.example 템플릿 파일 존재 및 핵심 환경변수 키 포함 확인
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "GOOGLE_SERVICE_ACCOUNT_FILE=",
        "GOOGLE_DRIVE_FOLDER_ID=",
        "VIRUSTOTAL_API_KEY=",
        "DOCTREE_SNAPSHOTS_DIR=",
    ):
        assert key in env_example

    # 5) AGENTS.md에 구버전 모듈 경로(src.classification)가 남아 있지 않고 정식 경로가 기재되어 있는지 확인
    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "src.classification" not in agents_md
    assert "doctree.content_taxonomy.category_classifier.load_catalog" in agents_md


@settings(max_examples=80, deadline=None)
@given(
    err_cls=st.sampled_from(
        [
            DriveAuthError,
            DriveListError,
            ClassificationError,
            SnapshotStorageError,
            SecurityAuditError,
        ]
    ),
    msg=st.text(max_size=60),
    preserve=st.booleans(),
)
@example(err_cls=DriveAuthError, msg="", preserve=False)
@example(err_cls=DriveListError, msg="드라이브 목록 조회 실패: 이미 접두어 있음", preserve=True)
def test_property_fuzz_boundary_error_formatting_and_http_get_coercion(
    err_cls: type[DoctreeBoundaryError],
    msg: str,
    preserve: bool,
) -> None:
    """DoctreeBoundaryError.format_error의 접두어 멱등성, coerce_boundary_error 보존성, execute_json_http_get의 예외 포획 불변식을 검증한다."""
    err = err_cls(msg, preserve_snapshot=preserve)
    formatted = err.format_error()
    assert err_cls(formatted, preserve_snapshot=preserve).format_error() == formatted
    assert coerce_boundary_error(err, preserve_snapshot=preserve) is err
    coerced = coerce_boundary_error(
        ValueError(msg),
        default_boundary=err.boundary,
        preserve_snapshot=preserve,
    )
    assert isinstance(coerced, DoctreeBoundaryError)
    assert coerced.boundary == err.boundary
    assert coerced.preserve_snapshot is preserve

    body, code, detail = execute_json_http_get(
        "https://example.com",
        headers={},
        urlopen=MagicMock(side_effect=ValueError(msg)),
    )
    assert body is None and code is None and isinstance(detail, str)
