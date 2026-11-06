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
    GOOGLE_API_AVAILABLE = True  # ---- 1
except ImportError:
    service_account = None
    Credentials = None
    InstalledAppFlow = None
    Request = None
    build = None
    GOOGLE_API_AVAILABLE = False

SCOPES: Final[tuple[str, ...]] = ('https://www.googleapis.com/auth/drive.readonly',)  # ---- 2

FOLDER_MIME = 'application/vnd.google-apps.folder'
PDF_MIME = 'application/pdf'
LIST_FIELDS = 'nextPageToken, files(id, name, mimeType, parents, size, sha256Checksum, modifiedTime, webViewLink)'  # ---- 3
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


def is_pdf_item(item: dict) -> bool:  # ---- 4
    """mimeType이 application/pdf이거나 이름이 .pdf로 끝나면(대소문자 무시) 콘텐츠 PDF로 본다."""
    return item.get('mimeType') == PDF_MIME or normalize_nfc(item.get('name')).lower().endswith('.pdf')


class GoogleDriveManager:
    """구글 드라이브 API 연동 (서비스 계정 및 OAuth 2.0 지원) 및 콘텐츠 PDF 메타데이터 목록 조회 (읽기 전용)"""

    __slots__ = ("folder_id", "service", "service_account_file", "token_file")

    def __init__(self, service_account_file: str | None = None, folder_id: str | None = None):  # ---- 5
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

    def _try_load_or_refresh_token(self) -> Any:  # ---- 6
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

    def _load_credentials_from_secret_file(self) -> Any:  # ---- 7
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

    def _list_all_items(self) -> list[dict]:  # ---- 8
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

    def _resolve_folder_path(self, item: dict, folders_map: dict[str, dict]) -> str | None:  # ---- 9
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

    def list_pdf_files(self) -> list[DrivePdfRecord]:  # ---- 10
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


# ======== 노트 ========
# 1 선택적 의존성 가드
#   설명: google 라이브러리가 없어도 import는 성공하고 GOOGLE_API_AVAILABLE=False가 된다. 인증이 필요한 호출만 실패한다(오프라인 명령과 테스트가 계속 동작).
# 2 읽기 전용 스코프
#   설명: drive.readonly 하나만 요청한다. 이 도구는 드라이브를 바꾸지 않고 메타데이터만 읽는다는 약속이다.
#   관련: backlog/decisions/decision-1, backlog/decisions/decision-2
# 3 API 필드 마스크
#   설명: files.list가 돌려줄 필드를 필요한 것만 고른다. sha256Checksum 덕분에 PDF를 내려받지 않고도 내용 동일성을 비교할 수 있다.
#   개념: Drive API fields 파라미터
# 4 PDF 판정
#   설명: mimeType 또는 .pdf 확장자 중 하나면 PDF로 본다(확장자가 없는 업로드도 mimeType으로 잡는다).
# 5 인증 파일 해석
#   설명: 인자, 환경 변수, 현재 디렉터리의 credentials.json 순으로 찾고 token.json은 그 옆에 둔다. 파일 내용(자격 증명)은 로그나 메시지에 싣지 않는다.
#   관련: src/doctree/runtime_env/workspace_settings.py, backlog/decisions/decision-2
# 6 토큰 재사용과 갱신
#   설명: 유효한 token.json이면 그대로 쓰고, 만료됐고 refresh_token이 있으면 갱신 후 원자적으로 다시 저장한다. 테스트 모드 OAuth는 refresh token이 7일 만에 만료되어 재인증이 필요하다.
#   관련: backlog/decisions/decision-2
# 7 서비스 계정과 OAuth 분기
#   설명: credentials.json의 type으로 서비스 계정과 설치형/웹 OAuth 클라이언트를 구분한다. OAuth는 브라우저 로그인(run_local_server)이 필요해 수동 실행만 가능하다.
#   관련: backlog/decisions/decision-2
#   개념: InstalledAppFlow
# 8 휴지통 제외 전체 수집
#   설명: trashed=false 조건으로 페이지당 1000개씩 nextPageToken이 끝날 때까지 모은다. 폴더도 함께 받아야 folder_path를 계산할 수 있다.
# 9 folder_path 계산 규칙
#   설명: 부모 체인을 따라 올라가 폴더 이름만 '/'로 잇는다. folder_id를 지정했으면 그 폴더에 닿지 못하는 항목(공유 항목 포함)은 None으로 제외하고, 미지정이면 알 수 없는 부모에서 멈춘다. visited 집합으로 순환 부모를 막는다.
# 10 목록 수집과 실패 구분
#   설명: 서비스가 없으면 빈 목록을 돌려주지만 API 호출이 실패하면 DriveListError를 던진다. 실패를 빈 목록으로 오인해 빈 스냅샷을 저장하는 사고를 막는 설계다.
#   관련: KBI-6#B2, src/doctree/drive_sync/snapshot_diff_tracker.py
