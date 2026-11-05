import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from hypothesis import example, given, settings
from hypothesis import strategies as st

from doctree.drive_sync.drive_pdf_fetcher import (
    SCOPES,
    DriveListError,
    GoogleDriveManager,
    _safe_non_negative_int,
    is_pdf_item,
)


class TestGoogleDriveManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.credentials_file = os.path.join(self.temp_dir, "credentials.json")
        # Keep constructor credential/token discovery inside this test's directory.
        env_patch = patch.dict(os.environ, {
            "GOOGLE_SERVICE_ACCOUNT_FILE": self.credentials_file,
            "GOOGLE_DRIVE_FOLDER_ID": "",
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init_without_credentials(self):
        non_existent = os.path.join(self.temp_dir, "non_existent.json")
        drive = GoogleDriveManager(service_account_file=non_existent, folder_id="folder_123")
        self.assertIsNone(drive.service)
        self.assertEqual(drive.folder_id, "folder_123")

    @patch("doctree.drive_sync.drive_pdf_fetcher.GOOGLE_API_AVAILABLE", True)
    @patch("doctree.drive_sync.drive_pdf_fetcher.service_account")
    @patch("doctree.drive_sync.drive_pdf_fetcher.build")
    @patch("doctree.drive_sync.drive_pdf_fetcher.Path.exists", return_value=False)
    @patch("os.path.exists")
    @patch("builtins.open")
    @patch("json.load")
    def test_init_with_credentials(self, mock_json_load, mock_open, mock_exists, _mock_path_exists, mock_build, mock_sa):
        mock_exists.return_value = True
        mock_json_load.return_value = {"type": "service_account"}
        mock_creds = MagicMock()
        mock_sa.Credentials.from_service_account_file.return_value = mock_creds
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        drive = GoogleDriveManager(service_account_file=self.credentials_file, folder_id="folder_123")
        self.assertEqual(drive.service, mock_service)
        mock_sa.Credentials.from_service_account_file.assert_called_once_with(
            self.credentials_file, scopes=SCOPES
        )
        mock_build.assert_called_once_with("drive", "v3", credentials=mock_creds)
        mock_open.assert_called_once_with(self.credentials_file, encoding="utf-8")

    def test_list_pdf_files_service_none(self):
        drive = GoogleDriveManager(service_account_file=self.credentials_file)
        drive.service = None
        files = drive.list_pdf_files()
        self.assertEqual(files, [])

    def test_list_pdf_files_success(self):
        drive = GoogleDriveManager(service_account_file=self.credentials_file, folder_id="target_folder")
        mock_service = MagicMock()
        mock_files_resource = MagicMock()
        mock_list_req = MagicMock()
        mock_list_req.execute.return_value = {
            "files": [
                {"id": "file_1", "name": "content1.pdf", "mimeType": "application/pdf", "parents": ["target_folder"], "size": "10485760"},
                {"id": "file_2", "name": "content2.pdf", "mimeType": "application/pdf", "parents": ["target_folder"], "size": "20971520"}
            ]
        }
        mock_files_resource.list.return_value = mock_list_req
        mock_service.files.return_value = mock_files_resource
        drive.service = mock_service

        files = drive.list_pdf_files()
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0]["name"], "content1.pdf")
        self.assertEqual(files[0]["folder_path"], "")
        self.assertEqual(files[1]["id"], "file_2")

    def test_list_pdf_files_nested_folders(self):
        drive = GoogleDriveManager(service_account_file=self.credentials_file, folder_id=None)
        drive.folder_id = None
        mock_service = MagicMock()
        mock_files_resource = MagicMock()
        mock_list_req = MagicMock()
        mock_list_req.execute.return_value = {
            "files": [
                {"id": "folder_root", "name": "책들", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"]},
                {"id": "folder_sub", "name": "책박스9번", "mimeType": "application/vnd.google-apps.folder", "parents": ["folder_root"]},
                {"id": "file_1", "name": "클린코드.pdf", "mimeType": "application/pdf", "parents": ["folder_sub"], "size": "10485760"}
            ]
        }
        mock_files_resource.list.return_value = mock_list_req
        mock_service.files.return_value = mock_files_resource
        drive.service = mock_service

        files = drive.list_pdf_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "클린코드.pdf")
        self.assertEqual(files[0]["folder_path"], "책들/책박스9번")

    def test_get_account_and_quota(self):
        drive = GoogleDriveManager(service_account_file=self.credentials_file)
        mock_service = MagicMock()
        mock_about_req = MagicMock()
        mock_about_req.execute.return_value = {
            "user": {"displayName": "Test User", "emailAddress": "test@example.com"},
            "storageQuota": {"limit": str(100 * 1024**3), "usage": str(50 * 1024**3), "usageInDrive": str(40 * 1024**3)}
        }
        mock_service.about.return_value.get.return_value = mock_about_req
        drive.service = mock_service

        account = drive.get_account_and_quota()
        self.assertIsNotNone(account)
        self.assertEqual(account["name"], "Test User")
        self.assertEqual(account["email"], "test@example.com")
        self.assertEqual(account["limit_gb"], 100.0)
        self.assertEqual(account["usage_gb"], 50.0)
        self.assertEqual(account["usage_percent"], 50.0)

    def test_list_pdf_files_raises_on_api_error(self):
        """목록 API 오류는 빈 목록 대신 DriveListError로 올라온다."""
        drive = GoogleDriveManager(service_account_file=str(Path(self.temp_dir) / "none.json"))
        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = RuntimeError("quota")
        drive.service = service
        with self.assertRaises(DriveListError) as ctx:
            drive.list_pdf_files()
        self.assertIn("quota", str(ctx.exception))

    def test_import_error_fallback_when_google_sdk_missing(self):
        """google-api-python-client / google-auth가 없을 때도 모듈이 ImportError 없이 로드되며 GOOGLE_API_AVAILABLE=False가 된다."""
        import importlib
        import sys

        import doctree.drive_sync.drive_pdf_fetcher as fetcher

        orig_err = fetcher.DriveListError
        orig_mgr = fetcher.GoogleDriveManager
        saved = sys.modules.get("google.oauth2")
        try:
            sys.modules["google.oauth2"] = None  # type: ignore[assignment]
            importlib.reload(fetcher)
            self.assertFalse(fetcher.GOOGLE_API_AVAILABLE)
            self.assertIsNone(fetcher.service_account)
        finally:
            if saved is not None:
                sys.modules["google.oauth2"] = saved
            else:
                sys.modules.pop("google.oauth2", None)
            importlib.reload(fetcher)
            fetcher.DriveListError = orig_err
            fetcher.GoogleDriveManager = orig_mgr

    def test_init_auto_discovers_cwd_credentials_json_and_token_flows(self):
        """현재 작업 디렉터리의 credentials.json 자동 감지, token.json 유효/만료 갱신/파손 폴백, OAuth InstalledAppFlow, 알 수 없는 포맷 및 예외 분기를 검증한다."""
        import json

        old_cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GOOGLE_SERVICE_ACCOUNT_FILE", None)
                os.environ.pop("GOOGLE_DRIVE_FOLDER_ID", None)
                # 1) credentials.json이 없을 때 기본 token.json 경로
                d_no_cred = GoogleDriveManager(service_account_file=None)
                self.assertIsNone(d_no_cred.service_account_file)
                self.assertEqual(d_no_cred.token_file, Path("token.json"))

                # 2) 현재 디렉터리에 credentials.json이 존재할 때 자동 감지 (알 수 없는 형식 또는 비객체 JSON이면 service=None)
                Path("credentials.json").write_text(json.dumps({"unknown_key": 123}), encoding="utf-8")
                d_unknown = GoogleDriveManager(service_account_file=None)
                self.assertEqual(d_unknown.service_account_file, "credentials.json")
                self.assertIsNone(d_unknown.service)

                Path("credentials.json").write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
                self.assertIsNone(GoogleDriveManager(service_account_file="credentials.json").service)

                # 3) credentials.json이 잘못된 JSON일 때 외부 except 블록 처리
                Path("credentials.json").write_text("{corrupt json", encoding="utf-8")
                d_corrupt_cred = GoogleDriveManager(service_account_file="credentials.json")
                self.assertIsNone(d_corrupt_cred.service)

                # 4) token.json이 유효할 때 (즉시 인증 완료)
                Path("token.json").write_text("{}", encoding="utf-8")
                valid_creds = MagicMock(valid=True, expired=False, refresh_token=None)
                with patch("doctree.drive_sync.drive_pdf_fetcher.Credentials.from_authorized_user_file", return_value=valid_creds), \
                        patch("doctree.drive_sync.drive_pdf_fetcher.build", return_value="svc_from_valid_token"):
                    d_tok = GoogleDriveManager(service_account_file="credentials.json")
                    self.assertEqual(d_tok.service, "svc_from_valid_token")

                # 5) token.json이 만료되었으나 refresh_token이 있을 때 (갱신 후 저장)
                expired_creds = MagicMock(valid=False, expired=True, refresh_token="rt_123")
                expired_creds.to_json.return_value = '{"refreshed": true}'
                with patch("doctree.drive_sync.drive_pdf_fetcher.Credentials.from_authorized_user_file", return_value=expired_creds), \
                        patch("doctree.drive_sync.drive_pdf_fetcher.Request"), \
                        patch("doctree.drive_sync.drive_pdf_fetcher.build", return_value="svc_refreshed"):
                    d_ref = GoogleDriveManager(service_account_file="credentials.json")
                    self.assertEqual(d_ref.service, "svc_refreshed")
                    expired_creds.refresh.assert_called_once()
                    self.assertEqual(Path("token.json").read_text(encoding="utf-8"), '{"refreshed": true}')

                # 6) token.json 로드 중 예외 발생 + credentials.json은 존재하지 않을 때
                Path("credentials.json").unlink()
                with patch("doctree.drive_sync.drive_pdf_fetcher.Credentials.from_authorized_user_file", side_effect=ValueError("bad token")):
                    d_missing_cred = GoogleDriveManager(service_account_file="non_existent_creds.json")
                    self.assertIsNone(d_missing_cred.service)

                # 7) token.json이 만료되고 refresh_token도 없을 때 credentials.json(OAuth installed)으로 폴백
                no_rt_creds = MagicMock(valid=False, expired=False, refresh_token=None)
                Path("credentials.json").write_text(json.dumps({"installed": {"client_id": "cid"}}), encoding="utf-8")
                oauth_creds = MagicMock()
                oauth_creds.to_json.return_value = '{"oauth": "ok"}'
                mock_flow = MagicMock()
                mock_flow.run_local_server.return_value = oauth_creds
                with patch("doctree.drive_sync.drive_pdf_fetcher.Credentials.from_authorized_user_file", return_value=no_rt_creds), \
                        patch("doctree.drive_sync.drive_pdf_fetcher.InstalledAppFlow.from_client_secrets_file", return_value=mock_flow), \
                        patch("doctree.drive_sync.drive_pdf_fetcher.build", return_value="svc_oauth"):
                    d_oauth = GoogleDriveManager(service_account_file="credentials.json")
                    self.assertEqual(d_oauth.service, "svc_oauth")
                    self.assertEqual(Path("token.json").read_text(encoding="utf-8"), '{"oauth": "ok"}')
        finally:
            os.chdir(old_cwd)

    def test_get_account_and_quota_edge_cases(self):
        """service가 None이거나, 무제한 스토리지(limit 없음), API 예외 발생 시 동작을 검증한다."""
        drive = GoogleDriveManager(service_account_file=self.credentials_file)
        drive.service = None
        self.assertIsNone(drive.get_account_and_quota())

        # 무제한 스토리지 계정 (storageQuota에 limit 키 없음, 비정상 숫자/bool 방어 포함)
        mock_service = MagicMock()
        mock_service.about.return_value.get.return_value.execute.return_value = {
            "user": {"displayName": "Org User", "emailAddress": "org@example.com"},
            "storageQuota": {"limit": True, "usage": str(10 * 1024**3), "usageInDrive": "not-an-int"},
        }
        drive.service = mock_service
        info = drive.get_account_and_quota()
        self.assertIsNone(info["limit_gb"])
        self.assertEqual(info["usage_percent"], 0.0)
        self.assertEqual(info["usage_gb"], 10.0)
        self.assertEqual(info["drive_usage_gb"], 0.0)

        # API 호출 예외 발생 시 None 반환
        mock_service.about.return_value.get.return_value.execute.side_effect = RuntimeError("network down")
        self.assertIsNone(drive.get_account_and_quota())


