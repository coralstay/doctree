# ---
# title: drive_pdf_fetcher
# description: 구글 드라이브 API v3로 PDF 메타데이터(SHA-256 포함)를 수집한다
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""구글 드라이브 API v3 연동 및 PDF 메타데이터(SHA-256 포함) 수집기."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final, TypedDict

from doctree.runtime_env.boundary_errors import DriveListError
from doctree.runtime_env.workspace_settings import atomic_write_text, normalize_nfc

__all__ = [
    "FOLDER_MIME",
    "GOOGLE_API_AVAILABLE",
    "LIST_FIELDS",
    "PDF_MIME",
    "SCOPES",
    "DriveListError",
    "DrivePdfRecord",
    "GoogleDriveManager",
    "is_pdf_item",
]

try:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GOOGLE_API_AVAILABLE = True
except ImportError:
    service_account = None
    Credentials = None
    InstalledAppFlow = None
    Request = None
    build = None
    GOOGLE_API_AVAILABLE = False

SCOPES: Final[tuple[str, ...]] = ('https://www.googleapis.com/auth/drive.readonly',)

FOLDER_MIME = 'application/vnd.google-apps.folder'
PDF_MIME = 'application/pdf'
LIST_FIELDS = 'nextPageToken, files(id, name, mimeType, parents, size, sha256Checksum, modifiedTime, webViewLink)'
_BYTES_PER_GIB: Final[int] = 1024**3


def _bytes_to_gib(byte_count: int) -> float:
    """바이트 단위를 소수점 둘째 자리 기비바이트(GiB)로 변환한다."""
    return round(byte_count / _BYTES_PER_GIB, 2)


