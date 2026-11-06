# ---
# title: boundary_errors
# description: 외부 입출력·런타임 경계의 통합 예외 계층과 HTTP GET 핸들러
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""외부 입출력·런타임 경계 통합 예외 계층 및 HTTP GET 핸들러."""
from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final
from urllib import error as urllib_error
from urllib import request as urllib_request

__all__ = [
    "AUTH_MISSING_MESSAGE",
    "BOUNDARY_TO_EXTERNAL",
    "SNAPSHOT_PRESERVED_NOTICE",
    "BoundaryKind",
    "ClassificationError",
    "DoctreeBoundaryError",
    "DriveAuthError",
    "DriveListError",
    "ExternalBoundary",
    "SecurityAuditError",
    "SnapshotStorageError",
    "coerce_boundary_error",
    "execute_json_http_get",
]


class ExternalBoundary(StrEnum):
    """프로세스가 외부 세계와 데이터를 주고받는 4대 물리·런타임 경계면."""

    PROCESS_TERMINAL = "process_terminal"
    NETWORK_API = "network_api"
    FILESYSTEM = "filesystem"
    SYSTEM_CLOCK = "system_clock"


class BoundaryKind(StrEnum):
    """예외가 발생한 세부 경계면 식별자."""

    AUTH = "auth"
    DRIVE_API = "drive_api"
    THREAT_INTEL_API = "threat_intel_api"
    FILESYSTEM = "filesystem"
    TAXONOMY = "taxonomy"
    SECURITY_AUDIT = "security_audit"


BOUNDARY_TO_EXTERNAL: Final[Mapping[BoundaryKind, ExternalBoundary]] = MappingProxyType(
    {
        BoundaryKind.AUTH: ExternalBoundary.NETWORK_API,
        BoundaryKind.DRIVE_API: ExternalBoundary.NETWORK_API,
        BoundaryKind.THREAT_INTEL_API: ExternalBoundary.NETWORK_API,
        BoundaryKind.FILESYSTEM: ExternalBoundary.FILESYSTEM,
        BoundaryKind.TAXONOMY: ExternalBoundary.FILESYSTEM,
        BoundaryKind.SECURITY_AUDIT: ExternalBoundary.FILESYSTEM,
    }
)


_BOUNDARY_PREFIX: Final[Mapping[BoundaryKind, str]] = MappingProxyType(
    {
        BoundaryKind.AUTH: "구글 드라이브 인증 실패",
        BoundaryKind.DRIVE_API: "드라이브 목록 조회 실패",
        BoundaryKind.THREAT_INTEL_API: "위협 인텔리전스 API 요청 실패",
        BoundaryKind.FILESYSTEM: "파일 입출력 실패",
        BoundaryKind.TAXONOMY: "분류 검증 실패",
        BoundaryKind.SECURITY_AUDIT: "보안 감사 실패",
    }
)

AUTH_MISSING_MESSAGE: Final[str] = (
    "구글 드라이브 인증에 실패했거나 credentials.json / token.json이 없습니다. "
    "`doctree auth`를 먼저 실행하세요."
)
SNAPSHOT_PRESERVED_NOTICE: Final[str] = "기존 스냅샷은 그대로 두었습니다."
_RAW_DETAIL_BOUNDARIES: Final[frozenset[BoundaryKind]] = frozenset(
    {BoundaryKind.AUTH, BoundaryKind.SECURITY_AUDIT}
)


class DoctreeBoundaryError(Exception):
    """doctree의 모든 외부 경계면 예외의 최상위 기반 클래스.

    발생 세부 경계(boundary), 상세 사유(detail), 스냅샷 보존 안내 여부(preserve_snapshot)를
    일관된 구조로 보유하며 format_error()로 통일된 사용자 메시지를 생성한다.
    """

    __slots__ = ()
    default_boundary: BoundaryKind = BoundaryKind.FILESYSTEM

    def __init__(
        self,
        detail: str,
        *,
        boundary: BoundaryKind | None = None,
        preserve_snapshot: bool = False,
    ) -> None:
        super().__init__(detail)
        self.detail = str(detail)
        self.boundary: BoundaryKind = boundary or self.default_boundary
        self.preserve_snapshot: bool = preserve_snapshot

    @property
    def external_boundary(self) -> ExternalBoundary:
        """이 예외가 속한 상위 4대 외부 경계면(ExternalBoundary)을 반환한다."""
        return BOUNDARY_TO_EXTERNAL[self.boundary]

    def format_error(self) -> str:
        """경계면 종류에 맞는 일관된 한국어 에러 메시지를 반환한다."""
        if self.boundary in _RAW_DETAIL_BOUNDARIES:
            return self.detail
        prefix = _BOUNDARY_PREFIX[self.boundary]
        if self.detail.startswith(f"{prefix}:"):
            return self.detail
        return f"{prefix}: {self.detail}"


class DriveAuthError(DoctreeBoundaryError, RuntimeError):
    """구글 드라이브 OAuth 2.0 / 서비스 계정 인증 경계 실패 (경계 ② 네트워크 소켓 & ③ 파일시스템)."""

    __slots__ = ()
    default_boundary = BoundaryKind.AUTH

    def __init__(
        self,
        detail: str = AUTH_MISSING_MESSAGE,
        *,
        preserve_snapshot: bool = False,
    ) -> None:
        super().__init__(detail, boundary=BoundaryKind.AUTH, preserve_snapshot=preserve_snapshot)


class DriveListError(DoctreeBoundaryError, RuntimeError):
    """구글 드라이브 API 목록 조회(files.list) 경계 실패 (경계 ② 외부 네트워크 소켓)."""

    __slots__ = ()
    default_boundary = BoundaryKind.DRIVE_API


class ClassificationError(DoctreeBoundaryError, ValueError):
    """분류 체계 YAML 설정·스냅샷 스키마·엄격 모드 미분류 검증 경계 실패 (경계 ③ 로컬 파일시스템 데이터 스키마)."""

    __slots__ = ()
    default_boundary = BoundaryKind.TAXONOMY


class SnapshotStorageError(DoctreeBoundaryError, OSError):
    """로컬 스냅샷·설정·캐시·PDF 파일시스템 입출력 경계 실패 (경계 ③ 로컬 파일시스템 VFS)."""

    __slots__ = ()
    default_boundary = BoundaryKind.FILESYSTEM


class SecurityAuditError(DoctreeBoundaryError, RuntimeError):
    """위협 인텔리전스 해시 대조 또는 로컬 PDF 보안 감사 차단 경계 실패 (경계 ② 네트워크 API & ③ 파일시스템)."""

    __slots__ = ()
    default_boundary = BoundaryKind.SECURITY_AUDIT


def coerce_boundary_error(
    error: BaseException,
    *,
    default_boundary: BoundaryKind = BoundaryKind.FILESYSTEM,
    preserve_snapshot: bool = False,
) -> DoctreeBoundaryError:
    """임의의 저수준 예외(OSError, JSONDecodeError, UnicodeError 등)를 DoctreeBoundaryError로 승격한다."""
    if isinstance(error, DoctreeBoundaryError):
        if preserve_snapshot and not error.preserve_snapshot:
            error.preserve_snapshot = True
        return error
    if isinstance(error, json.JSONDecodeError):
        return ClassificationError(
            f"JSON 파싱 오류: {error}",
            preserve_snapshot=preserve_snapshot,
        )
    return DoctreeBoundaryError(
        str(error),
        boundary=default_boundary,
        preserve_snapshot=preserve_snapshot,
    )


_MAX_HTTP_RESPONSE_BYTES: Final[int] = 10 * 1024 * 1024


def execute_json_http_get(
    url: str,
    headers: Mapping[str, str],
    *,
    timeout: float = 10.0,
    service_name: str = "API",
    urlopen: Any = urllib_request.urlopen,
) -> tuple[dict[str, Any] | None, int | None, str]:
    """외부 웹/REST API(VirusTotal, MetaDefender, OTX) GET 요청 및 예외 처리를 일원화한다 (경계 ② 네트워크 소켓).

    반환값: (parsed_json_or_none, http_status_code_or_none, error_detail)
    - 정상(200): (dict, 200, "")
    - 미등록(404): (None, 404, f"{service_name} 미등록 해시")
    - 그 외 HTTP 오류: (None, code, f"HTTP {code}")
    - 네트워크·타임아웃·JSON 파싱 오류: (None, None, str(error))
    """
    if not url.startswith(("https://", "http://")):
        return None, None, f"{service_name} 지원하지 않는 URL 스킴입니다: {url}"
    try:
        req = urllib_request.Request(url, headers=dict(headers), method="GET")
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read(_MAX_HTTP_RESPONSE_BYTES).decode("utf-8")
            body = json.loads(raw)
            if not isinstance(body, dict):
                return None, None, f"{service_name} 응답 형식 오류: JSON 객체가 아닙니다."
            return body, 200, ""
    except urllib_error.HTTPError as exc:
        if exc.code == 404:
            return None, 404, f"{service_name} 미등록 해시"
        return None, exc.code, f"HTTP {exc.code}"
    except Exception as exc:
        return None, None, str(exc)