FOLDER = "application/vnd.google-apps.folder"
PDF = "application/pdf"


def make_drive(items, folder_id=None, pages=None):
    """Drive API service를 MagicMock으로 대체한 GoogleDriveManager를 만든다."""
    tmp = tempfile.mkdtemp()
    drive = GoogleDriveManager(service_account_file=os.path.join(tmp, "none.json"))
    shutil.rmtree(tmp, ignore_errors=True)
    drive.folder_id = folder_id
    service = MagicMock()
    req = MagicMock()
    if pages is not None:
        req.execute.side_effect = pages
    else:
        req.execute.return_value = {"files": items}
    service.files.return_value.list.return_value = req
    drive.service = service
    return drive


def list_kwargs(drive):
    """files().list() 첫 호출의 키워드 인자."""
    return drive.service.files.return_value.list.call_args_list[0].kwargs


class TestRecordShape(unittest.TestCase):
    """목록 레코드 형태."""

    ITEMS = (
        {"id": "f_kb", "name": "knowledge-base", "mimeType": FOLDER, "parents": ["root_id"]},
        {"id": "f_raw", "name": "raw-data", "mimeType": FOLDER, "parents": ["f_kb"]},
        {
            "id": "b2", "name": "\u1112\u1161\u11ab.pdf", "mimeType": PDF, "parents": ["f_raw"],
            "size": "2048", "sha256Checksum": "abc123", "modifiedTime": "2026-01-02T03:04:05.000Z",
            "webViewLink": "https://drive.google.com/file/d/b2/view",
        },
        {"id": "b1", "name": "a.pdf", "mimeType": PDF, "parents": ["f_raw"]},
        {"id": "b0", "name": "z.pdf", "mimeType": PDF, "parents": ["root_id"], "size": "1"},
    )

    def test_record_has_exact_keys(self):
        """레코드는 정확히 id·name·folder_path·size_bytes·sha256·modified_time·web_view_link 키를 갖는다."""
        files = make_drive(self.ITEMS).list_pdf_files()
        self.assertEqual(len(files), 3)
        for f in files:
            self.assertEqual(
                set(f.keys()),
                {"id", "name", "folder_path", "size_bytes", "sha256", "modified_time", "web_view_link"},
            )

    def test_record_values_and_types(self):
        """size_bytes는 int, sha256은 str(없으면 ""), modified_time은 Drive RFC3339 문자열 그대로, 이름은 NFC."""
        files = {f["id"]: f for f in make_drive(self.ITEMS).list_pdf_files()}
        b2 = files["b2"]
        self.assertEqual(b2["name"], "한.pdf")
        self.assertEqual(b2["folder_path"], "knowledge-base/raw-data")
        self.assertEqual(b2["size_bytes"], 2048)
        self.assertIsInstance(b2["size_bytes"], int)
        self.assertEqual(b2["sha256"], "abc123")
        self.assertEqual(b2["modified_time"], "2026-01-02T03:04:05.000Z")
        self.assertEqual(b2["web_view_link"], "https://drive.google.com/file/d/b2/view")
        b1 = files["b1"]
        self.assertEqual(b1["size_bytes"], 0)
        self.assertEqual(b1["sha256"], "")
        self.assertEqual(b1["modified_time"], "")
        self.assertEqual(b1["web_view_link"], "")

    def test_requests_web_view_link_field(self):
        """files().list fields에 webViewLink·sha256Checksum·modifiedTime·size를 요청한다."""
        drive = make_drive(self.ITEMS)
        drive.list_pdf_files()
        fields = list_kwargs(drive)["fields"]
        for name in ("webViewLink", "sha256Checksum", "modifiedTime", "size", "parents", "mimeType"):
            self.assertIn(name, fields)

    def test_sorted_by_folder_path_then_name(self):
        """결과는 (folder_path, name) 순으로 결정적으로 정렬된다."""
        files = make_drive(self.ITEMS).list_pdf_files()
        self.assertEqual([(f["folder_path"], f["name"]) for f in files], [
            ("", "z.pdf"),
            ("knowledge-base/raw-data", "a.pdf"),
            ("knowledge-base/raw-data", "한.pdf"),
        ])

    def test_paginates(self):
        """nextPageToken을 따라 모든 페이지를 수집한다."""
        drive = make_drive(None, pages=[
            {"files": [{"id": "p1", "name": "p1.pdf", "mimeType": PDF, "parents": ["root_id"]}], "nextPageToken": "t2"},
            {"files": [{"id": "p2", "name": "p2.pdf", "mimeType": PDF, "parents": ["root_id"]}]},
        ])
        self.assertEqual([f["id"] for f in drive.list_pdf_files()], ["p1", "p2"])


