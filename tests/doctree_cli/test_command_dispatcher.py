# ---
# title: test_command_dispatcher
# description: doctree CLI 명령(auth·status·update·classify·audit 등)의 통합 테스트
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""doctree CLI (src.doctree_cli.command_dispatcher) 통합 검증: auth, status, update, classify, audit."""
import contextlib
import io
import json
import re
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pytest_bdd import given, parsers, scenario, then, when
from rich.console import Console

from doctree.content_taxonomy.category_classifier import classify_records, load_catalog
from doctree.doctree_cli import command_dispatcher as cli
from doctree.drive_sync.drive_pdf_fetcher import DriveListError, GoogleDriveManager
from doctree.runtime_env.workspace_settings import (
    BASE_DIR,
    GOOGLE_DRIVE_FOLDER_ID,
    GOOGLE_SERVICE_ACCOUNT_FILE,
)

ROOT = Path(__file__).resolve().parent.parent.parent

KST = timezone(timedelta(hours=9), "KST")
NOW = datetime(2026, 10, 21, 22, 30, tzinfo=KST)

ACCOUNT = {
    "name": "tester",
    "email": "t@example.com",
    "limit_gb": 100.0,
    "usage_gb": 70.0,
    "drive_usage_gb": 68.0,
    "usage_percent": 70.0,
}

CATEGORIES_YAML = """\
schema_version: 1
categories:
  - id: cs
    name: 컴퓨터 과학
    subcategories:
      - id: cs.algo
        name: 알고리즘
"""

ASSIGNMENTS_YAML = """\
schema_version: 1
contents:
  - file_id: a
    title: 종만북 1권
    category_id: cs.algo
