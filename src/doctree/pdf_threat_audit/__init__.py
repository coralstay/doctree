# ---
# title: pdf_threat_audit/__init__
# description: pdf_threat_audit 도메인의 공개 API를 지연 import로 노출하는 패키지 초기화 (폐기된 PDF 파이프라인 포함)
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""pdf_threat_audit 도메인: 로컬 PDF 바이트 검사기, 페이지별 파서 및 VirusTotal 위협 감사 클라이언트."""
from __future__ import annotations

from types import MappingProxyType

from doctree.runtime_env.workspace_settings import resolve_lazy_attribute

_EXPORTS = MappingProxyType({
    "DANGEROUS_PDF_TAGS": "pdf_byte_inspector",
    "LocalPdfAudit": "pdf_byte_inspector",
    "compute_sha256": "pdf_byte_inspector",
    "discover_local_pdfs": "pdf_byte_inspector",
    "inspect_pdf_bytes": "pdf_byte_inspector",
    "ContentParseResult": "pdf_page_ingestor",
    "PageVerificationRecord": "pdf_page_ingestor",
    "RawPdfPage": "pdf_page_ingestor",
    "extract_raw_pdf_pages": "pdf_page_ingestor",
    "parse_pdf_to_content_data": "pdf_page_ingestor",
    "render_dot_to_svg": "pdf_page_ingestor",
    "verify_code_and_dot_blocks": "pdf_page_ingestor",
    "verify_content_data": "pdf_page_ingestor",
    "verify_single_page": "pdf_page_ingestor",
    "ThreatIntelProvider": "threat_intel_client",
    "VerdictStatus": "threat_intel_client",
    "VirusTotalClient": "threat_intel_client",
    "VtVerdict": "threat_intel_client",
    "audit_drive_records": "threat_intel_client",
    "audit_local_pdfs": "threat_intel_client",
    "check_hashes_with_vt": "threat_intel_client",
    "load_vt_cache": "threat_intel_client",
    "query_virustotal_hash": "threat_intel_client",
    "save_vt_cache": "threat_intel_client",
})

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    return resolve_lazy_attribute(__name__, globals(), _EXPORTS, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})