class TestSlashInFileName(unittest.TestCase):
    """파일명 속 '/'는 폴더 구분자가 아니다."""

    ITEMS = (
        {"id": "f_kb", "name": "knowledge-base", "mimeType": FOLDER, "parents": ["root_id"]},
        {"id": "f_raw", "name": "raw-data", "mimeType": FOLDER, "parents": ["f_kb"]},
        {"id": "ab", "name": "A/B-테스트", "mimeType": PDF, "parents": ["f_raw"]},
        {"id": "c", "name": "C/D/E.pdf", "mimeType": PDF, "parents": ["root_id"]},
    )

    def test_slash_name_split_correctly(self):
        """knowledge-base/raw-data 안의 'A/B-테스트' → folder_path와 name이 올바르게 분리된다."""
        files = {f["id"]: f for f in make_drive(self.ITEMS).list_pdf_files()}
        self.assertEqual(files["ab"]["folder_path"], "knowledge-base/raw-data")
        self.assertEqual(files["ab"]["name"], "A/B-테스트")

    def test_folder_path_only_from_parent_folders(self):
        """folder_path는 상위 폴더 이름만으로 만들며 파일명을 쪼개지 않는다 (루트 파일 'C/D/E.pdf' → "")."""
        files = {f["id"]: f for f in make_drive(self.ITEMS).list_pdf_files()}
        self.assertEqual(files["c"]["folder_path"], "")
        self.assertEqual(files["c"]["name"], "C/D/E.pdf")
        self.assertNotIn("knowledge-base/raw-data/A", {f["folder_path"] for f in files.values()})

    def test_slash_in_folder_name_kept_whole(self):
        """폴더 이름의 '/'도 이름 그대로 이어붙일 뿐 별도 해석하지 않는다."""
        items = [
            {"id": "f1", "name": "I/O", "mimeType": FOLDER, "parents": ["root_id"]},
            {"id": "b", "name": "b.pdf", "mimeType": PDF, "parents": ["f1"]},
        ]
        self.assertEqual(make_drive(items).list_pdf_files()[0]["folder_path"], "I/O")