def _safe_non_negative_int(value: Any) -> int:
    """임의 입력(문자열·정수·None 등)을 0 이상의 정수로 안전하게 변환한다."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


class DrivePdfRecord(TypedDict):
    """스냅샷 JSON에 직렬화되는 단일 구글 드라이브 PDF 메타데이터 레코드 스키마 (PEP 589)."""

    id: str
    name: str
    folder_path: str
    size_bytes: int
    sha256: str
    modified_time: str
    web_view_link: str


def is_pdf_item(item: dict) -> bool:
    """mimeType이 application/pdf이거나 이름이 .pdf로 끝나면(대소문자 무시) 콘텐츠 PDF로 본다."""
    return item.get('mimeType') == PDF_MIME or normalize_nfc(item.get('name')).lower().endswith('.pdf')


class GoogleDriveManager:
    """구글 드라이브 API 연동 (서비스 계정 및 OAuth 2.0 지원) 및 콘텐츠 PDF 메타데이터 목록 조회 (읽기 전용)"""

    __slots__ = ("folder_id", "service", "service_account_file", "token_file")

    def __init__(self, service_account_file: str | None = None, folder_id: str | None = None):
        self.service_account_file = service_account_file or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not self.service_account_file and Path("credentials.json").exists():
            self.service_account_file = "credentials.json"

        self.folder_id = folder_id or os.getenv("GOOGLE_DRIVE_FOLDER_ID")
        self.token_file = Path(self.service_account_file).parent / "token.json" if self.service_account_file else Path("token.json")
        self.service = None

        if GOOGLE_API_AVAILABLE and (
            self.token_file.exists()
            or (self.service_account_file and os.path.exists(self.service_account_file))
        ):
            self._authenticate()

    def _try_load_or_refresh_token(self) -> Any:
        """기존 token.json에서 유효한 자격증명을 로드하거나 만료 시 refresh_token으로 원자적 갱신한다."""
        if not self.token_file.exists():
            return None
        try:
            credentials = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
            if credentials and credentials.valid:
                return credentials
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                atomic_write_text(self.token_file, credentials.to_json())
                return credentials
        except Exception as e:
            print(f"[GoogleDriveManager] 기존 token.json 로드 실패: {e}")
        return None

    def _load_credentials_from_secret_file(self) -> Any:
        """서비스 계정 키 또는 OAuth 2.0 클라이언트 시크릿(credentials.json)으로부터 자격증명을 생성한다."""
        if not self.service_account_file or not os.path.exists(self.service_account_file):
            print("[GoogleDriveManager] 인증 키 파일(credentials.json)이 없습니다.")
            return None

        with open(self.service_account_file, encoding='utf-8') as f:
            creds_data = json.load(f)

        if not isinstance(creds_data, dict):
            print(f"[GoogleDriveManager] 알 수 없는 인증 파일 형식입니다: {self.service_account_file}")
            return None

        if creds_data.get("type") == "service_account":
            return service_account.Credentials.from_service_account_file(
                self.service_account_file,
                scopes=SCOPES,
            )
        if "installed" in creds_data or "web" in creds_data:
            flow = InstalledAppFlow.from_client_secrets_file(self.service_account_file, SCOPES)
            print("\n[GoogleDriveManager] 🌐 브라우저를 열어 구글 로그인을 진행합니다...")
            credentials = flow.run_local_server(port=0)
            atomic_write_text(self.token_file, credentials.to_json())
            print(f"[GoogleDriveManager] 인증 토큰 저장 완료: {self.token_file}")
            return credentials

        print(f"[GoogleDriveManager] 알 수 없는 인증 파일 형식입니다: {self.service_account_file}")
        return None

    def _authenticate(self):
        """Service Account 또는 OAuth 2.0 InstalledAppFlow를 감지하여 인증 초기화"""
        try:
            credentials = self._try_load_or_refresh_token()
            if credentials is None:
                credentials = self._load_credentials_from_secret_file()
            if credentials is not None:
                self.service = build('drive', 'v3', credentials=credentials)
        except Exception as e:
            print(f"[GoogleDriveManager] 인증 초기화 경고: {e}")

    def get_account_and_quota(self) -> dict | None:
        """현재 로그인된 구글 계정 정보 및 드라이브 스토리지 사용 현황을 조회합니다 (about.get)."""
        if not self.service:
            return None
        try:
            about = self.service.about().get(
                fields="user(displayName, emailAddress), storageQuota(limit, usage, usageInDrive)"
            ).execute()
            user = about.get('user') or {}
            quota = about.get('storageQuota') or {}
            limit_bytes = _safe_non_negative_int(quota.get('limit'))
            usage_bytes = _safe_non_negative_int(quota.get('usage'))
            drive_usage_bytes = _safe_non_negative_int(quota.get('usageInDrive'))

            return {
                "name": user.get('displayName') or 'Unknown',
                "email": user.get('emailAddress') or 'Unknown',
                "limit_gb": _bytes_to_gib(limit_bytes) if limit_bytes > 0 else None,
                "usage_gb": _bytes_to_gib(usage_bytes),
                "drive_usage_gb": _bytes_to_gib(drive_usage_bytes),
                "usage_percent": round((usage_bytes / limit_bytes) * 100, 1) if limit_bytes > 0 else 0.0
            }
        except Exception as e:
            print(f"[GoogleDriveManager] 계정 및 스토리지 정보 조회 실패: {e}")
            return None

    def _list_all_items(self) -> list[dict]:
        """휴지통을 제외한 드라이브 항목(폴더·파일)을 페이지네이션으로 모두 수집한다."""
        all_items: list[dict] = []
        page_token = None
        while True:
            response = self.service.files().list(
                q="trashed=false",
                spaces='drive',
                pageSize=1000,
                fields=LIST_FIELDS,
                pageToken=page_token
            ).execute()
            all_items.extend(response.get('files', []))
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        return all_items

    def _resolve_folder_path(self, item: dict, folders_map: dict[str, dict]) -> str | None:
        """파일의 상위 폴더 이름만 '/'로 이어 folder_path를 만든다 (파일명은 절대 쪼개지 않는다).

        - folder_id 지정: 조상 체인이 folder_id에 닿아야 하며, 그 폴더 기준 상대 경로를 돌려준다
          (바로 아래 파일은 ""). 닿지 않으면(상위가 폴더 맵에 없는 공유 항목 포함) None → 제외.
        - folder_id 미지정: 내 드라이브 루트 기준 경로. 체인은 폴더 맵에 없는 부모(루트 또는 알 수 없는
          공유 폴더)에서 멈추므로, 루트 파일과 부모를 알 수 없는 공유 항목은 "".
        """
        parents = item.get('parents') or []
        current_parent_id = parents[0] if parents else None
        folder_names: list[str] = []
        visited: set[str] = set()
        while current_parent_id and current_parent_id not in visited:
            if self.folder_id and current_parent_id == self.folder_id:
                return '/'.join(reversed(folder_names))
            folder = folders_map.get(current_parent_id)
            if folder is None:
                break
            visited.add(current_parent_id)
            folder_names.append(folder['name'])
            folder_parents = folder.get('parents') or []
            current_parent_id = folder_parents[0] if folder_parents else None
        if self.folder_id:
            return None
        return '/'.join(reversed(folder_names))

    def list_pdf_files(self) -> list[DrivePdfRecord]:
        """콘텐츠 PDF 목록을 스냅샷용 메타데이터 레코드로 반환한다.

        레코드 키: id, name, folder_path, size_bytes(int), sha256(str, 없으면 ""),
        modified_time(Drive RFC3339 문자열), web_view_link. (folder_path, name) 순 정렬.

        서비스가 없으면(인증 실패) 빈 목록을 돌려준다(호출 측이 drive.service로 먼저 확인).
        API 호출이 실패하면 DriveListError를 낸다 — 빈 목록과 구별해 빈 스냅샷을 막기 위함(KBI-6#B2).
        """
        if not self.service:
            print("[GoogleDriveManager] 구글 드라이브 서비스가 활성화되지 않았습니다 (자격 증명 파일 확인 필요).")
            return []

        try:
            all_items = self._list_all_items()
        except Exception as e:
            raise DriveListError(f"구글 드라이브 파일 목록 조회 실패: {e}") from e

        folders_map: dict[str, dict] = {
            item['id']: {'name': normalize_nfc(item.get('name')), 'parents': item.get('parents') or []}
            for item in all_items
            if item.get('id') and item.get('mimeType') == FOLDER_MIME
        }

        records: list[DrivePdfRecord] = []
        for item in all_items:
            if not item.get('id') or item.get('mimeType') == FOLDER_MIME or not is_pdf_item(item):
                continue
            folder_path = self._resolve_folder_path(item, folders_map)
            if folder_path is None:
                continue
            records.append({
                'id': str(item['id']),
                'name': normalize_nfc(item.get('name')),
                'folder_path': folder_path,
                'size_bytes': _safe_non_negative_int(item.get('size')),
                'sha256': str(item.get('sha256Checksum') or "").strip().lower(),
                'modified_time': str(item.get('modifiedTime') or ""),
                'web_view_link': str(item.get('webViewLink') or ""),
            })

        records.sort(key=lambda r: (r['folder_path'], r['name']))
        return records
