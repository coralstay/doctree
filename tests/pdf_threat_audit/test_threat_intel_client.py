# ---
# title: test_threat_intel_client
# description: VirusTotal 해시 평판 클라이언트와 캐시의 테스트
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""VirusTotal API v3 해시 평판 조회 클라이언트 및 캐시(doctree.pdf_threat_audit.threat_intel_client) 단위·통합 테스트."""
import hashlib
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib import error as urllib_error

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from doctree.doctree_cli import command_dispatcher as cli
from doctree.pdf_threat_audit.threat_intel_client import (
    ThreatIntelProvider,
    VerdictStatus,
    VirusTotalClient,
    VtVerdict,
    _extract_analysis_stats,
    _parse_cached_verdict,
    audit_drive_records,
    check_hashes_with_vt,
    load_vt_cache,
    query_virustotal_hash,
    save_vt_cache,
)


def _make_pdf(path: Path, body: bytes = b"1 0 obj << /Type /Catalog >> endobj\n") -> str:
    content = b"%PDF-1.7\n" + body + b"\n%%EOF\n"
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_query_virustotal_hash_parses_clean_suspicious_malicious_and_errors():
    clean_payload = json.dumps({
        "data": {"attributes": {"last_analysis_stats": {"malicious": 0, "suspicious": 0, "harmless": 65, "undetected": 5}}}
    }).encode("utf-8")
    with patch("doctree.pdf_threat_audit.threat_intel_client.urllib_request.urlopen", return_value=_FakeResp(clean_payload)):
        v = query_virustotal_hash("a" * 64, "fake-key")
        assert v == VtVerdict(sha256="a" * 64, status=VerdictStatus.CLEAN, malicious=0, suspicious=0, harmless=65, undetected=5)

    susp_payload = json.dumps({
        "data": {"attributes": {"last_analysis_stats": {"malicious": 0, "suspicious": 2, "harmless": 60, "undetected": 8}}}
    }).encode("utf-8")
    with patch("doctree.pdf_threat_audit.threat_intel_client.urllib_request.urlopen", return_value=_FakeResp(susp_payload)):
        v_susp = query_virustotal_hash("s" * 64, "fake-key")
        assert v_susp.status == VerdictStatus.SUSPICIOUS
        assert v_susp.suspicious == 2

    mal_payload = json.dumps({
        "data": {"attributes": {"last_analysis_stats": {"malicious": 5, "suspicious": 1, "harmless": 50, "undetected": 10}}}
    }).encode("utf-8")
    with patch("doctree.pdf_threat_audit.threat_intel_client.urllib_request.urlopen", return_value=_FakeResp(mal_payload)):
        v2 = query_virustotal_hash("b" * 64, "fake-key")
        assert v2.status == VerdictStatus.MALICIOUS
        assert v2.malicious == 5

    not_found = urllib_error.HTTPError("url", 404, "Not Found", {}, None)
    with patch("doctree.pdf_threat_audit.threat_intel_client.urllib_request.urlopen", side_effect=not_found):
        assert query_virustotal_hash("c" * 64, "fake-key").status == VerdictStatus.NOT_FOUND

    rate_limit = urllib_error.HTTPError("url", 429, "Too Many Requests", {}, None)
    with patch("doctree.pdf_threat_audit.threat_intel_client.urllib_request.urlopen", side_effect=rate_limit):
        v_429 = query_virustotal_hash("c" * 64, "fake-key")
        assert v_429.status == VerdictStatus.ERROR
        assert v_429.detail == "HTTP 429"

    with patch("doctree.pdf_threat_audit.threat_intel_client.urllib_request.urlopen", side_effect=TimeoutError("vt timed out")):
        v_timeout = query_virustotal_hash("c" * 64, "fake-key")
        assert v_timeout.status == VerdictStatus.ERROR
        assert "vt timed out" in v_timeout.detail


@pytest.mark.parametrize(
    "corrupt_content",
    [
        "{broken json",
        "[1, 2, 3]",
        '{"sha1": {"malicious": "not_an_int"}}',
        '{"sha1": {"status": "unknown_verdict_xyz", "malicious": 0}}',
        '{"sha1": {"status": "clean", "malicious": -1}}',
        '{"sha1": {"status": "clean", "malicious": true}}',
    ],
)
def test_load_vt_cache_handles_corrupt_or_malformed_files_safely(tmp_path, corrupt_content):
    cache_file = tmp_path / ".vt_cache.json"
    assert load_vt_cache(cache_file) == {}
    cache_file.write_text(corrupt_content, encoding="utf-8")
    assert load_vt_cache(cache_file) == {}


def test_load_vt_cache_skips_non_dict_entries_and_error_verdicts_are_not_persisted(tmp_path):
    cache_file = tmp_path / ".vt_cache.json"
    cache_file.write_text(
        json.dumps({
            "ignored-entry": "not-a-dict",
            "cached-err": {"status": "error", "detail": "HTTP 429"},
            "cached-clean": {"status": "clean", "harmless": 70},
        }),
        encoding="utf-8",
    )
    loaded = load_vt_cache(cache_file)
    assert "ignored-entry" not in loaded
    assert loaded["cached-clean"].status == VerdictStatus.CLEAN

    # API 키도 없고 provider도 없으면 캐시된 정상 항목만 반환하고 나머지는 건너뛴다
    res_no_key = check_hashes_with_vt(["cached-clean", "uncached-sha", ""], api_key=None, cache_path=cache_file)
    assert list(res_no_key.keys()) == ["cached-clean"]

    # ThreatIntelProvider 프로토콜 런타임 준수 및 커스텀 provider 주입 검증
    vt_client = VirusTotalClient(api_key="fake-key")
    assert isinstance(vt_client, ThreatIntelProvider)

    class _StubProvider:
        def query_hash(self, sha256: str) -> VtVerdict:
            return VtVerdict(sha256=sha256, status=VerdictStatus.CLEAN, harmless=99)

    assert isinstance(_StubProvider(), ThreatIntelProvider)
    res_provider = check_hashes_with_vt(["stub-sha"], api_key=None, cache_path=cache_file, provider=_StubProvider())
    assert res_provider["stub-sha"].harmless == 99

    # ERROR 상태인 캐시는 재조회 대상이 되며, 새로 조회한 결과가 ERROR면 캐시 파일을 덮어쓰지 않는다
    with patch("doctree.pdf_threat_audit.threat_intel_client.query_virustotal_hash") as mock_query:
        mock_query.return_value = VtVerdict(sha256="cached-err", status=VerdictStatus.ERROR, detail="still failing")
        res_err = check_hashes_with_vt(["cached-err"], api_key="key", cache_path=cache_file)
    assert res_err["cached-err"].status == VerdictStatus.ERROR

    # 정상 조회 결과는 캐시에 저장되고 반환된다
    with patch("doctree.pdf_threat_audit.threat_intel_client.query_virustotal_hash") as mock_query2:
        mock_query2.return_value = VtVerdict(sha256="brand-new", status=VerdictStatus.MALICIOUS, malicious=7, detail="vt")
        res_new = check_hashes_with_vt(["cached-clean", "brand-new"], api_key="key", cache_path=cache_file)
    assert res_new["brand-new"].status == VerdictStatus.MALICIOUS
    assert res_new["brand-new"].malicious == 7
    reloaded = load_vt_cache(cache_file)
    assert reloaded["brand-new"].malicious == 7

    # 디스크 쓰기 장애(OSError) 시 기존 VT 캐시 파일이 100% 보존되고 임시 파일이 남지 않는다
    with (
        patch.object(Path, "replace", side_effect=OSError("simulated vt disk full")),
        pytest.raises(OSError, match="simulated vt disk full"),
    ):
        save_vt_cache(cache_file, {"corrupt": VtVerdict(sha256="corrupt", status=VerdictStatus.CLEAN)})
    assert load_vt_cache(cache_file) == reloaded
    assert not list(tmp_path.glob(".*.tmp"))


def test_audit_drive_records_flags_malicious_and_suspicious(tmp_path):
    cache_file = tmp_path / ".vt_cache.json"
    save_vt_cache(
        cache_file,
        {
            "sha-clean": VtVerdict(sha256="sha-clean", status=VerdictStatus.CLEAN, harmless=60),
            "sha-susp": VtVerdict(sha256="sha-susp", status=VerdictStatus.SUSPICIOUS, suspicious=2),
            "sha-mal": VtVerdict(sha256="sha-mal", status=VerdictStatus.MALICIOUS, malicious=4),
        },
    )
    records = [
        {"id": "1", "name": "clean.pdf", "sha256": "sha-clean"},
        {"id": "2", "name": "susp.pdf", "sha256": "sha-susp"},
        {"id": "3", "name": "mal.pdf", "sha256": "sha-mal"},
    ]
    checked, flagged = audit_drive_records(records, api_key=None, cache_path=cache_file)
    assert checked == 3
    assert [r["id"] for r, _ in flagged] == ["2", "3"]


def _resolve_vt_stub_response(
    sha: str,
    vt_spec: dict,
    clean_sha: str,
    mal_sha: str,
    transient_err_sha: str,
    fail_transient_once: dict[str, bool],
) -> tuple[int, dict | None]:
    if sha == transient_err_sha and fail_transient_once["should_fail"]:
        fail_transient_once["should_fail"] = False
        return vt_spec["responses"]["rate_limited"]["status"], None
    if sha in (clean_sha, transient_err_sha):
        resp = vt_spec["responses"]["clean"]
        return resp["status"], resp["body"]
    if sha == mal_sha:
        resp = vt_spec["responses"]["malicious"]
        return resp["status"], resp["body"]
    return vt_spec["responses"]["not_found"]["status"], None


def _start_vt_stub_server(
    vt_spec: dict,
    clean_sha: str,
    mal_sha: str,
    transient_err_sha: str,
):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    request_log: list[tuple[str, dict]] = []
    fail_transient_once = {"should_fail": True}

    class ThreatIntelStubHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def _send_json(self, status: int, body: dict | None = None):
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body is not None:
                self.wfile.write(json.dumps(body).encode("utf-8"))

        def do_GET(self):
            request_log.append((self.path, dict(self.headers)))
            req_hdr = vt_spec["required_header"]
            if self.headers.get(req_hdr["name"]) != req_hdr["value"]:
                return self._send_json(vt_spec["unauthorized_status"])
            sha = self.path.rsplit("/", 1)[-1]
            status, body = _resolve_vt_stub_response(
                sha, vt_spec, clean_sha, mal_sha, transient_err_sha, fail_transient_once
            )
            return self._send_json(status, body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ThreatIntelStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, request_log


@pytest.mark.integration
def test_integration_real_loopback_http_server_with_vt_and_disk_cache(tmp_path):
    """[Integration] http_cassettes.yaml 명세를 로드하여 실제 로컬 루프백 TCP HTTP 서버(127.0.0.1)에서 VT 프로토콜 통신 및 디스크 캐시 연동을 검증한다."""
    import yaml

    from doctree.pdf_threat_audit.threat_intel_client import audit_local_pdfs

    cassette_path = Path(__file__).with_name("http_cassettes.yaml")
    cassettes = yaml.safe_load(cassette_path.read_text(encoding="utf-8"))["endpoints"]
    vt_spec = cassettes["virustotal_v3"]

    clean_pdf = tmp_path / "clean_doc.pdf"
    mal_pdf = tmp_path / "malicious_doc.pdf"
    clean_sha = _make_pdf(clean_pdf, body=b"1 0 obj << /Type /Page >> endobj")
    mal_sha = _make_pdf(mal_pdf, body=b"1 0 obj << /OpenAction << /S /Launch >> >> endobj")
    transient_err_sha = "9" * 64

    server, request_log = _start_vt_stub_server(vt_spec, clean_sha, mal_sha, transient_err_sha)
    port = server.server_port

    cache_file = tmp_path / ".vt_cache.json"
    try:
        with patch(
            "doctree.pdf_threat_audit.threat_intel_client.VT_API_URL",
            f"http://127.0.0.1:{port}/vt/files/{{sha256}}",
        ):
            # 1차 실행: 실제 TCP 소켓을 통해 로컬 PDF 2종 해시 조회 후 .vt_cache.json 디스크 기록
            audits_first = audit_local_pdfs([clean_pdf, mal_pdf], api_key="real-vt-key", cache_path=cache_file)
            assert len(audits_first) == 2
            assert audits_first[0].is_safe is True
            assert audits_first[0].vt_verdict.harmless == 71
            assert audits_first[1].is_safe is False
            assert audits_first[1].vt_verdict.status == VerdictStatus.MALICIOUS
            assert audits_first[1].vt_verdict.malicious == 9
            assert audits_first[1].dangerous_tags == ("/Launch",)
            assert len(request_log) == 2
            assert cache_file.is_file()

            # 2차 실행: 디스크 캐시(.vt_cache.json) 히트로 추가 네트워크 요청이 0건이어야 한다
            audits_second = audit_local_pdfs([clean_pdf, mal_pdf], api_key="real-vt-key", cache_path=cache_file)
            assert len(request_log) == 2
            assert audits_second[0].vt_verdict.harmless == 71

            # 일시적 HTTP 429 장애 -> 캐시에 저장되지 않음 -> 재시도 시 HTTP 200 복구 및 캐시 저장
            v_err = check_hashes_with_vt([transient_err_sha], api_key="real-vt-key", cache_path=cache_file)
            assert v_err[transient_err_sha].status == VerdictStatus.ERROR
            assert transient_err_sha not in load_vt_cache(cache_file)

            v_recovered = check_hashes_with_vt([transient_err_sha], api_key="real-vt-key", cache_path=cache_file)
            assert v_recovered[transient_err_sha].status == VerdictStatus.CLEAN
            assert transient_err_sha in load_vt_cache(cache_file)
    finally:
        server.shutdown()
        server.server_close()


def test_audit_cli_checks_drive_snapshot_and_local_pdfs(tmp_path, capsys):
    snap = tmp_path / "2026-10-27.json"
    clean_pdf = tmp_path / "clean.pdf"
    js_pdf = tmp_path / "with_js.pdf"
    clean_sha = _make_pdf(clean_pdf)
    js_sha = _make_pdf(js_pdf, body=b"<< /S /JavaScript >>")

    snap.write_text(
        json.dumps({"count": 1, "records": [{"id": "1", "name": "clean.pdf", "sha256": clean_sha}]}),
        encoding="utf-8",
    )
    vt_cache = tmp_path / ".vt_cache.json"
    save_vt_cache(vt_cache, {clean_sha: VtVerdict(sha256=clean_sha, status=VerdictStatus.CLEAN, harmless=68)})

    code = cli.main([
        "audit",
        str(clean_pdf),
        "--snapshot",
        str(snap),
        "--vt-cache",
        str(vt_cache),
        "--strict",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "주의/위협 0개" in out

    code_bad = cli.main([
        "audit",
        str(js_pdf),
        "--snapshot",
        str(snap),
        "--vt-cache",
        str(vt_cache),
        "--strict",
    ])
    assert code_bad == 1
    out_bad = capsys.readouterr().out
    assert "/JavaScript" in out_bad
    assert js_sha[:16] in out_bad


_STAT_VAL_STRATEGY = st.one_of(st.none(), st.booleans(), st.integers(min_value=-10, max_value=100), st.text(max_size=8))


@settings(max_examples=80, deadline=None)
@given(
    sha=st.one_of(st.none(), st.text(max_size=20)),
    status=st.one_of(st.sampled_from([s.value for s in VerdictStatus]), st.text(max_size=10)),
    mal=_STAT_VAL_STRATEGY,
    susp=_STAT_VAL_STRATEGY,
    detail=st.one_of(st.none(), st.booleans(), st.integers(min_value=-5, max_value=5), st.text(max_size=20)),
)
@example(sha="  ABC123  ", status="clean", mal=0, susp=0, detail=None)
@example(sha="  ABC123  ", status="clean", mal=0, susp=0, detail=0)
@example(sha="sha-bool", status="clean", mal=True, susp=0, detail="x")
def test_property_fuzz_parse_cached_verdict_and_extract_analysis_stats(
    sha: Any,
    status: str,
    mal: Any,
    susp: Any,
    detail: Any,
) -> None:
    """_parse_cached_verdict와 _extract_analysis_stats가 불리언·음수·None detail을 방어하고 음이 아닌 정수만 수용한다."""
    raw_item = {"status": status, "malicious": mal, "suspicious": susp, "detail": detail}
    verdict = _parse_cached_verdict(sha, raw_item)
    if verdict is not None:
        assert verdict.sha256 == sha.strip().lower()
        assert type(verdict.malicious) is int and verdict.malicious >= 0
        assert type(verdict.suspicious) is int and verdict.suspicious >= 0
        assert verdict.detail == ("" if detail is None else str(detail))

    stats = _extract_analysis_stats({"data": {"attributes": {"last_analysis_stats": raw_item}}})
    assert all(type(v) is int and v >= 0 for v in stats.values())