class TestScope(unittest.TestCase):
    """GOOGLE_DRIVE_FOLDER_ID 범위와 휴지통 제외."""

    ITEMS = (
        {"id": "f_kb", "name": "knowledge-base", "mimeType": FOLDER, "parents": ["root_id"]},
        {"id": "f_raw", "name": "raw-data", "mimeType": FOLDER, "parents": ["f_kb"]},
        {"id": "f_lang", "name": "어학", "mimeType": FOLDER, "parents": ["f_raw"]},
        {"id": "f_other", "name": "Saved from Chrome", "mimeType": FOLDER, "parents": ["root_id"]},
        {"id": "in_raw", "name": "raw.pdf", "mimeType": PDF, "parents": ["f_raw"]},
        {"id": "in_lang", "name": "lang.pdf", "mimeType": PDF, "parents": ["f_lang"]},
        {"id": "in_other", "name": "other.pdf", "mimeType": PDF, "parents": ["f_other"]},
        {"id": "at_root", "name": "root.pdf", "mimeType": PDF, "parents": ["root_id"]},
        {"id": "shared", "name": "shared.pdf", "mimeType": PDF, "parents": ["unknown_folder"]},
        {"id": "orphan", "name": "orphan.pdf", "mimeType": PDF},
    )

    def paths(self, folder_id):
        return {f["id"]: f["folder_path"] for f in make_drive(self.ITEMS, folder_id=folder_id).list_pdf_files()}

    def test_folder_id_limits_to_subtree(self):
        """folder_id가 지정되면 그 폴더의 하위 트리만, 그 폴더 기준 상대 경로로 반환한다."""
        self.assertEqual(self.paths("f_raw"), {"in_raw": "", "in_lang": "어학"})

    def test_folder_id_excludes_unknown_parents(self):
        """folder_id가 지정되면 부모를 알 수 없는 공유 항목·부모 없는 항목은 제외한다."""
        paths = self.paths("f_kb")
        self.assertEqual(paths, {"in_raw": "raw-data", "in_lang": "raw-data/어학"})

    def test_whole_drive_when_folder_id_empty(self):
        """folder_id가 비어 있으면(None·"") 드라이브 전체를 내 드라이브 루트 기준 경로로 반환한다."""
        expected = {
            "in_raw": "knowledge-base/raw-data",
            "in_lang": "knowledge-base/raw-data/어학",
            "in_other": "Saved from Chrome",
            "at_root": "",
            "shared": "",
            "orphan": "",
        }
        self.assertEqual(self.paths(None), expected)
        self.assertEqual(self.paths(""), expected)

    def test_cyclic_parents_do_not_hang(self):
        """폴더 부모 관계가 순환해도 무한 루프 없이 끝난다."""
        items = [
            {"id": "x", "name": "x", "mimeType": FOLDER, "parents": ["y"]},
            {"id": "y", "name": "y", "mimeType": FOLDER, "parents": ["x"]},
            {"id": "b", "name": "b.pdf", "mimeType": PDF, "parents": ["x"]},
        ]
        self.assertEqual(len(make_drive(items).list_pdf_files()), 1)
        self.assertEqual(make_drive(items, folder_id="z").list_pdf_files(), [])

    def test_query_excludes_trashed(self):
        """files().list 쿼리 q에 trashed=false가 들어가 휴지통 파일을 제외한다."""
        for folder_id in (None, "f_raw"):
            drive = make_drive(self.ITEMS, folder_id=folder_id)
            drive.list_pdf_files()
            self.assertIn("trashed=false", list_kwargs(drive)["q"].replace(" ", ""))


