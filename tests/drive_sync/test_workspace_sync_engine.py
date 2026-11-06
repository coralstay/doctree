# ---
# title: test_workspace_sync_engine
# description: 드라이브와 로컬 contents/ 3-Way 비교 및 pull 동기화 엔진의 테스트
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""드라이브 ⟷ 로컬 contents/ 고속 스캔·3-Way 비교 및 도서 가져오기(pull) 엔진(workspace_sync_engine.py) 단위·통합 테스트."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from rich.console import Console

from doctree.doctree_cli import command_dispatcher as cli
from doctree.drive_sync.workspace_sync_engine import (
    LocalPdfEntry,
    _sanitize_pdf_filename,
    compare_drive_and_local,
    format_sync_report,
    load_local_contents_cache,
    pull_drive_pdf_to_project,
    resolve_drive_record_for_pull,
    save_local_contents_cache,
    scan_workspace_contents,
)
from doctree.runtime_env.boundary_errors import (
    ClassificationError,
    DriveListError,
    SnapshotStorageError,
)
from doctree.runtime_env.workspace_settings import normalize_nfc


def _make_pdf(path: Path, text: str = "hello") -> str:
    data = f"%PDF-1.7\n({text})\n%%EOF\n".encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_local_contents_cache_load_save_and_error_handling(tmp_path: Path):
    """로컬 contents/ 해시 캐시 로드·원자적 저장 및 손상/오류 분기를 검증한다."""
    # 1) None 또는 없는 파일 또는 디렉터리일 때 빈 dict 반환
    assert load_local_contents_cache(None) == {}
    assert load_local_contents_cache(tmp_path / "missing.json") == {}
    assert load_local_contents_cache(tmp_path) == {}

    # 2) 손상된 JSON 또는 배열 JSON일 때 빈 dict 반환
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{corrupt", encoding="utf-8")
    assert load_local_contents_cache(bad_json) == {}
    bad_json.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_local_contents_cache(bad_json) == {}

    # 3) 유효한 엔트리와 무효한 엔트리(짧은 해시, 후행 개행 해시, bool size 등) 혼합 시 유효 엔트리만 로드
    valid_sha = "a" * 64
    mixed_cache = tmp_path / "mixed.json"
    mixed_cache.write_text(
        json.dumps(
            {
                "databases/contents/a.pdf": {
                    "project": "databases",
                    "name": "a.pdf",
                    "size_bytes": 100,
                    "mtime_ns": 123456,
                    "sha256": valid_sha,
                },
                "invalid_sha": {
                    "project": "databases",
                    "name": "b.pdf",
                    "size_bytes": 100,
                    "mtime_ns": 123456,
                    "sha256": "short",
                },
                "trailing_newline_sha": {
                    "project": "databases",
                    "name": "newline.pdf",
                    "size_bytes": 100,
                    "mtime_ns": 123456,
                    "sha256": f"{valid_sha}\n",
                },
                "invalid_bool_size": {
                    "project": "databases",
                    "name": "c.pdf",
                    "size_bytes": True,
                    "mtime_ns": 123456,
                    "sha256": valid_sha,
                },
                "not_a_dict": "string_val",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_local_contents_cache(mixed_cache)
    assert list(loaded.keys()) == ["databases/contents/a.pdf"]
    assert loaded["databases/contents/a.pdf"]["sha256"] == valid_sha

    # 4) save_local_contents_cache 원자적 저장 및 os.replace / write_text 실패 분기
    cache_out = tmp_path / "sub" / ".local_contents_cache.json"
    save_local_contents_cache(cache_out, loaded)
    assert cache_out.is_file()

    with (
        patch.object(Path, "replace", side_effect=OSError("disk full")),
        pytest.raises(SnapshotStorageError, match="로컬 해시 캐시 저장 실패"),
    ):
        save_local_contents_cache(cache_out, loaded)
    assert not (cache_out.parent / f".{cache_out.name}.tmp").exists()

    with (
        patch.object(Path, "write_bytes", side_effect=OSError("ro fs")),
        pytest.raises(SnapshotStorageError, match="로컬 해시 캐시 저장 실패"),
    ):
        save_local_contents_cache(cache_out, loaded)


def test_scan_workspace_contents_and_3way_comparison_and_report(tmp_path: Path):
    """고속 로컬 스캔(캐시 미스 -> 캐시 히트) 및 3-Way 정합성 비교·터미널 리포트 포매팅을 검증한다."""
    # 존재하지 않는 워크스페이스
    assert scan_workspace_contents(tmp_path / "no_ws") == ([], 0, 0)

    ws = tmp_path / "ws"
    (ws / ".hidden" / "contents").mkdir(parents=True)
    sha_matched = _make_pdf(ws / "databases" / "contents" / "친절한-SQL-튜닝.pdf", "sql book")
    _make_pdf(ws / "handcrafted-cpp" / "contents" / "로컬전용.pdf", "local cpp")
    cache_file = tmp_path / ".local_contents_cache.json"

    # 1차 스캔: 캐시 미스(recomputed=2, hits=0)
    entries1, hits1, recomp1 = scan_workspace_contents(ws, cache_path=cache_file)
    assert len(entries1) == 2
    assert hits1 == 0
    assert recomp1 == 2
    assert cache_file.is_file()

    # 2차 스캔: 파일 변경 없음 -> 캐시 히트(hits=2, recomputed=0), cache_path=None 분기 포함
    entries2, hits2, recomp2 = scan_workspace_contents(ws, cache_path=cache_file)
    assert len(entries2) == 2
    assert hits2 == 2
    assert recomp2 == 0
    entries_no_save, _, _ = scan_workspace_contents(ws, cache_path=None)
    assert len(entries_no_save) == 2

    # 3-Way 비교 (Matched 1, Drive Only 2(해시 있음 1 + 해시 없음 1), Local Only 1)
    drive_records = [
        {"id": "d1", "name": "친절한-SQL-튜닝.pdf", "folder_path": "DB", "sha256": sha_matched},
        {"id": "d2", "name": "드라이브전용.pdf", "folder_path": "CS", "sha256": "b" * 64},
        {"id": "d3", "name": "해시없는문서.pdf", "folder_path": "", "sha256": ""},
    ]
    comp = compare_drive_and_local(drive_records, entries2, cache_hits=hits2, recomputed=recomp2)
    assert len(comp.matched) == 1
    assert comp.matched[0].sha256 == sha_matched
    assert len(comp.drive_only) == 2
    assert len(comp.local_only) == 1

    report = format_sync_report(comp, len(drive_records), len(entries2))
    assert "양쪽 일치 1권 · 드라이브 전용 2권 · 로컬 전용 1개" in report
    assert "databases/contents/친절한-SQL-튜닝.pdf" in report
    assert "path=CS/드라이브전용.pdf" in report
    assert "path=해시없는문서.pdf" in report
    assert "handcrafted-cpp/contents/로컬전용.pdf" in report

    # 빈 결과 리포트 분기 검증
    empty_comp = compare_drive_and_local([], [])
    empty_report = format_sync_report(empty_comp, 0, 0)
    assert "양쪽 일치하는 도서가 없습니다." in empty_report
    assert "드라이브 전용 도서가 없습니다." in empty_report
    assert "로컬 전용 도서가 없습니다." in empty_report


def test_resolve_drive_record_and_pull_pdf_to_project(tmp_path: Path):
    """resolve_drive_record_for_pull의 모든 매칭·오류 분기와 pull_drive_pdf_to_project 다운로드를 검증한다."""
    ws = tmp_path / "ws"
    pdf_bytes = b"%PDF-1.7\n1 0 obj << >> endobj\n%%EOF\n"
    sha_sql = hashlib.sha256(pdf_bytes).hexdigest()

    records = [
        {"id": "id-sql", "name": "친절한-SQL-튜닝.pdf", "folder_path": "DB", "sha256": sha_sql},
        {"id": "id-cpp-1", "name": "A/B-C++.pdf", "folder_path": "CPP", "sha256": "c" * 64},
        {"id": "id-cpp-2", "name": "Effective-C++.pdf", "folder_path": "CPP", "sha256": "d" * 64},
        {"id": "id-dup-1", "name": "중복제목.pdf", "folder_path": "A", "sha256": "e" * 64},
        {"id": "id-dup-2", "name": "중복제목.pdf", "folder_path": "B", "sha256": "f" * 64},
    ]

    # 1) 빈 질의 오류
    with pytest.raises(ClassificationError, match="가져올 도서 식별자"):
        resolve_drive_record_for_pull(records, "   ")

    # 2) ID 정확 일치, SHA-256 정확 일치, 파일명(.pdf 유무) 정확 일치, 부분 문자열 유일 일치
    assert (
        resolve_drive_record_for_pull(records, "id-sql")["id"] == "id-sql"
        and resolve_drive_record_for_pull(records, sha_sql)["id"] == "id-sql"
        and resolve_drive_record_for_pull(records, "친절한-SQL-튜닝")["id"] == "id-sql"
        and resolve_drive_record_for_pull(records, "Effective")["id"] == "id-cpp-2"
    )

    # 3) 동명 도서 복수 존재, 동일 SHA-256 복수 존재, 부분 일치 복수 존재, 미존재 오류
    with pytest.raises(ClassificationError, match="동일한 이름의 도서가 2권"):
        resolve_drive_record_for_pull(records, "중복제목.pdf")
    dup_sha_records = [
        {"id": "s1", "name": "A.pdf", "folder_path": "", "sha256": "1" * 64},
        {"id": "s2", "name": "B.pdf", "folder_path": "", "sha256": "1" * 64},
    ]
    with pytest.raises(ClassificationError, match="동일한 SHA-256 해시의 도서가 2권"):
        resolve_drive_record_for_pull(dup_sha_records, "1" * 64)
    with pytest.raises(ClassificationError, match="일치하는 도서가 2권"):
        resolve_drive_record_for_pull(records, "C++")
    with pytest.raises(ClassificationError, match="찾을 수 없습니다"):
        resolve_drive_record_for_pull(records, "존재하지않는책")

    # 4) pull_drive_pdf_to_project: 잘못된 프로젝트명 및 잘못된 파일명 차단
    drive = MagicMock()
    drive.service = MagicMock()
    drive.service.files.return_value.get_media.return_value.execute.return_value = pdf_bytes

    with pytest.raises(ClassificationError, match="유효하지 않은 대상 프로젝트"):
        pull_drive_pdf_to_project(drive, records, "id-sql", "../escape", workspace_dir=ws)
    with pytest.raises(ClassificationError, match="유효하지 않은 대상 프로젝트"):
        pull_drive_pdf_to_project(drive, records, "id-sql", "   ", workspace_dir=ws)
    with pytest.raises(ClassificationError, match="유효하지 않은 하위 경로"):
        pull_drive_pdf_to_project(drive, records, "id-sql", "databases", workspace_dir=ws, subdir="../escape")
    with pytest.raises(ClassificationError, match="유효하지 않은 출력 파일명"):
        pull_drive_pdf_to_project(drive, records, "id-sql", "databases", workspace_dir=ws, output_name=".pdf")

    # 5) pull 정상 다운로드 (.pdf 확장자 자동 보정 + 계층 경로 + --subdir + 캐시 갱신) 및 cache_path=None 분기
    cache_file = tmp_path / ".local_contents_cache.json"
    _rec, dest = pull_drive_pdf_to_project(
        drive,
        records,
        "id-sql",
        "databases",
        workspace_dir=ws,
        output_name="커스텀이름",
        cache_path=cache_file,
    )
    # 하위 경로 미지정 시 도서 파일명과 같은 디렉토리(contents/<도서명>/)에 배치
    assert dest == ws / "databases" / "contents" / "커스텀이름" / "커스텀이름.pdf"
    assert dest.read_bytes() == pdf_bytes
    assert cache_file.is_file()

    _, dest_inline_track = pull_drive_pdf_to_project(
        drive,
        records,
        "id-sql",
        "handcrafted-java/spring/01_toby_spring",
        workspace_dir=ws,
        subdir="contents/part1",
    )
    assert (
        dest_inline_track
        == ws / "handcrafted-java" / "contents" / "spring" / "01_toby_spring" / "part1" / "친절한-SQL-튜닝.pdf"
    )

    # sha256가 비어 있는 레코드 다운로드 및 cache_path=None 분기
    records_no_sha = [{"id": "id-nosha", "name": "nosha.pdf", "folder_path": "", "sha256": ""}]
    _, dest2 = pull_drive_pdf_to_project(
        drive,
        records_no_sha,
        "id-nosha",
        "databases",
        workspace_dir=ws,
        cache_path=None,
    )
    assert dest2 == ws / "databases" / "contents" / "nosha" / "nosha.pdf"
    assert dest2.is_file()

    # 6) Drive 서비스 미활성화, API 예외, SHA-256 불일치 시 DriveListError 검증
    drive_no_svc = MagicMock(service=None)
    with pytest.raises(DriveListError, match="활성화되지 않았습니다"):
        pull_drive_pdf_to_project(drive_no_svc, records, "id-sql", "databases", workspace_dir=ws)

    drive_err = MagicMock()
    drive_err.service.files.return_value.get_media.return_value.execute.side_effect = RuntimeError("net fail")
    with pytest.raises(DriveListError, match="다운로드 실패"):
        pull_drive_pdf_to_project(drive_err, records, "id-sql", "databases", workspace_dir=ws)

    drive_bad_sha = MagicMock()
    drive_bad_sha.service.files.return_value.get_media.return_value.execute.return_value = b"corrupted"
    with pytest.raises(DriveListError, match="SHA-256 해시가 스냅샷과 일치하지 않습니다"):
        pull_drive_pdf_to_project(drive_bad_sha, records, "id-sql", "databases", workspace_dir=ws)


def test_cli_sync_and_pull_commands(tmp_path: Path):
    """CLI `doctree sync` 및 `doctree pull` 명령의 성공(0) 및 실패(1) 흐름을 검증한다."""
    ws = tmp_path / "ws"
    pdf_bytes = b"%PDF-1.7\n(sync test)\n%%EOF\n"
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    (ws / "databases" / "contents").mkdir(parents=True)
    (ws / "databases" / "contents" / "친절한-SQL-튜닝.pdf").write_bytes(pdf_bytes)

    snaps_dir = tmp_path / "snapshots"
    snaps_dir.mkdir()
    snap_file = snaps_dir / "2026-10-28.json"
    snap_file.write_text(
        json.dumps(
            {
                "count": 1,
                "records": [
                    {
                        "id": "b1",
                        "name": "친절한-SQL-튜닝.pdf",
                        "folder_path": "DB",
                        "size_bytes": len(pdf_bytes),
                        "sha256": sha,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    buf = io.StringIO()
    test_console = Console(file=buf, width=120, force_terminal=False, color_system=None)

    with patch.object(cli, "console", test_console):
        # 1) sync (최신 스냅샷 자동 탐색 및 --snapshot 명시)
        assert (
            cli.main(
                [
                    "sync",
                    "--snapshots-dir",
                    str(snaps_dir),
                    "--workspace-dir",
                    str(ws),
                ]
            )
            == 0
        )
        assert "양쪽 일치 1권" in buf.getvalue()

        buf.truncate(0)
        buf.seek(0)
        assert (
            cli.main(
                [
                    "sync",
                    "--snapshot",
                    str(snap_file),
                    "--workspace-dir",
                    str(ws),
                    "--cache-file",
                    str(tmp_path / "explicit_cache.json"),
                ]
            )
            == 0
        )
        assert "양쪽 일치 1권" in buf.getvalue()

        with patch("doctree.doctree_cli.command_dispatcher.SNAPSHOTS_DIR", str(snaps_dir)), \
                patch("doctree.doctree_cli.command_dispatcher.LOCAL_CONTENTS_CACHE_FILE", str(tmp_path / "default_cache.json")):
            assert cli.main(["sync", "--workspace-dir", str(ws)]) == 0
            assert cli.main(["sync", "--snapshot", str(snap_file), "--workspace-dir", str(ws)]) == 0

        # 2) 스냅샷이 없는 빈 폴더에서 sync 실패 (종료 코드 1)
        empty_snaps = tmp_path / "empty_snaps"
        assert cli.main(["sync", "--snapshots-dir", str(empty_snaps), "--workspace-dir", str(ws)]) == 1

        # 3) pull 성공 및 인증 실패 시 종료 코드 1
        fake_drive = MagicMock()
        fake_drive.service = MagicMock()
        fake_drive.service.files.return_value.get_media.return_value.execute.return_value = pdf_bytes
        buf.truncate(0)
        buf.seek(0)
        with patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=fake_drive):
            assert (
                cli.main(
                    [
                        "pull",
                        "b1",
                        "handcrafted-cpp",
                        "--snapshots-dir",
                        str(snaps_dir),
                        "--workspace-dir",
                        str(ws),
                    ]
                )
                == 0
            )
        assert "📥 도서 가져오기 완료:" in buf.getvalue()
        assert (ws / "handcrafted-cpp" / "contents" / "친절한-SQL-튜닝" / "친절한-SQL-튜닝.pdf").is_file()

        unauth_drive = MagicMock(service=None)
        with patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=unauth_drive):
            assert (
                cli.main(
                    [
                        "pull",
                        "b1",
                        "handcrafted-cpp",
                        "--snapshots-dir",
                        str(snaps_dir),
                        "--workspace-dir",
                        str(ws),
                    ]
                )
                == 1
            )


@settings(max_examples=100, deadline=None)
@given(
    raw_name=st.one_of(st.none(), st.text(max_size=60)),
    fallback_name=st.text(max_size=60),
)
@example(raw_name=".   \u1100\u1161.pdf", fallback_name="기본.pdf")
@example(raw_name=".\u00a0a.pdf", fallback_name="기본.pdf")
@example(raw_name="../../\x00\n../친절한 SQL.PDF", fallback_name="기본.pdf")
@example(raw_name="/.pdf", fallback_name=".. / ..")
@example(raw_name=". .pdf", fallback_name=".pdf")
@example(raw_name="\u200b\u202e\ufeff.pdf", fallback_name="\u200b.pdf")
def test_property_fuzz_sanitize_pdf_filename_idempotence_and_confinement(
    raw_name: str | None,
    fallback_name: str,
) -> None:
    """임의 유니코드(NFD 자모·제어문자·공백·경로 탈출 문자)에 대해 normalize_nfc와 _sanitize_pdf_filename의 멱등성 및 타깃 디렉터리 격리를 검증한다."""
    norm_fb = normalize_nfc(fallback_name)
    assert normalize_nfc(norm_fb) == norm_fb
    assert normalize_nfc(unicodedata.normalize("NFD", fallback_name)) == norm_fb

    target_dir = Path("/tmp/doctree_fuzz_ws/databases/contents").resolve()
    try:
        sanitized = _sanitize_pdf_filename(raw_name, fallback_name)
    except ClassificationError:
        return

    assert _sanitize_pdf_filename(sanitized, fallback_name) == sanitized
    assert _sanitize_pdf_filename(unicodedata.normalize("NFD", sanitized), fallback_name) == sanitized
    assert sanitized.lower().endswith(".pdf")
    assert len(sanitized) > 4
    assert "/" not in sanitized
    assert "\\" not in sanitized
    assert not sanitized.startswith(".")
    assert all(ch.isprintable() for ch in sanitized)
    assert (target_dir / sanitized).resolve().parent == target_dir


_SYNC_SHA_STRATEGY = st.sampled_from(["", "   ", "a" * 64, "B" * 64, f"  {'c' * 64}  "])


@settings(max_examples=80, deadline=None)
@given(
    raw_drive=st.lists(
        st.tuples(st.text(max_size=20), st.text(max_size=20), _SYNC_SHA_STRATEGY),
        max_size=8,
    ),
    raw_local=st.lists(
        st.tuples(st.text(min_size=1, max_size=24), _SYNC_SHA_STRATEGY),
        max_size=8,
    ),
)
@example(
    raw_drive=[("한글.pdf", "DB", "A" * 64), ("무해시.pdf", "", "")],
    raw_local=[("databases/contents/\u1112\u1161\u11ab\u1100\u1173\u11af.pdf", "a" * 64), ("p/contents/x.pdf", "")],
)
def test_property_fuzz_compare_drive_and_local_set_conservation(
    raw_drive: list[tuple[str, str, str]],
    raw_local: list[tuple[str, str]],
) -> None:
    """compare_drive_and_local의 3-Way 기수 보존, 해시 집합 상호 배타성 및 NFD/NFC 동치 불변식을 검증한다."""
    drive_recs = [
        {"id": f"d-{i}", "name": name, "folder_path": folder, "sha256": sha}
        for i, (name, folder, sha) in enumerate(raw_drive)
    ]
    local_ents = [LocalPdfEntry(relative_path=rel, sha256=sha) for rel, sha in raw_local]

    comp = compare_drive_and_local(drive_recs, local_ents)
    assert len(comp.drive_only) + sum(len(g.drive_records) for g in comp.matched) == len(drive_recs)
    assert len(comp.local_only) + sum(len(g.local_entries) for g in comp.matched) == len(local_ents)

    matched_shas = {g.sha256 for g in comp.matched}
    drive_only_shas = {str(r.get("sha256") or "").strip().lower() for r in comp.drive_only} - {""}
    local_only_shas = {e.sha256 for e in comp.local_only} - {""}
    assert matched_shas.isdisjoint(drive_only_shas)
    assert matched_shas.isdisjoint(local_only_shas)
    assert drive_only_shas.isdisjoint(local_only_shas)


def test_resolve_drive_record_precedence_and_sync_promotion_metamorphic() -> None:
    """쿼리 해석 4단계 우선순위(id > sha256 > 정확 이름 > 부분 이름) 및 3-Way 동기화 승격 메타모픽 관계를 검증한다."""
    sha_target = "d" * 64
    overlapping_records = [
        {"id": "exact.pdf", "name": "다른책.pdf", "folder_path": "A", "sha256": "1" * 64},
        {"id": "rec-sha", "name": "exact.pdf", "folder_path": "B", "sha256": sha_target},
        {"id": "rec-sub", "name": f"부제-{sha_target}.pdf", "folder_path": "C", "sha256": "2" * 64},
    ]
    # 1단계(id)가 3단계(exact name)보다 우선한다.
    assert resolve_drive_record_for_pull(overlapping_records, "exact.pdf")["id"] == "exact.pdf"
    # 2단계(sha256)가 4단계(부분 문자열 이름)보다 우선한다.
    assert resolve_drive_record_for_pull(overlapping_records, sha_target)["id"] == "rec-sha"

    # 메타모픽 관계: drive_only에 있던 SHA-256을 로컬 엔트리에 추가하면 matched로 정확히 승격된다.
    before = compare_drive_and_local(overlapping_records, [])
    assert len(before.drive_only) == 3
    assert len(before.matched) == 0

    promoted_local = [LocalPdfEntry(relative_path="db/contents/exact.pdf", sha256=sha_target)]
    after = compare_drive_and_local(overlapping_records, promoted_local)
    assert len(after.drive_only) == 2
    assert len(after.matched) == 1
    assert after.matched[0].sha256 == sha_target
    assert after.local_only == ()


class WorkspaceSyncStateMachine(RuleBasedStateMachine):
    """[Pass 7/8] 로컬 캐시 스캔·Drive PDF pull·3-Way 비교 상태 전이 및 손상 다운로드 시 기존 파일 보존 상태 머신."""

    def __init__(self) -> None:
        super().__init__()
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.ws_dir = self.tmp_dir / "ws"
        self.proj_dir = self.ws_dir / "proj-a"
        (self.proj_dir / "contents").mkdir(parents=True, exist_ok=True)
        self.cache_file = self.tmp_dir / ".local_contents_cache.json"
        self.drive_records: dict[str, dict[str, Any]] = {}
        self.local_files: dict[str, str] = {}

    def teardown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @rule(
        doc_idx=st.integers(min_value=1, max_value=4),
        payload=st.binary(min_size=4, max_size=32),
    )
    def rule_register_drive_record_and_pull(self, doc_idx: int, payload: bytes) -> None:
        pdf_bytes = b"%PDF-1.7\n" + payload + b"\n%%EOF\n"
        sha = hashlib.sha256(pdf_bytes).hexdigest()
        rec = {
            "id": f"fid-{doc_idx}",
            "name": f"book_{doc_idx}.pdf",
            "folder_path": "Books",
            "sha256": sha,
        }
        self.drive_records[f"fid-{doc_idx}"] = rec
        drive = MagicMock()
        drive.service = MagicMock()
        drive.service.files.return_value.get_media.return_value.execute.return_value = pdf_bytes
        pulled_rec, dest = pull_drive_pdf_to_project(
            drive,
            list(self.drive_records.values()),
            f"fid-{doc_idx}",
            "proj-a",
            workspace_dir=self.ws_dir,
            cache_path=self.cache_file,
        )
        assert pulled_rec["sha256"] == sha
        assert dest.read_bytes() == pdf_bytes
        assert dest == (self.proj_dir / "contents" / f"book_{doc_idx}" / f"book_{doc_idx}.pdf").resolve()
        self.local_files[f"proj-a/contents/book_{doc_idx}/book_{doc_idx}.pdf"] = sha

    @rule(doc_idx=st.integers(min_value=1, max_value=4))
    def rule_corrupt_pull_preserves_existing_file(self, doc_idx: int) -> None:
        rec = {
            "id": f"fid-{doc_idx}",
            "name": f"book_{doc_idx}.pdf",
            "folder_path": "Books",
            "sha256": "a" * 64,
        }
        dest = self.proj_dir / "contents" / f"book_{doc_idx}" / f"book_{doc_idx}.pdf"
        prior_bytes = dest.read_bytes() if dest.exists() else None
        drive = MagicMock()
        drive.service = MagicMock()
        drive.service.files.return_value.get_media.return_value.execute.return_value = b"%PDF-corrupt"
        with pytest.raises(DriveListError, match="일치하지 않습니다"):
            pull_drive_pdf_to_project(
                drive,
                [rec],
                f"fid-{doc_idx}",
                "proj-a",
                workspace_dir=self.ws_dir,
                cache_path=self.cache_file,
            )
        assert (dest.read_bytes() if dest.exists() else None) == prior_bytes

    @invariant()
    def invariant_scan_and_3way_sync_consistent(self) -> None:
        assert not list((self.proj_dir / "contents").rglob(".*.tmp"))
        entries, _hits, _recomputed = scan_workspace_contents(self.ws_dir, cache_path=self.cache_file)
        assert {e.relative_path: e.sha256 for e in entries} == self.local_files
        comp = compare_drive_and_local(list(self.drive_records.values()), entries)
        local_shas = set(self.local_files.values())
        assert {g.sha256 for g in comp.matched} <= local_shas
        assert all(r["sha256"] not in local_shas for r in comp.drive_only)


def test_stateful_workspace_sync_and_pull_fault_preservation() -> None:
    """상태 머신으로 로컬 스캔 캐시·Drive pull·3-Way 비교 전이 및 손상 다운로드 시 기존 PDF 보존을 검증한다."""
    run_state_machine_as_test(
        WorkspaceSyncStateMachine,
        settings=settings(max_examples=20, stateful_step_count=10, deadline=None),
    )