"""


def _section(text: str, header: str) -> str:
    m = re.search(rf"^\[{re.escape(header)}\]\s*$(.*?)(?=^\[|\Z)", text, re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""


class TestPackaging(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "pyproject.toml").read_text()

    def test_package_name(self):
        """패키지명은 doctree다."""
        self.assertRegex(_section(self.text, "project"), r'(?m)^name\s*=\s*"doctree"\s*$')

    def test_single_doctree_entry_point(self):
        """CLI 진입점은 doctree 하나만 등록된다."""
        scripts = _section(self.text, "project.scripts")
        entries = dict(re.findall(r'(?m)^\s*"?([\w\-]+)"?\s*=\s*"([^"]+)"', scripts))
        self.assertEqual(entries, {"doctree": "doctree.doctree_cli.command_dispatcher:main"})


class TestParser(unittest.TestCase):
    def test_help_lists_workflows_and_subcommands(self):
        """doctree --help 및 doctree help 서브커맨드가 전체 명령어·워크플로·상세 도움말을 출력한다."""
        parser = cli.build_parser()
        self.assertEqual(parser.prog, "doctree")
        help_text = parser.format_help()
        self.assertIn("auth", help_text)
        self.assertIn("status", help_text)
        self.assertIn("help", help_text)
        self.assertIn("[1] 스냅샷 · 분류 · 보안 감사 · 레이아웃 기본 워크플로", help_text)
        self.assertIn("[2] Step 1~4 로컬 학습 프로젝트 파이프라인 워크플로", help_text)
        self.assertIn("이 도움말 메시지를 출력하고 종료", help_text)

        buf = io.StringIO()
        with _console(buf):
            self.assertEqual(cli.main(["help"]), 0)
            self.assertIn("usage: doctree", buf.getvalue())

            buf.truncate(0)
            buf.seek(0)
            self.assertEqual(cli.main(["help", "update"]), 0)
            self.assertIn("usage: doctree update", buf.getvalue())
            self.assertIn("사용 예시:", buf.getvalue())

            buf.truncate(0)
            buf.seek(0)
            self.assertEqual(cli.main(["help", "layout", "get"]), 0)
            self.assertIn("usage: doctree layout get", buf.getvalue())

            buf.truncate(0)
            buf.seek(0)
            self.assertEqual(cli.main(["help", "unknown_cmd"]), 1)
            self.assertIn("알 수 없는 명령어입니다: unknown_cmd", buf.getvalue())

    def test_command_required(self):
        """명령 없이 실행하면 사용법을 보여주고 실패한다."""
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            cli.main([])
        self.assertNotEqual(ctx.exception.code, 0)


class TestCommands(unittest.TestCase):
    def _fake_drive(self, service=True):
        drive = MagicMock()
        drive.service = MagicMock() if service else None
        drive.get_account_and_quota.return_value = ACCOUNT
        return drive

    @patch("doctree.doctree_cli.command_dispatcher.print_account_dashboard")
    @patch("doctree.doctree_cli.command_dispatcher.GoogleDriveManager")
    def test_auth_prints_account_dashboard(self, mock_cls, mock_dash):
        """doctree auth는 인증 후 계정 대시보드를 출력한다."""
        mock_cls.return_value = self._fake_drive()
        self.assertEqual(cli.main(["auth"]), 0)
        mock_dash.assert_called_once_with(ACCOUNT)

    @patch("doctree.doctree_cli.command_dispatcher.print_account_dashboard")
    @patch("doctree.doctree_cli.command_dispatcher.GoogleDriveManager")
    def test_status_prints_account_dashboard(self, mock_cls, mock_dash):
        """doctree status는 계정·용량 대시보드를 출력한다."""
        mock_cls.return_value = self._fake_drive()
        self.assertEqual(cli.main(["status"]), 0)
        mock_dash.assert_called_once_with(ACCOUNT)

    @patch("doctree.doctree_cli.command_dispatcher.print_account_dashboard")
    @patch("doctree.doctree_cli.command_dispatcher.GoogleDriveManager")
    def test_auth_and_status_fail_without_service(self, mock_cls, mock_dash):
        """인증이 안 되면 auth·status 모두 0이 아닌 코드로 끝난다."""
        mock_cls.return_value = self._fake_drive(service=False)
        for cmd in ("auth", "status"):
            self.assertNotEqual(cli.main([cmd]), 0)
        mock_dash.assert_not_called()


class TestCredentialLocation(unittest.TestCase):
    @patch("doctree.doctree_cli.command_dispatcher.print_account_dashboard")
    @patch("doctree.doctree_cli.command_dispatcher.GoogleDriveManager")
    def test_auth_status_use_existing_credentials(self, mock_cls, _dash):
        """auth·status는 기존 credentials.json 경로로 GoogleDriveManager를 만든다."""
        mock_cls.return_value = MagicMock(service=None)
        for cmd in ("auth", "status"):
            mock_cls.reset_mock()
            cli.main([cmd])
            mock_cls.assert_called_once_with(
                service_account_file=GOOGLE_SERVICE_ACCOUNT_FILE,
                folder_id=GOOGLE_DRIVE_FOLDER_ID,
            )

    @patch("doctree.drive_sync.drive_pdf_fetcher.GOOGLE_API_AVAILABLE", False)
    def test_token_path_unchanged(self):
        """token.json 위치는 저장소 루트 그대로라 재로그인이 필요 없다."""
        drive = cli.build_drive()
        self.assertIsInstance(drive, GoogleDriveManager)
        self.assertEqual(Path(drive.token_file).resolve(), (BASE_DIR / "token.json").resolve())
        self.assertEqual(Path(drive.service_account_file).resolve(), (BASE_DIR / "credentials.json").resolve())


def _rec(id_, name, folder="contents", size=1_000_000, sha256=None):
    return {
        "id": id_,
        "name": name,
        "folder_path": folder,
        "size_bytes": size,
        "sha256": sha256 if sha256 is not None else f"sha256-{id_}",
        "modified_time": "2026-01-01T00:00:00Z",
        "web_view_link": f"https://drive/{id_}",
    }


def _console(out):
    return patch("doctree.doctree_cli.command_dispatcher.console", Console(file=out, width=200, color_system=None))


RECORDS = [_rec("a", "종만북-1권.pdf"), _rec("b", "모르는책.pdf")]


class UpdateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snapshots = self.tmp / "snapshots"
        self.cats = self.tmp / "category_tree.yaml"
        self.assigns = self.tmp / "content_category_map.yaml"
        self.cats.write_text(textwrap.dedent(CATEGORIES_YAML), encoding="utf-8")
        self.assigns.write_text(textwrap.dedent(ASSIGNMENTS_YAML), encoding="utf-8")
        p_cats = patch("doctree.doctree_cli.command_dispatcher.CATEGORY_TREE_FILE", str(self.cats))
        p_assigns = patch("doctree.doctree_cli.command_dispatcher.CONTENT_CATEGORY_MAP_FILE", str(self.assigns))
        p_now = patch("doctree.doctree_cli.command_dispatcher.now_kst", return_value=NOW)
        p_cats.start()
        p_assigns.start()
        p_now.start()
        self.addCleanup(p_cats.stop)
        self.addCleanup(p_assigns.stop)
        self.addCleanup(p_now.stop)

    def _drive(self, records=RECORDS, service=True, account=ACCOUNT, error=None):
        drive = MagicMock()
        drive.service = MagicMock() if service else None
        drive.get_account_and_quota.return_value = account
        if error is not None:
            drive.list_pdf_files.side_effect = error
        else:
            drive.list_pdf_files.return_value = list(records)
        return drive

    def run_update(self, drive, *extra):
        out = io.StringIO()
        with patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=drive), \
                _console(out), contextlib.redirect_stdout(out):
            code = cli.main(["update", "--snapshots-dir", str(self.snapshots), *extra])
        return code, out.getvalue()

    def snapshot_files(self):
        if not self.snapshots.exists():
            return {}
        return {str(p.relative_to(self.snapshots)): p.read_bytes() for p in self.snapshots.rglob("*") if p.is_file()}


class TestUpdateCommand(UpdateTestBase):
    def test_update_listed_in_help(self):
        """doctree --help에 update 명령이 보인다."""
        self.assertIn("update", cli.build_parser().format_help())

    def test_update_writes_only_snapshot_json(self):
        """update는 조회 → 분류 → 점검 → snapshots/YYYY-MM-DD.json만 저장하고 .md 파일은 만들지 않는다."""
        code, out = self.run_update(self._drive())
        self.assertEqual(code, 0, out)
        snap = self.snapshots / "2026-10-21.json"
        self.assertTrue(snap.exists())
        self.assertEqual(list(self.snapshots.glob("*.md")), [])
        self.assertIn("종만북-1권.pdf", snap.read_text(encoding="utf-8"))

    def test_default_snapshots_dir_from_settings(self):
        """--snapshots-dir가 없으면 settings.SNAPSHOTS_DIR에 쓴다."""
        out = io.StringIO()
        with patch("doctree.doctree_cli.command_dispatcher.SNAPSHOTS_DIR", str(self.snapshots)), \
                patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=self._drive()), \
                _console(out):
            self.assertEqual(cli.main(["update"]), 0)
        self.assertTrue((self.snapshots / "2026-10-21.json").exists())

    def test_console_summary(self):
        """성공하면 총 권수·분류/미분류·경고 수·변경 요약·스냅샷 경로를 콘솔에 요약한다."""
        code, out = self.run_update(self._drive())
        self.assertEqual(code, 0)
        flat = " ".join(out.split())
        self.assertIn("총 2권", flat)
        self.assertIn("분류 1권", flat)
        self.assertIn("미분류 1권", flat)
        self.assertIn("경고 0건", flat)
        self.assertIn("최초 등록 2권", flat)
        self.assertIn("2026-10-21.json", flat)

    def test_output_paths_not_wrapped(self):
        """좁은 터미널에서도 스냅샷 경로가 줄바꿈으로 잘리지 않아 복사할 수 있다."""
        out = io.StringIO()
        with patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=self._drive()), \
                patch("doctree.doctree_cli.command_dispatcher.console", Console(file=out, width=30, color_system=None)):
            self.assertEqual(cli.main(["update", "--snapshots-dir", str(self.snapshots)]), 0)
        self.assertIn(str(self.snapshots / "2026-10-21.json"), out.getvalue())

    def test_second_run_reports_no_changes(self):
        """같은 목록으로 다시 돌리면 '변경 없음'으로 요약한다."""
        self.run_update(self._drive())
        code, out = self.run_update(self._drive())
        self.assertEqual(code, 0)
        self.assertIn("변경 없음", out)

    def test_strict_succeeds_when_all_classified(self):
        """미분류가 없으면 --strict에서도 정상 갱신된다."""
        code, out = self.run_update(self._drive(records=[_rec("a", "종만북-1권.pdf")]), "--strict")
        self.assertEqual(code, 0, out)


class TestUpdateFailureKeepsSnapshots(UpdateTestBase):
    """인증·조회·분류 설정·엄격 모드 실패 시 기존 스냅샷을 덮어쓰지 않고 오류로 끝난다."""

    def setUp(self):
        super().setUp()
        code, out = self.run_update(self._drive())
        self.assertEqual(code, 0, out)
        self.before = self.snapshot_files()
        self.assertIn("2026-10-21.json", self.before)
        patcher = patch("doctree.doctree_cli.command_dispatcher.now_kst", return_value=NOW + timedelta(days=1))
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_untouched(self, code, out, expect):
        self.assertNotEqual(code, 0)
        self.assertIn(expect, out)
        self.assertEqual(self.snapshot_files(), self.before)

    def test_auth_failure(self):
        """인증 실패(service None)면 아무것도 쓰지 않고 실패한다."""
        code, out = self.run_update(self._drive(service=False))
        self.assert_untouched(code, out, "인증")

    def test_listing_error(self):
        """목록 조회 API 오류면 아무것도 쓰지 않고 실패한다."""
        code, out = self.run_update(self._drive(error=DriveListError("boom")))
        self.assert_untouched(code, out, "조회")

    def test_zero_records(self):
        """인증은 됐지만 PDF가 0개면 빈 스냅샷 방지를 위해 실패로 본다."""
        code, out = self.run_update(self._drive(records=[]))
        self.assert_untouched(code, out, "0개")

    def test_category_config_error(self):
        """분류 설정 오류(ClassificationError)면 아무것도 쓰지 않고 실패한다."""
        self.assigns.write_text(
            "schema_version: 1\ncontents:\n  - file_id: a\n    title: x\n    category_id: unknown.cat\n",
            encoding="utf-8",
        )
        code, out = self.run_update(self._drive())
        self.assert_untouched(code, out, "unknown.cat")

    def test_strict_blocks_on_unclassified(self):
        """--strict에서 미분류 도서가 있으면 스냅샷을 저장하지 않고 실패한다."""
        code, out = self.run_update(self._drive(), "--strict")
        self.assert_untouched(code, out, "미분류")

    def test_first_run_failure_creates_nothing(self):
        """최초 실행에서 실패해도 snapshots 디렉터리에 아무것도 만들지 않는다."""
        empty = self.tmp / "fresh"
        out = io.StringIO()
        with patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=self._drive(service=False)), \
                _console(out), contextlib.redirect_stdout(out):
            code = cli.main(["update", "--snapshots-dir", str(empty)])
        self.assertNotEqual(code, 0)
        self.assertFalse(empty.exists())


@pytest.fixture
def local_files(tmp_path):
    categories = tmp_path / "category_tree.yaml"
    assignments = tmp_path / "content_category_map.yaml"
    snapshot = tmp_path / "snapshot.json"
    categories.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "categories": [
                    {
                        "id": "cs",
                        "name": "컴퓨터 과학 기초",
                        "subcategories": [
                            {"id": "cs.algorithms", "name": "알고리즘·자료구조"},
                            {"id": "cs.systems", "name": "운영체제"},
                        ],
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assignments.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contents": [
                    {"file_id": "known", "title": "원래 제목", "category_id": "cs.algorithms"}
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    snapshot.write_text(
        json.dumps(
            {
                "count": 1,
                "records": [{"id": "known", "name": "다른 이름.pdf", "folder_path": "새 폴더"}],
            }
        ),
        encoding="utf-8",
    )
    return categories, assignments, snapshot


def command(files, *arguments):
    categories, assignments, snapshot = files
    return cli.main([
        "classify",
        "--categories",
        str(categories),
        "--assignments",
        str(assignments),
        "--snapshot",
        str(snapshot),
        *map(str, arguments),
    ])


def test_strict_classify_prints_fixed_categories_without_authentication(local_files, capsys):
    originals = [path.read_bytes() for path in local_files]
    with patch("doctree.doctree_cli.command_dispatcher.build_drive") as drive:
        assert command(local_files, "--strict") == 0
        drive.assert_not_called()

    out = capsys.readouterr().out
    assert "알고리즘·자료구조 (1)" in out
    assert "운영체제 (0)" in out
    assert "다른 이름.pdf" in out
    assert "미분류 콘텐츠가 없습니다." in out
    assert [path.read_bytes() for path in local_files] == originals


def test_strict_failure_on_unclassified(local_files, capsys):
    local_files[2].write_text(
        json.dumps({"records": [{"id": "new", "name": "신규 도서.pdf"}]}),
        encoding="utf-8",
    )

    assert command(local_files, "--strict") == 1
    assert "미분류" in capsys.readouterr().out


def test_non_strict_review_shows_unknown_file_outside_tree(local_files, capsys):
    local_files[2].write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "new",
                        "name": "원래 제목",
                        "folder_path": "컴퓨터 과학 기초/알고리즘·자료구조",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    originals = [path.read_bytes() for path in local_files[:2]]

    assert command(local_files) == 0
    out = capsys.readouterr().out
    tree, review = out.split("[미분류·검토 대기]")
    assert "미분류 1권" in tree
    assert "알고리즘·자료구조 (0)" in tree
    assert "원래 제목" not in tree
    assert "원래 제목" in review
    assert [path.read_bytes() for path in local_files[:2]] == originals


@pytest.mark.parametrize(
    "text",
    [
        "[invalid JSON",
        "null",
        "[]",
        "{}",
        '{"records": {}}',
        '{"count": 1, "records": []}',
        '{"count": false, "records": []}',
        '{"records": [{"id": "known", "name": "one"}, {"id": "known", "name": "two"}]}',
        '{"records": [{"name": "missing ID"}]}',
    ],
)
def test_invalid_snapshot_fails_cleanly(local_files, text, capsys):
    local_files[2].write_text(text, encoding="utf-8")

    assert command(local_files) == 1
    assert "분류 검증 실패" in capsys.readouterr().out


def test_invalid_assignment_fails_even_without_strict(local_files, capsys):
    assignments = yaml.safe_load(local_files[1].read_text(encoding="utf-8"))
    assignments["contents"][0]["category_id"] = "invented.category"
    local_files[1].write_text(yaml.safe_dump(assignments), encoding="utf-8")

    assert command(local_files) == 1
    assert "invented.category" in capsys.readouterr().out


def test_default_settings_resolve_outside_the_working_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    # 실제 배정표(config/content_category_map.yaml)는 개인 Drive 파일 ID라 .gitignore 대상이므로 예시 파일로 대체한다.
    monkeypatch.setattr(
        "doctree.doctree_cli.command_dispatcher.CONTENT_CATEGORY_MAP_FILE",
        str(BASE_DIR / "config" / "content_category_map.example.yaml"),
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text('{"records": []}', encoding="utf-8")

    assert cli.main(["classify", "--snapshot", str(snapshot), "--strict"]) == 0
    assert "컴퓨터 과학 기초" in capsys.readouterr().out


def test_classify_uses_latest_snapshot_when_flag_omitted(local_files, tmp_path, capsys):
    categories, assignments, snapshot = local_files
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    (snap_dir / "2026-10-27.json").write_bytes(snapshot.read_bytes())

    with patch("doctree.doctree_cli.command_dispatcher.SNAPSHOTS_DIR", str(snap_dir)):
        assert (
            cli.main([
                "classify",
                "--categories",
                str(categories),
                "--assignments",
                str(assignments),
                "--strict",
            ])
            == 0
        )
    assert "다른 이름.pdf" in capsys.readouterr().out


def test_migrated_content_keeps_subject_even_when_stored_in_another_subjects_folder():
    catalog = load_catalog(
        BASE_DIR / "config/category_tree.yaml",
        BASE_DIR / "config/content_category_map.example.yaml",
    )
    assignment = next(content for content in catalog.assignments if content.title == "A Tour of C++")
    result = classify_records(
        [{"id": assignment.file_id, "name": "new name.pdf", "folder_path": "Algorithms & Data Structures"}],
        catalog,
        strict=True,
    )

    assert result.contents[0].category_id == "languages.c-cpp"


def test_now_returns_tz_aware_and_classify_without_snapshot_in_empty_dir_fails(local_files, tmp_path, capsys):
    now_val = cli.now_kst()
    assert now_val.tzinfo is not None

    categories, assignments, _ = local_files
    empty_snaps = tmp_path / "empty_snapshots"
    with patch("doctree.doctree_cli.command_dispatcher.SNAPSHOTS_DIR", str(empty_snaps)):
        code = cli.main([
            "classify",
            "--categories",
            str(categories),
            "--assignments",
            str(assignments),
        ])
    assert code == 1
    assert "저장된 스냅샷이 없습니다" in capsys.readouterr().out


def test_update_vt_checked_summary_and_strict_vt_block(tmp_path):
    from doctree.pdf_threat_audit.threat_intel_client import VerdictStatus, VtVerdict, save_vt_cache

    cats = tmp_path / "category_tree.yaml"
    assigns = tmp_path / "content_category_map.yaml"
    snaps = tmp_path / "snapshots"
    vt_cache = tmp_path / ".vt_cache.json"
    cats.write_text(textwrap.dedent(CATEGORIES_YAML), encoding="utf-8")
    assigns.write_text(textwrap.dedent(ASSIGNMENTS_YAML), encoding="utf-8")
    save_vt_cache(
        vt_cache,
        {"sha256-a": VtVerdict(sha256="sha256-a", status=VerdictStatus.MALICIOUS, malicious=4)},
    )

    drive = MagicMock()
    drive.service = MagicMock()
    drive.list_pdf_files.return_value = [_rec("a", "종만북-1권.pdf", sha256="sha256-a")]

    out_strict = io.StringIO()
    with patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=drive), \
            patch("doctree.doctree_cli.command_dispatcher.VT_CACHE_FILE", str(vt_cache)), \
            _console(out_strict):
        code_strict = cli.main([
            "update",
            "--snapshots-dir", str(snaps),
            "--categories", str(cats),
            "--assignments", str(assigns),
            "--strict",
        ])
    assert code_strict == 1
    assert "VirusTotal 위협 감지 1건: 종만북-1권.pdf" in out_strict.getvalue()
    assert not snaps.exists()

    # --strict가 아닐 때는 VT 확인 건수를 출력하고 스냅샷을 정상 기록한다
    out_non_strict = io.StringIO()
    with patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=drive), \
            patch("doctree.doctree_cli.command_dispatcher.VT_CACHE_FILE", str(vt_cache)), \
            patch("doctree.doctree_cli.command_dispatcher.now_kst", return_value=NOW), \
            _console(out_non_strict):
        code_ok = cli.main([
            "update",
            "--snapshots-dir", str(snaps),
            "--categories", str(cats),
            "--assignments", str(assigns),
        ])
    assert code_ok == 0
    assert "VirusTotal(SHA-256) 확인: 1건 · 위협 1건" in out_non_strict.getvalue()


def test_default_local_content_dirs_and_audit_all_branches_and_main_entrypoint(tmp_path, capsys):
    import hashlib
    import runpy
    import sys

    from doctree.pdf_threat_audit.threat_intel_client import VerdictStatus, VtVerdict, save_vt_cache

    # 1) 존재하지 않는 워크스페이스 경로
    assert cli.discover_workspace_content_dirs(tmp_path / "no_such_ws") == []

    # 2) 워크스페이스 내 숨김 폴더(.hidden/contents) 제외 및 정상 프로젝트 contents/ 탐색
    ws = tmp_path / "workspace"
    (ws / ".hidden" / "contents").mkdir(parents=True)
    (ws / "proj_without_contents").mkdir(parents=True)
    proj_contents = ws / "proj_a" / "contents"
    proj_contents.mkdir(parents=True)
    assert cli.discover_workspace_content_dirs(ws) == [proj_contents.resolve()]

    # 3) 손상된 로컬 PDF (헤더·EOF 모두 없고 위험 태그는 없으나 VT 캐시에서 악성 판정)
    corrupt_pdf = proj_contents / "corrupt.pdf"
    corrupt_bytes = b"NOT_A_PDF_AT_ALL_12345"
    corrupt_pdf.write_bytes(corrupt_bytes)
    corrupt_sha = hashlib.sha256(corrupt_bytes).hexdigest()

    drive_mal_sha = "f" * 64
    vt_cache = tmp_path / ".vt_cache.json"
    save_vt_cache(
        vt_cache,
        {
            corrupt_sha: VtVerdict(sha256=corrupt_sha, status=VerdictStatus.MALICIOUS, malicious=3),
            drive_mal_sha: VtVerdict(sha256=drive_mal_sha, status=VerdictStatus.MALICIOUS, malicious=5),
        },
    )

    snaps_dir = tmp_path / "snaps"
    snaps_dir.mkdir()

    # 스냅샷 폴더가 비어 있고 --snapshot 및 paths 인자를 생략한 비엄격(non-strict) 실행
    with patch("doctree.doctree_cli.command_dispatcher.WORKSPACE_DIR", ws):
        code_empty_snap = cli.main([
            "audit",
            "--snapshots-dir", str(snaps_dir),
            "--vt-cache", str(vt_cache),
        ])
    assert code_empty_snap == 0
    out1 = capsys.readouterr().out
    assert "PDF 헤더(%PDF-) 누락" in out1
    assert "PDF 종료(%%EOF) 누락/손상" in out1
    assert "VT=malicious(3)" in out1

    # 최신 스냅샷 파일이 존재할 때 자동 로드 및 드라이브 VT 경고 출력 검증
    (snaps_dir / "2026-10-27.json").write_text(
        json.dumps({
            "schema_version": 1,
            "date": "2026-10-27",
            "generated_at": "2026-10-27T00:00:00+09:00",
            "total": 1,
            "records": [{
                "id": "mal1",
                "name": "bad_drive.pdf",
                "folder_path": "",
                "size_bytes": 100,
                "sha256": drive_mal_sha,
                "modified_time": "",
                "web_view_link": "",
            }],
        }),
        encoding="utf-8",
    )
    with patch("doctree.doctree_cli.command_dispatcher.WORKSPACE_DIR", ws):
        code_latest = cli.main([
            "audit",
            "--snapshots-dir", str(snaps_dir),
            "--vt-cache", str(vt_cache),
            "--strict",
        ])
    assert code_latest == 1
    out2 = capsys.readouterr().out
    assert "[드라이브 VT 경고] bad_drive.pdf" in out2

    # 4) __main__ 진입점 실행 검증 (로컬 단일 파일만 지정하여 빠르게 실행)
    with patch.object(
        sys,
        "argv",
        ["doctree", "audit", str(corrupt_pdf), "--snapshots-dir", str(snaps_dir), "--vt-cache", str(vt_cache)],
    ):
        with pytest.raises(SystemExit) as exc_info:
            runpy.run_path(str(Path(cli.__file__)), run_name="__main__")
        assert exc_info.value.code == 0


@pytest.mark.integration
def test_integration_multi_day_drive_snapshot_classification_and_linter_pipeline(tmp_path):
    """[Integration] 실제 config/category_tree.yaml(16개 대분야·55개 세부분야)과 다중 모듈(스냅샷 추적·분류·중복/파일명 린터·트리 렌더러)의 다일차(Day 1 → Day 2) 통합 파이프라인을 검증한다."""
    from doctree.content_taxonomy.duplicate_and_naming_linter import WarningKind, run_checks
    from doctree.doctree_cli.terminal_tree_renderer import format_classification_summary
    from doctree.drive_sync.snapshot_diff_tracker import change_summary, record_run

    real_categories = BASE_DIR / "config" / "category_tree.yaml"
    assignments_file = tmp_path / "content_category_map.yaml"
    snapshots_dir = tmp_path / "snapshots"

    assignments_file.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contents": [
                    {"file_id": "book-1", "title": "종만북 1권", "category_id": "cs.algorithms"},
                    {"file_id": "book-2", "title": "코드 컴플리트 2판", "category_id": "software.clean-code"},
                    {"file_id": "book-3", "title": "친절한 SQL 튜닝", "category_id": "databases.oracle-tuning"},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    catalog = load_catalog(real_categories, assignments_file)
    assert len(catalog.categories) == 16

    # Day 1 (2026-10-26): 최초 3권 스냅샷 기록
    day1_records = [
        _rec("book-1", "종만북-1권.pdf", folder="CS/알고리즘", sha256="sha-b1"),
        _rec("book-2", "Code-Complete-2.pdf", folder="SW/클린코드", sha256="sha-b2"),
        _rec("book-3", "친절한-SQL-튜닝.pdf", folder="DB/오라클", sha256="sha-b3"),
    ]
    run_day1 = record_run(day1_records, snapshots_dir, datetime(2026, 10, 26, 10, 0, tzinfo=KST))
    assert run_day1.first_run is True
    assert change_summary(run_day1) == "최초 등록 3권"
    cls_day1 = classify_records(day1_records, catalog, strict=True)
    assert len(cls_day1.unclassified) == 0

    # Day 2 (2026-10-27):
    # - book-1: 이름 변경 + 폴더 이동
    # - book-2: 내용(sha256) 개정판으로 변경
    # - book-3: 삭제됨
    # - book-4: 신규 추가(미분류) + book-1과 동일한 sha256(완전 중복) + 'Copy of ' 접두어 및 확장자 누락
    day2_records = [
        _rec("book-1", "알고리즘-문제해결전략-1권.pdf", folder="CS/심화", sha256="sha-b1"),
        _rec("book-2", "Code-Complete-2.pdf", folder="SW/클린코드", sha256="sha-b2-rev2"),
        _rec("book-4", "Copy of 알고리즘-문제해결전략-1권", folder="백업", sha256="sha-b1"),
    ]
    run_day2 = record_run(day2_records, snapshots_dir, datetime(2026, 10, 27, 10, 0, tzinfo=KST))
    assert run_day2.first_run is False
    assert change_summary(run_day2) == "추가 1 · 삭제 1 · 이름 변경 1 · 폴더 이동 1 · 내용 변경 1"

    warnings_day2 = run_checks(day2_records)
    assert [w.kind for w in warnings_day2] == [
        WarningKind.EXACT_DUPLICATE,
        WarningKind.MISSING_PDF_EXTENSION,
        WarningKind.COPY_OF_PREFIX,
    ]

    cls_day2 = classify_records(day2_records, catalog, strict=False)
    assert [c.record["id"] for c in cls_day2.unclassified] == ["book-4"]
    rendered = format_classification_summary(cls_day2)
    assert "파일 3개 · 분류 2권 · 미분류 1권" in rendered
    assert "알고리즘-문제해결전략-1권.pdf" in rendered
    assert "- Copy of 알고리즘-문제해결전략-1권 (id=book-4, path=백업/Copy of 알고리즘-문제해결전략-1권)" in rendered


@pytest.mark.e2e
def test_e2e_cli_binary_subprocess_classify_audit_and_update_guard(tmp_path):
    """[E2E] cli_golden_transcripts.yaml 골든 트랜스크립트 명세를 로드하여 실제 설치된 CLI 실행 파일(.venv/bin/doctree) 서브프로세스의 표준입출력·종료코드·파일 무결성을 검증한다."""
    import hashlib
    import os
    import subprocess

    from doctree.pdf_threat_audit.threat_intel_client import VerdictStatus, VtVerdict, save_vt_cache

    doctree_bin = ROOT / ".venv" / "bin" / "doctree"
    assert doctree_bin.is_file()
    transcripts = yaml.safe_load(
        (Path(__file__).with_name("cli_golden_transcripts.yaml")).read_text(encoding="utf-8")
    )["transcripts"]

    # 격리된 E2E 워크스페이스 구성
    ws_dir = tmp_path / "freeride_ws"
    proj_clean = ws_dir / "databases" / "contents"
    proj_threat = ws_dir / "security" / "contents"
    proj_clean.mkdir(parents=True)
    proj_threat.mkdir(parents=True)

    clean_pdf = proj_clean / "친절한-SQL-튜닝.pdf"
    clean_bytes = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
    clean_pdf.write_bytes(clean_bytes)
    clean_sha = hashlib.sha256(clean_bytes).hexdigest()

    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    snap_file = snapshots_dir / "2026-10-27.json"
    snap_payload = {
        "taken_at": "2026-10-27T09:00:00+09:00",
        "count": 1,
        "records": [
            {
                "id": "1ZyXwVuTsRqPoNmLkJiHgFeDcBa543210",
                "name": "친절한 SQL 튜닝.pdf",
                "folder_path": "DB",
                "size_bytes": len(clean_bytes),
                "sha256": clean_sha,
                "modified_time": "2026-10-27T00:00:00Z",
                "web_view_link": "https://drive.google.com/file/d/1ZyXwVuTsRqPoNmLkJiHgFeDcBa543210/view",
            }
        ],
    }
    snap_file.write_text(json.dumps(snap_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    original_snap_bytes = snap_file.read_bytes()

    unclassified_snap = tmp_path / "unclassified_snapshot.json"
    unclassified_snap.write_text(
        json.dumps(
            {
                "taken_at": "2026-10-27T09:30:00+09:00",
                "count": 2,
                "records": [
                    *snap_payload["records"],
                    {
                        "id": "unregistered-999",
                        "name": "미등록신규도서.pdf",
                        "folder_path": "신규폴더",
                        "size_bytes": 1024,
                        "sha256": "a" * 64,
                        "modified_time": "2026-10-27T00:00:00Z",
                        "web_view_link": "",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    vt_cache = tmp_path / ".vt_cache.json"
    save_vt_cache(vt_cache, {clean_sha: VtVerdict(sha256=clean_sha, status=VerdictStatus.CLEAN, harmless=72)})

    env = {
        **os.environ,
        "DOCTREE_SNAPSHOTS_DIR": str(snapshots_dir),
        "DOCTREE_CATEGORY_TREE_FILE": str(ROOT / "config" / "category_tree.yaml"),
        "DOCTREE_CONTENT_CATEGORY_MAP_FILE": str(ROOT / "config" / "content_category_map.example.yaml"),
        "DOCTREE_PROJECT_LAYOUT_PROFILES_FILE": str(ROOT / "config" / "project_layout_profiles.yaml"),
        "DOCTREE_VT_CACHE_FILE": str(vt_cache),
        "FREERIDE_WORKSPACE_DIR": str(ws_dir),
        "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "missing_credentials.json"),
        "GOOGLE_DRIVE_FOLDER_ID": "",
        "VIRUSTOTAL_API_KEY": "",
    }

    def _assert_transcript(spec_name: str):
        spec = transcripts[spec_name]
        cmd_args = [
            arg.format(
                unclassified_snapshot=str(unclassified_snap),
                scaffold_target=str(ws_dir / "handcrafted-java"),
                clean_pdf=str(clean_pdf),
                parsed_book_dir=str(ws_dir / "databases" / "content_data" / "친절한-SQL-튜닝"),
            )
            for arg in spec["command"]
        ]
        res = subprocess.run(
            [str(doctree_bin), *cmd_args],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == spec["exit_code"], f"[{spec_name}] stderr={res.stderr} stdout={res.stdout}"
        for expected_line in spec["expected_stdout_contains"]:
            assert expected_line in res.stdout, f"[{spec_name}] missing {expected_line!r} in:\n{res.stdout}"

    # E2E 1) --version, --help, help, auth, status, classify(엄격/비엄격/미분류 차단), audit(클린), sync, scaffold, parse, practice 골든 트랜스크립트 검증
    for spec_name in (
        "version_flag",
        "help_all_commands",
        "help_subcommand_topic",
        "auth_without_credentials",
        "status_without_credentials",
        "classify_strict_clean",
        "classify_non_strict_unclassified_review",
        "classify_strict_unclassified_rejected",
        "audit_strict_clean",
        "sync_3way_comparison",
        "scaffold_handcrafted_project",
        "parse_verify_pdf",
        "practice_generate_katas",
    ):
        _assert_transcript(spec_name)

    # E2E 2) 로컬 워크스페이스에 악성 스크립트(/JavaScript) PDF 유입 후 audit_strict_infected_pdf 검증
    infected_pdf = proj_threat / "malicious_autoopen.pdf"
    infected_pdf.write_bytes(b"%PDF-1.7\n<< /OpenAction << /S /JavaScript /JS (alert(1)) >> >>\n%%EOF\n")
    _assert_transcript("audit_strict_infected_pdf")

    # E2E 3) update_without_credentials_preserves_snapshot 검증 및 스냅샷 바이트 무결성 대조
    _assert_transcript("update_without_credentials_preserves_snapshot")
    assert snap_file.read_bytes() == original_snap_bytes


# ==============================================================================
# BDD 실행 가능한 명세 (pytest-bdd: doctree_workflows.feature 바인딩)
# ==============================================================================

FEATURE_FILE = str(Path(__file__).with_name("doctree_workflows.feature"))


@pytest.mark.integration
@scenario(FEATURE_FILE, "[경계 1+2+3] 드라이브 조회부터 분류·린팅·VirusTotal 감사·스냅샷 원자적 저장까지의 전체 Update 파이프라인")
def test_bdd_integration_full_update_pipeline():
    pass


@pytest.mark.integration
@scenario(FEATURE_FILE, "[경계 2+3] 다일차(Day 1 -> Day 2) 스냅샷 변경 추적 및 중복·파일명 점검 통합 파이프라인")
def test_bdd_integration_multi_day_pipeline():
    pass


@pytest.mark.e2e
@scenario(FEATURE_FILE, "[경계 1+2+4] 로컬 워크스페이스에 위험 태그(/JavaScript) PDF 유입 시 엄격 모드 E2E 감사 차단")
def test_bdd_e2e_audit_blocks_javascript_pdf():
    pass


@pytest.mark.e2e
@scenario(FEATURE_FILE, "[경계 2+4] 자격 증명 없는 환경에서 doctree update 실행 시 기존 스냅샷 무결성 보존 E2E 가드")
def test_bdd_e2e_update_preserves_snapshot():
    pass


@pytest.mark.integration
@scenario(FEATURE_FILE, "[Step 1~4 전 과정 통합] 3-Way 동기화·프로젝트 스캐폴딩·PDF 파싱 및 동일성 검증·실습(Kata) 생성 파이프라인")
def test_bdd_integration_steps_1_to_4_full_pipeline():
    pass


@pytest.fixture
def bdd_ctx(tmp_path):
    return {"tmp_path": tmp_path}


@given(parsers.parse('구글 드라이브에 정상 도서("{pdf_name}")가 존재하고 VirusTotal 클린 캐시가 준비되어 있을 때'))
def step_given_drive_and_vt_for_update(bdd_ctx, pdf_name):
    from doctree.pdf_threat_audit.threat_intel_client import VerdictStatus, VtVerdict, save_vt_cache

    tmp_path = bdd_ctx["tmp_path"]
    cats = tmp_path / "category_tree.yaml"
    assigns = tmp_path / "content_category_map.yaml"
    snaps = tmp_path / "snapshots"
    vt_cache = tmp_path / ".vt_cache.json"
    cats.write_text(textwrap.dedent(CATEGORIES_YAML), encoding="utf-8")
    assigns.write_text(textwrap.dedent(ASSIGNMENTS_YAML), encoding="utf-8")
    save_vt_cache(
        vt_cache,
        {"sha256-a": VtVerdict(sha256="sha256-a", status=VerdictStatus.CLEAN, harmless=70)},
    )
    drive = MagicMock()
    drive.service = MagicMock()
    drive.list_pdf_files.return_value = [_rec("a", pdf_name, sha256="sha256-a")]

    bdd_ctx["cats"] = cats
    bdd_ctx["assigns"] = assigns
    bdd_ctx["snaps"] = snaps
    bdd_ctx["vt_cache"] = vt_cache
    bdd_ctx["drive"] = drive


@when(parsers.parse('사용자가 엄격 모드("{cmd_str}")로 스냅샷 갱신 파이프라인을 실행하면'))
def step_when_run_update_pipeline(bdd_ctx, cmd_str):
    assert cmd_str == "doctree update --strict"
    out = io.StringIO()
    with patch("doctree.doctree_cli.command_dispatcher.build_drive", return_value=bdd_ctx["drive"]), \
            patch("doctree.doctree_cli.command_dispatcher.VT_CACHE_FILE", str(bdd_ctx["vt_cache"])), \
            patch("doctree.doctree_cli.command_dispatcher.now_kst", return_value=NOW), \
            _console(out):
        code = cli.main([
            "update",
            "--snapshots-dir", str(bdd_ctx["snaps"]),
            "--categories", str(bdd_ctx["cats"]),
            "--assignments", str(bdd_ctx["assigns"]),
            "--strict",
        ])
    bdd_ctx["update_code"] = code
    bdd_ctx["update_out"] = out.getvalue()


@then(parsers.parse('종료 코드는 {expected_code:d}이고 오늘자 스냅샷 파일("{snap_name}")이 원자적으로 생성되어야 하며'))
def step_then_update_snapshot_created(bdd_ctx, expected_code, snap_name):
    assert bdd_ctx["update_code"] == expected_code
    assert (bdd_ctx["snaps"] / snap_name).is_file()


@then(parsers.parse('콘솔 출력에 "{expected_first}"과 "{expected_second}"이 표시되어야 한다'))
def step_then_update_console_contains(bdd_ctx, expected_first, expected_second):
    assert expected_first in bdd_ctx["update_out"]
    assert expected_second in bdd_ctx["update_out"]


@given("1일차(2026-10-26) 드라이브 스냅샷에 정상 도서 3권이 등록되어 있고")
def step_given_day1_snapshot(bdd_ctx):
    from doctree.drive_sync.snapshot_diff_tracker import record_run

    tmp_path = bdd_ctx["tmp_path"]
    assignments_file = tmp_path / "content_category_map.yaml"
    snapshots_dir = tmp_path / "snapshots"
    assignments_file.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contents": [
                    {"file_id": "book-1", "title": "종만북 1권", "category_id": "cs.algorithms"},
                    {"file_id": "book-2", "title": "코드 컴플리트 2판", "category_id": "software.clean-code"},
                    {"file_id": "book-3", "title": "친절한 SQL 튜닝", "category_id": "databases.oracle-tuning"},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    bdd_ctx["catalog"] = load_catalog(BASE_DIR / "config" / "category_tree.yaml", assignments_file)
    bdd_ctx["snapshots_dir"] = snapshots_dir
    day1_records = [
        _rec("book-1", "종만북-1권.pdf", folder="CS/알고리즘", sha256="sha-b1"),
        _rec("book-2", "Code-Complete-2.pdf", folder="SW/클린코드", sha256="sha-b2"),
        _rec("book-3", "친절한-SQL-튜닝.pdf", folder="DB/오라클", sha256="sha-b3"),
    ]
    record_run(day1_records, snapshots_dir, datetime(2026, 10, 26, 10, 0, tzinfo=KST))


@when(parsers.parse('2일차(2026-10-27)에 이름 변경·폴더 이동·개정판 해시 변경·중복 미분류 도서("{dup_name}")가 반영되면'))
def step_when_day2_applied(bdd_ctx, dup_name):
    from doctree.content_taxonomy.duplicate_and_naming_linter import run_checks
    from doctree.drive_sync.snapshot_diff_tracker import change_summary, record_run

    day2_records = [
        _rec("book-1", "알고리즘-문제해결전략-1권.pdf", folder="CS/심화", sha256="sha-b1"),
        _rec("book-2", "Code-Complete-2.pdf", folder="SW/클린코드", sha256="sha-b2-rev2"),
        _rec("book-4", dup_name, folder="백업", sha256="sha-b1"),
    ]
    run_day2 = record_run(day2_records, bdd_ctx["snapshots_dir"], datetime(2026, 10, 27, 10, 0, tzinfo=KST))
    bdd_ctx["summary"] = change_summary(run_day2)
    bdd_ctx["warnings"] = run_checks(day2_records)
    bdd_ctx["classification"] = classify_records(day2_records, bdd_ctx["catalog"], strict=False)


@then(parsers.parse('변경 요약은 "{expected_summary}"이어야 하고'))
def step_then_change_summary(bdd_ctx, expected_summary):
    assert bdd_ctx["summary"] == expected_summary


@then(parsers.parse('린터 경고는 "{expected_kinds}" 순서로 발생해야 하며'))
def step_then_linter_warnings(bdd_ctx, expected_kinds):
    expected = [k.strip() for k in expected_kinds.split(",")]
    assert [str(w.kind) for w in bdd_ctx["warnings"]] == expected


@then(parsers.parse('미분류 검토 대기 목록에 "{expected_id}"가 표시되어야 한다'))
def step_then_unclassified_contains(bdd_ctx, expected_id):
    assert [c.record["id"] for c in bdd_ctx["classification"].unclassified] == [expected_id]


@given(parsers.parse('드라이브 스냅샷에 정상 PDF("{pdf_name}")와 VirusTotal 클린 캐시가 준비되어 있고'))
def step_given_drive_snapshot_and_clean_cache(bdd_ctx, pdf_name):
    import hashlib
    import os

    from doctree.pdf_threat_audit.threat_intel_client import VerdictStatus, VtVerdict, save_vt_cache

    tmp_path = bdd_ctx["tmp_path"]
    ws_dir = tmp_path / "freeride_ws"
    proj_clean = ws_dir / "databases" / "contents"
    proj_clean.mkdir(parents=True, exist_ok=True)

    clean_pdf = proj_clean / pdf_name.replace(" ", "-")
    clean_bytes = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
    clean_pdf.write_bytes(clean_bytes)
    clean_sha = hashlib.sha256(clean_bytes).hexdigest()

    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir(exist_ok=True)
    snap_file = snapshots_dir / "2026-10-27.json"
    snap_payload = {
        "taken_at": "2026-10-27T09:00:00+09:00",
        "count": 1,
        "records": [
            {
                "id": "1ZyXwVuTsRqPoNmLkJiHgFeDcBa543210",
                "name": pdf_name,
                "folder_path": "DB",
                "size_bytes": len(clean_bytes),
                "sha256": clean_sha,
                "modified_time": "2026-10-27T00:00:00Z",
                "web_view_link": "https://drive.google.com/file/d/1ZyXwVuTsRqPoNmLkJiHgFeDcBa543210/view",
            }
        ],
    }
    snap_file.write_text(json.dumps(snap_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    vt_cache = tmp_path / ".vt_cache.json"
    save_vt_cache(vt_cache, {clean_sha: VtVerdict(sha256=clean_sha, status=VerdictStatus.CLEAN, harmless=72)})

    bdd_ctx["ws_dir"] = ws_dir
    bdd_ctx["snap_file"] = snap_file
    bdd_ctx["original_snap_bytes"] = snap_file.read_bytes()
    bdd_ctx["env"] = {
        **os.environ,
        "DOCTREE_SNAPSHOTS_DIR": str(snapshots_dir),
        "DOCTREE_CATEGORY_TREE_FILE": str(ROOT / "config" / "category_tree.yaml"),
        "DOCTREE_CONTENT_CATEGORY_MAP_FILE": str(ROOT / "config" / "content_category_map.example.yaml"),
        "DOCTREE_PROJECT_LAYOUT_PROFILES_FILE": str(ROOT / "config" / "project_layout_profiles.yaml"),
        "DOCTREE_VT_CACHE_FILE": str(vt_cache),
        "FREERIDE_WORKSPACE_DIR": str(ws_dir),
        "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "missing_credentials.json"),
        "GOOGLE_DRIVE_FOLDER_ID": "",
        "VIRUSTOTAL_API_KEY": "",
    }


@given(parsers.parse('로컬 워크스페이스 "{rel_dir}" 폴더에 "{tag}" 자동 실행 태그를 가진 PDF가 유입되었을 때'))
def step_given_infected_local_pdf(bdd_ctx, rel_dir, tag):
    target_dir = bdd_ctx["ws_dir"] / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    infected = target_dir / "infected.pdf"
    infected.write_bytes(f"%PDF-1.7\n<< /OpenAction << /S {tag} /JS (alert(1)) >> >>\n%%EOF\n".encode())


@when(parsers.parse('외부 서브프로세스로 "{cmd_str}" 명령을 실행하면'))
def step_when_run_cli_subprocess(bdd_ctx, cmd_str):
    import subprocess

    parts = cmd_str.split()
    assert parts[0] == "doctree"
    doctree_bin = ROOT / ".venv" / "bin" / "doctree"
    bdd_ctx["proc"] = subprocess.run(
        [str(doctree_bin), *parts[1:]],
        env=bdd_ctx["env"],
        cwd=str(bdd_ctx["tmp_path"]),
        capture_output=True,
        text=True,
        check=False,
    )


@then(parsers.parse("프로세스 종료 코드는 {expected_code:d}이어야 하고"))
def step_then_exit_code(bdd_ctx, expected_code):
    assert bdd_ctx["proc"].returncode == expected_code


@then(parsers.parse('표준 출력에 "{expected_text}"가 출력되어야 하며'))
@then(parsers.parse('표준 출력에 "{expected_text}" 경고가 포함되어야 한다'))
def step_then_stdout_contains(bdd_ctx, expected_text):
    assert expected_text in bdd_ctx["proc"].stdout


@then("기존 스냅샷 파일의 바이트는 변경되지 않고 그대로 보존되어야 한다")
def step_then_snapshot_preserved(bdd_ctx):
    assert bdd_ctx["snap_file"].read_bytes() == bdd_ctx["original_snap_bytes"]


@when('사용자가 워크스페이스에 대해 Step 1부터 Step 4까지의 전체 파이프라인("sync -> scaffold -> parse -> practice")을 실행하면')
def step_when_run_steps_1_to_4(bdd_ctx):
    ws_dir = bdd_ctx["ws_dir"]
    snap_file = bdd_ctx["snap_file"]
    db_proj = ws_dir / "databases"
    buf = io.StringIO()
    with _console(buf):
        c_sync = cli.main(["sync", "--snapshot", str(snap_file), "--workspace-dir", str(ws_dir)])
        c_scaf = cli.main(["scaffold", "handcrafted", str(db_proj)])
        c_parse = cli.main(["parse", str(db_proj), "--verify", "--strict"])
        c_prac = cli.main(["practice", str(db_proj)])
    bdd_ctx["pipeline_codes"] = (c_sync, c_scaf, c_parse, c_prac)
    bdd_ctx["pipeline_out"] = buf.getvalue()


@then("3-Way 동기화에서 양쪽 일치 1권이 감지되고 스캐폴딩·페이지별 파싱 검증(PASS)·실습(Kata) 생성이 모두 종료 코드 0으로 완료되어야 한다")
def step_then_steps_1_to_4_succeeded(bdd_ctx):
    assert bdd_ctx["pipeline_codes"] == (0, 0, 0, 0)
    out = bdd_ctx["pipeline_out"]
    assert "양쪽 일치 1권" in out
    assert "🏗️ 프로젝트 스캐폴딩 완료:" in out
    assert "PASS 1 · WARN 0" in out
    assert "🎯 실습(Kata) 생성 완료:" in out