class TestPdfFilter(unittest.TestCase):
    """도서 PDF 판정."""

    ITEMS = (
        {"id": "folder_pdf", "name": "모음.pdf", "mimeType": FOLDER, "parents": ["root_id"]},
        {"id": "pdf_mime", "name": "패턴을-활용한-리팩토링", "mimeType": PDF, "parents": ["root_id"]},
        {"id": "pdf_ext_upper", "name": "CONTENT.PDF", "mimeType": "application/octet-stream", "parents": ["root_id"]},
        {"id": "pdf_both", "name": "a.pdf", "mimeType": PDF, "parents": ["root_id"]},
        {"id": "doc", "name": "notes", "mimeType": "application/vnd.google-apps.document", "parents": ["root_id"]},
        {"id": "epub", "name": "content.epub", "mimeType": "application/epub+zip", "parents": ["root_id"]},
        {"id": "pdf_in_middle", "name": "a.pdf.zip", "mimeType": "application/zip", "parents": ["root_id"]},
    )

    def ids(self):
        return {f["id"] for f in make_drive(self.ITEMS).list_pdf_files()}

    def test_only_pdf_mime_or_extension(self):
        """mimeType이 application/pdf이거나 이름이 .pdf(대소문자 무시)로 끝나는 파일만 수집한다."""
        self.assertEqual(self.ids(), {"pdf_mime", "pdf_ext_upper", "pdf_both"})

    def test_folders_and_other_types_excluded(self):
        """폴더(이름이 .pdf로 끝나도)와 다른 형식(문서·epub·zip)은 제외한다."""
        ids = self.ids()
        for excluded in ("folder_pdf", "doc", "epub", "pdf_in_middle"):
            self.assertNotIn(excluded, ids)

    def test_pdf_mime_without_extension_included(self):
        """확장자가 없어도 mimeType이 application/pdf면 도서로 본다 (예: 패턴을-활용한-리팩토링)."""
        files = {f["id"]: f for f in make_drive(self.ITEMS).list_pdf_files()}
        self.assertEqual(files["pdf_mime"]["name"], "패턴을-활용한-리팩토링")


