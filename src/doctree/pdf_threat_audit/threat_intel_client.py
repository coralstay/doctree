# ---
# title: threat_intel_client
# description: 해시 평판 조회 클라이언트(VirusTotal v3)와 로컬 캐시
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""위협 인텔리전스 해시 평판 조회 클라이언트(ThreatIntelProvider Protocol · VirusTotal v3) 및 로컬 캐시."""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib import request as urllib_request

from doctree.pdf_threat_audit.pdf_byte_inspector import (
    LocalPdfAudit,
    compute_sha256,
    discover_local_pdfs,
    inspect_pdf_bytes,
)
from doctree.runtime_env.boundary_errors import execute_json_http_get
from doctree.runtime_env.workspace_settings import PathLike, atomic_write_text, normalize_nfc

__all__ = [
    "VT_API_URL",
    "ThreatIntelProvider",
    "VerdictStatus",
    "VirusTotalClient",
    "VtVerdict",
    "audit_drive_records",
    "audit_local_pdfs",
    "check_hashes_with_vt",
    "load_vt_cache",
    "query_virustotal_hash",
    "save_vt_cache",
]

VT_API_URL = "https://www.virustotal.com/api/v3/files/{sha256}"  # ---- 1


class VerdictStatus(StrEnum):
    """해시 평판 조회 상태 (StrEnum이므로 문자열 비교와 100% 호환)."""

    CLEAN = "clean"
    MALICIOUS = "malicious"
    SUSPICIOUS = "suspicious"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class VtVerdict:
    """해시 기반 위협 인텔리전스 조회 결과."""

    sha256: str
    status: VerdictStatus | str
    malicious: int = 0
    suspicious: int = 0
    harmless: int = 0
    undetected: int = 0
    detail: str = ""


@runtime_checkable
class ThreatIntelProvider(Protocol):  # ---- 2
    """해시 기반 위협 인텔리전스 조회 프로바이더 구조적 서브타이핑 인터페이스 (PEP 544)."""

    __slots__ = ()

    def query_hash(self, sha256: str) -> VtVerdict:
        """64자리 SHA-256 해시에 대한 위협 평판(VtVerdict)을 반환한다."""
        ...


@dataclass(frozen=True, slots=True)
class VirusTotalClient:
    """VirusTotal API v3 기반 ThreatIntelProvider 구현체."""

    api_key: str
    timeout: float = 10.0

    def query_hash(self, sha256: str) -> VtVerdict:
        return query_virustotal_hash(sha256, self.api_key, timeout=self.timeout)


_VALID_VERDICT_STATUSES: frozenset[str] = frozenset(item.value for item in VerdictStatus)
_COUNT_FIELDS: tuple[str, ...] = ("malicious", "suspicious", "harmless", "undetected")


def _parse_cached_verdict(sha: Any, item: Any) -> VtVerdict | None:
    """캐시 JSON의 단일 항목이 유효한 VtVerdict 스키마를 충족할 때만 인스턴스를 반환한다."""
    if not isinstance(sha, str) or not sha.strip() or not isinstance(item, dict):
        return None
    norm_sha = sha.strip().lower()
    status = str(item.get("status", VerdictStatus.CLEAN))
    if status not in _VALID_VERDICT_STATUSES:
        return None
    counts: dict[str, int] = {}
    for field_name in _COUNT_FIELDS:
        raw_val = item.get(field_name, 0)
        if type(raw_val) is not int or raw_val < 0:
            return None
        counts[field_name] = raw_val
    raw_detail = item.get("detail")
    return VtVerdict(
        sha256=norm_sha,
        status=status,
        malicious=counts["malicious"],
        suspicious=counts["suspicious"],
        harmless=counts["harmless"],
        undetected=counts["undetected"],
        detail="" if raw_detail is None else str(raw_detail),
    )


def load_vt_cache(cache_path: PathLike) -> dict[str, VtVerdict]:  # ---- 3
    p = Path(cache_path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, VtVerdict] = {}
    for sha, item in raw.items():
        if (verdict := _parse_cached_verdict(sha, item)) is not None:
            out[verdict.sha256] = verdict
    return out


def save_vt_cache(cache_path: PathLike, cache: dict[str, VtVerdict]) -> None:
    payload = {
        sha: {
            "status": str(v.status),
            "malicious": v.malicious,
            "suspicious": v.suspicious,
            "harmless": v.harmless,
            "undetected": v.undetected,
            "detail": v.detail,
        }
        for sha, v in sorted(cache.items())
    }
    atomic_write_text(cache_path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _extract_analysis_stats(body: dict[str, Any]) -> dict[str, int]:
    """VirusTotal 응답 JSON에서 중첩된 last_analysis_stats 사전을 방어적으로 추출한다."""
    data_obj = body.get("data")
    attrs_obj = data_obj.get("attributes") if isinstance(data_obj, dict) else None
    stats_obj = attrs_obj.get("last_analysis_stats") if isinstance(attrs_obj, dict) else None
    raw_stats = stats_obj if isinstance(stats_obj, dict) else {}
    return {
        field_name: raw_val if (type(raw_val := raw_stats.get(field_name, 0)) is int and raw_val >= 0) else 0
        for field_name in _COUNT_FIELDS
    }


def query_virustotal_hash(sha256: str, api_key: str, timeout: float = 10.0) -> VtVerdict:  # ---- 4
    """VirusTotal API v3에서 SHA-256 해시 평판을 조회한다 (파일 업로드 없음)."""
    norm_sha = sha256.strip().lower()
    body, code, detail = execute_json_http_get(
        VT_API_URL.format(sha256=norm_sha),
        headers={"x-apikey": api_key, "Accept": "application/json"},
        timeout=timeout,
        service_name="VirusTotal",
        urlopen=urllib_request.urlopen,
    )
    if body is None:
        status = VerdictStatus.NOT_FOUND if code == 404 else VerdictStatus.ERROR
        return VtVerdict(sha256=norm_sha, status=status, detail=detail)

    stats = _extract_analysis_stats(body)
    malicious = stats["malicious"]
    suspicious = stats["suspicious"]
    harmless = stats["harmless"]
    undetected = stats["undetected"]

    if malicious > 0:
        status = VerdictStatus.MALICIOUS
    elif suspicious > 0:
        status = VerdictStatus.SUSPICIOUS
    else:
        status = VerdictStatus.CLEAN

    return VtVerdict(
        sha256=norm_sha,
        status=status,
        malicious=malicious,
        suspicious=suspicious,
        harmless=harmless,
        undetected=undetected,
    )


def check_hashes_with_vt(  # ---- 5
    sha256_list: Iterable[str],
    api_key: str | None,
    cache_path: PathLike | None = None,
    *,
    provider: ThreatIntelProvider | None = None,
) -> dict[str, VtVerdict]:
    """SHA-256 목록을 캐시 우선으로 조회하고, 미캐시 항목만 위협 인텔리전스 프로바이더로 확인한다."""
    cache = load_vt_cache(cache_path) if cache_path else {}
    active_provider = provider if provider is not None else (VirusTotalClient(api_key) if api_key else None)
    results: dict[str, VtVerdict] = {}
    dirty = False

    normalized_hashes = dict.fromkeys(
        norm_sha for s in sha256_list if isinstance(s, str) and (norm_sha := s.strip().lower())
    )
    for sha in normalized_hashes:
        if sha in cache and cache[sha].status != VerdictStatus.ERROR:
            results[sha] = cache[sha]
            continue
        if active_provider is None:
            continue
        verdict = active_provider.query_hash(sha)
        results[sha] = verdict
        if verdict.status != VerdictStatus.ERROR:
            cache[sha] = verdict
            dirty = True

    if dirty and cache_path:
        save_vt_cache(cache_path, cache)
    return results


def audit_drive_records(  # ---- 6
    records: Iterable[dict],
    api_key: str | None,
    cache_path: PathLike | None = None,
) -> tuple[int, list[tuple[dict, VtVerdict]]]:
    """드라이브 스냅샷 레코드들의 SHA-256 해시를 VirusTotal(또는 로컬 캐시)로 점검한다."""
    recs = list(records)
    verdicts = check_hashes_with_vt(
        (str(r.get("sha256") or "") for r in recs),
        api_key=api_key,
        cache_path=cache_path,
    )
    flagged: list[tuple[dict, VtVerdict]] = []
    for rec in recs:
        sha = str(rec.get("sha256") or "").strip().lower()
        v = verdicts.get(sha)
        if v and v.status in (VerdictStatus.MALICIOUS, VerdictStatus.SUSPICIOUS):
            flagged.append((rec, v))
    return len(verdicts), flagged


def audit_local_pdfs(  # ---- 7
    roots: Iterable[PathLike],
    api_key: str | None = None,
    cache_path: PathLike | None = None,
) -> list[LocalPdfAudit]:
    """로컬 PDF 파일들의 SHA-256, 헤더/EOF 무결성, 위험 태그(/JS, /Launch 등), 위협 평판을 점검한다."""
    pdfs = discover_local_pdfs(roots)
    partial: list[tuple[Path, str, bool, bool, tuple[str, ...]]] = []
    for pdf in pdfs:
        sha = compute_sha256(pdf)
        valid_header, valid_eof, tags = inspect_pdf_bytes(pdf)
        partial.append((pdf, sha, valid_header, valid_eof, tags))

    verdicts = check_hashes_with_vt(
        (item[1] for item in partial),
        api_key=api_key,
        cache_path=cache_path,
    )

    audits: list[LocalPdfAudit] = []
    for pdf, sha, valid_header, valid_eof, tags in partial:
        audits.append(
            LocalPdfAudit(
                path=normalize_nfc(pdf),
                sha256=sha,
                valid_pdf_header=valid_header,
                valid_pdf_eof=valid_eof,
                dangerous_tags=tags,
                vt_verdict=verdicts.get(sha),
            )
        )
    return audits


# ======== 노트 ========
# 1 VirusTotal 조회 URL
#   설명: 파일을 업로드하지 않고 SHA-256 해시로만 평판을 묻는 v3 엔드포인트다. API 키는 환경 변수(VIRUSTOTAL_API_KEY)로만 받고 코드·로그에 남기지 않는다.
#   관련: .env.example
# 2 프로바이더 계약
#   설명: query_hash 하나만 요구하는 Protocol이다. VirusTotal 외의 서비스나 테스트용 가짜 구현을 같은 자리에 꽂을 수 있다.
#   개념: structural typing(Protocol)
# 3 손상 캐시는 빈 캐시
#   설명: 캐시 파일이 없거나 읽기·JSON 오류면 예외 없이 빈 사전으로 시작한다. 캐시는 재생성 가능한 가속 장치이기 때문이다.
# 4 판정 매핑
#   설명: 악성 탐지 수가 0 초과면 MALICIOUS, 의심이 0 초과면 SUSPICIOUS, 아니면 CLEAN. 404는 NOT_FOUND, 그 외 실패는 ERROR다.
#   관련: src/doctree/runtime_env/boundary_errors.py (execute_json_http_get)
# 5 캐시 우선 조회
#   설명: 캐시에 있고 ERROR가 아닌 판정은 재사용하고, 나머지만 프로바이더에 묻는다. ERROR는 캐시하지 않아 다음 실행에서 재시도된다. 프로바이더도 API 키도 없으면 원격 조회 없이 캐시만 쓴다.
#   개념: 캐시 우선(read-through)
# 6 스냅샷 해시 감사
#   설명: MALICIOUS/SUSPICIOUS만 flagged로 돌려준다. 반환하는 첫 값은 판정을 얻은 해시 수다.
#   관련: src/doctree/doctree_cli/command_dispatcher.py (cmd_update, cmd_audit)
# 7 로컬 PDF 감사
#   설명: 해시 계산, 바이트 검사, 평판 조회를 합쳐 파일당 LocalPdfAudit를 만든다.
#   관련: src/doctree/pdf_threat_audit/pdf_byte_inspector.py