@pytest.mark.integration
def test_integration_drive_api_cassettes_about_and_paginated_files():
    """[Integration / 경계 1] drive_api_cassettes.yaml에 선언된 Google Drive API v3(about.get · files.list 다중 페이지) 카세트를 재생하여 파싱·정규화·서브트리 필터링을 검증한다."""

    cassettes_path = Path(__file__).with_name("drive_api_cassettes.yaml")
    cassettes = yaml.safe_load(cassettes_path.read_text(encoding="utf-8"))

    # 1) about.get 카세트 검증 (100GB 일반 계정 & 무제한 워크스페이스 계정)
    for case_name, spec in cassettes["about_get_responses"].items():
        drive = make_drive([])
        drive.service.about.return_value.get.return_value.execute.return_value = {
            "user": spec["user"],
            "storageQuota": spec["storageQuota"],
        }
        parsed = drive.get_account_and_quota()
        expected = spec["expected_parsed"]
        for key, val in expected.items():
            assert parsed[key] == val, f"[{case_name}] {key}: {parsed[key]} != {val}"

    # 2) files.list 다중 페이지네이션·NFD->NFC·제목 내 슬래시·폴더 서브트리 필터링 카세트 검증
    list_spec = cassettes["files_list_paginated_cassette"]
    drive_paged = make_drive(
        None,
        folder_id="folder_root_books",
        pages=list_spec["pages"],
    )
    records = drive_paged.list_pdf_files()
    expected_records = list_spec["expected_subtree_records_for_folder_root_books"]
    assert len(records) == len(expected_records)
    for actual, exp in zip(records, expected_records, strict=True):
        for k, v in exp.items():
            assert actual[k] == v


@settings(max_examples=80, deadline=None)
@given(
    raw_val=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-100_000, max_value=100_000),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(max_size=20),
    ),
    raw_name=st.one_of(st.none(), st.integers(), st.text(max_size=30)),
    raw_mime=st.one_of(st.none(), st.text(max_size=30)),
)
@example(raw_val=float("inf"), raw_name=123, raw_mime=None)
@example(raw_val=float("-inf"), raw_name="\u1100\u1161.PDF", raw_mime="application/octet-stream")
@example(raw_val=True, raw_name=None, raw_mime="application/pdf")
def test_property_fuzz_safe_non_negative_int_and_is_pdf_item(
    raw_val: Any,
    raw_name: Any,
    raw_mime: Any,
) -> None:
    """_safe_non_negative_int의 무한대/불리언/음수 방어 및 is_pdf_item의 비문자열/유니코드 안전성을 검증한다."""
    parsed = _safe_non_negative_int(raw_val)
    assert type(parsed) is int
    assert parsed >= 0
    assert _safe_non_negative_int(parsed) == parsed

    verdict = is_pdf_item({"name": raw_name, "mimeType": raw_mime})
    assert isinstance(verdict, bool)


if __name__ == "__main__":
    unittest.main()
