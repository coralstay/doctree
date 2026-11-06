# ---
# title: command_dispatcher
# description: 'doctree CLI의 명령어 파서와 디스패처: 드라이브 스냅샷·분류·레이아웃·감사 명령을 실행한다'
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""doctree: 구글 드라이브 콘텐츠(contents) PDF 스냅샷, 분류, 레이아웃 규격 및 보안 감사 관리 CLI."""
from __future__ import annotations

import argparse
import functools
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from doctree import __version__
from doctree.content_taxonomy.category_classifier import (
    ClassificationError,
    classify_records,
    load_catalog,
)
from doctree.content_taxonomy.duplicate_and_naming_linter import run_checks
from doctree.content_taxonomy.practice_kata_builder import (
    backfill_practice_pitfalls,
    build_practice_katas,
    verify_practice_katas,
)
from doctree.content_taxonomy.snippet_registry import (
    export_snippets_blog_posts,
    export_snippets_markdown,
    format_snippets_report,
    sync_snippet_registry,
    verify_snippet,
)
from doctree.doctree_cli.terminal_tree_renderer import (
    console,
    format_classification_summary,
    format_local_audit_reasons,
    print_account_dashboard,
)
from doctree.drive_sync.drive_pdf_fetcher import DriveListError, GoogleDriveManager
from doctree.drive_sync.snapshot_diff_tracker import (
    KST,
    change_summary,
    load_latest_snapshot,
    load_snapshot_records,
    record_run,
)
from doctree.drive_sync.workspace_sync_engine import (
    compare_drive_and_local,
    discover_workspace_content_dirs,
    format_sync_report,
    pull_drive_pdf_to_project,
    scan_workspace_contents,
)
from doctree.pdf_threat_audit.pdf_page_ingestor import parse_pdf_to_content_data
from doctree.pdf_threat_audit.threat_intel_client import audit_drive_records, audit_local_pdfs
from doctree.runtime_env.boundary_errors import (
    AUTH_MISSING_MESSAGE,
    SNAPSHOT_PRESERVED_NOTICE,
    BoundaryKind,
    DoctreeBoundaryError,
    DriveAuthError,
    SecurityAuditError,
    coerce_boundary_error,
)
from doctree.runtime_env.layout_profile_store import (
    OUTPUT_FORMATS,
    add_layout_items,
    format_layout_value,
    get_layout_value,
    load_layout_profiles,
    remove_layout_items,
    set_layout_field,
)
from doctree.runtime_env.project_scaffolder import scaffold_project, verify_project_scaffold
from doctree.runtime_env.workspace_settings import (
    CATEGORY_TREE_FILE,
    CONTENT_CATEGORY_MAP_FILE,
    GOOGLE_DRIVE_FOLDER_ID,
    GOOGLE_SERVICE_ACCOUNT_FILE,
    LOCAL_CONTENTS_CACHE_FILE,
    PROJECT_LAYOUT_PROFILES_FILE,
    SNAPSHOTS_DIR,
    VIRUSTOTAL_API_KEY,
    VT_CACHE_FILE,
    WORKSPACE_DIR,
    discover_local_pdfs,
)

__all__ = [
    "DoctreeHelpFormatter",
    "build_drive",
    "build_parser",
    "cmd_audit",
    "cmd_auth",
    "cmd_classify",
    "cmd_help",
    "cmd_layout",
    "cmd_parse",
    "cmd_practice",
    "cmd_pull",
    "cmd_scaffold",
    "cmd_snippet",
    "cmd_status",
    "cmd_sync",
    "cmd_update",
    "main",
    "now_kst",
]


def build_drive() -> GoogleDriveManager:
    """기존 credentials.json / token.json 위치 그대로 Drive 클라이언트를 만든다 (재로그인 불필요)."""
    return GoogleDriveManager(
        service_account_file=GOOGLE_SERVICE_ACCOUNT_FILE,
        folder_id=GOOGLE_DRIVE_FOLDER_ID,
    )


def _render_boundary_error(
    error: BaseException,
    *,
    default_boundary: BoundaryKind = BoundaryKind.FILESYSTEM,
    preserve_snapshot: bool = False,
) -> int:
    """모든 CLI 경계 예외를 일관된 포맷(❌ <경계별 접두어>: <상세>)과 종료 코드 1로 렌더링한다."""
    coerced = coerce_boundary_error(
        error,
        default_boundary=default_boundary,
        preserve_snapshot=preserve_snapshot,
    )
    console.print(f"❌ {coerced.format_error()}", style="red", markup=False, soft_wrap=True)
    if coerced.preserve_snapshot:
        console.print(f"[yellow]{SNAPSHOT_PRESERVED_NOTICE}[/yellow]", soft_wrap=True)
    return 1


_CLI_CAUGHT_ERRORS = (DoctreeBoundaryError, OSError, UnicodeError, json.JSONDecodeError)


def _boundary_guarded(
    default_boundary: BoundaryKind = BoundaryKind.FILESYSTEM,
    *,
    preserve_snapshot: bool = False,
) -> Callable[[Callable[[argparse.Namespace], int]], Callable[[argparse.Namespace], int]]:
    """CLI 커맨드 핸들러의 경계 예외를 일관되게 포착해 _render_boundary_error로 위임하는 데코레이터."""

    def decorator(func: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
        @functools.wraps(func)
        def wrapper(args: argparse.Namespace) -> int:
            try:
                return func(args)
            except _CLI_CAUGHT_ERRORS as error:
                return _render_boundary_error(
                    error,
                    default_boundary=default_boundary,
                    preserve_snapshot=preserve_snapshot,
                )

        return wrapper

    return decorator


def _print_account(drive: GoogleDriveManager) -> int:
    if not drive.service:
        return _render_boundary_error(DriveAuthError(AUTH_MISSING_MESSAGE))
    print_account_dashboard(drive.get_account_and_quota())
    return 0


def cmd_auth(_args: argparse.Namespace) -> int:
    """Google OAuth 2.0 인증(token.json 생성·갱신) 후 계정 대시보드를 출력한다."""
    console.print("\n[bold cyan]🔑 Google Drive OAuth 2.0 인증[/bold cyan]", soft_wrap=True)
    drive = build_drive()
    code = _print_account(drive)
    if code == 0:
        console.print("[bold green]✅ 구글 드라이브 연동 성공[/bold green]", soft_wrap=True)
    return code


def cmd_status(_args: argparse.Namespace) -> int:
    """구글 드라이브 계정과 스토리지 용량을 출력한다."""
    console.print("\n[bold cyan]📊 Google Drive 계정 상태[/bold cyan]", soft_wrap=True)
    return _print_account(build_drive())


def now_kst() -> datetime:
    """한국 표준시(KST, UTC+9)가 붙은 현재 시각 (테스트에서 바꿔 끼운다)."""
    return datetime.now(KST)


def _resolve_snapshot_records(
    snapshot: Path | None,
    snapshots_dir: Path,
    *,
    required: bool = True,
) -> list:
    if snapshot is not None:
        return load_snapshot_records(snapshot, error_cls=ClassificationError)
    latest = load_latest_snapshot(snapshots_dir)
    if latest is None:
        if not required:
            return []
        raise ClassificationError(
            "저장된 스냅샷이 없습니다. --snapshot을 지정하거나 `doctree update`를 먼저 실행하세요."
        )
    _, records = latest
    return records


@_boundary_guarded(BoundaryKind.TAXONOMY)
def cmd_classify(args: argparse.Namespace) -> int:
    """고정 분류표로 로컬 스냅샷을 검토하여 터미널에 출력한다. Drive 인증·조회는 수행하지 않는다."""
    catalog = load_catalog(args.category_tree, args.content_map)
    records = _resolve_snapshot_records(args.snapshot, Path(SNAPSHOTS_DIR))
    result = classify_records(records, catalog, strict=args.strict)
    summary = format_classification_summary(result)
    console.print(summary, markup=False, highlight=False, soft_wrap=True, end="")
    return 0


@_boundary_guarded(BoundaryKind.FILESYSTEM, preserve_snapshot=True)
def cmd_update(args: argparse.Namespace) -> int:
    """조회 → 분류 → 점검 → snapshots/YYYY-MM-DD.json 저장 및 터미널 요약 출력."""
    snapshots_dir = Path(args.snapshots_dir or SNAPSHOTS_DIR)
    category_tree_path = Path(args.category_tree or CATEGORY_TREE_FILE)
    content_map_path = Path(args.content_map or CONTENT_CATEGORY_MAP_FILE)
    console.print("\n[bold cyan]📚 콘텐츠 스냅샷 갱신[/bold cyan]", soft_wrap=True)

    drive = build_drive()
    if not drive.service:
        raise DriveAuthError(AUTH_MISSING_MESSAGE, preserve_snapshot=True)
    records = drive.list_pdf_files()
    if not records:
        raise DriveListError(
            "드라이브에서 PDF를 0개 조회했습니다. 빈 스냅샷을 막기 위해 중단합니다 (폴더 ID·권한 확인).",
            preserve_snapshot=True,
        )

    catalog = load_catalog(category_tree_path, content_map_path)
    result = classify_records(records, catalog, strict=args.strict)

    warnings = run_checks(records)
    vt_checked, vt_flagged = audit_drive_records(
        records,
        api_key=VIRUSTOTAL_API_KEY or None,
        cache_path=VT_CACHE_FILE,
    )
    if vt_flagged and args.strict:
        flagged_names = ", ".join(r.get("name", r.get("id", "")) for r, _ in vt_flagged)
        raise SecurityAuditError(
            f"VirusTotal 위협 감지 {len(vt_flagged)}건: {flagged_names}",
            preserve_snapshot=True,
        )

    now = now_kst()
    run = record_run(records, snapshots_dir, now)

    unclassified = len(result.unclassified)
    console.print(
        f"[bold green]✅ 총 {run.total}권[/bold green] · 분류 {run.total - unclassified}권 · "
        f"미분류 {unclassified}권 · 경고 {len(warnings)}건",
        soft_wrap=True,
    )
    if vt_checked:
        console.print(
            f"🛡️ VirusTotal(SHA-256) 확인: {vt_checked}건 · 위협 {len(vt_flagged)}건",
            soft_wrap=True,
        )
    console.print(f"🔄 최근 변경: {change_summary(run)}", soft_wrap=True)
    console.print(f"🗂️ 스냅샷: {run.snapshot_path}", soft_wrap=True)
    return 0


@_boundary_guarded(BoundaryKind.FILESYSTEM)
def cmd_audit(args: argparse.Namespace) -> int:
    """드라이브 스냅샷(SHA-256) 및 로컬 contents/*.pdf 보안·무결성 점검을 수행한다."""
    snapshots_dir = Path(args.snapshots_dir or SNAPSHOTS_DIR)
    api_key = args.vt_api_key if args.vt_api_key is not None else (VIRUSTOTAL_API_KEY or None)
    cache_path = Path(args.vt_cache or VT_CACHE_FILE)

    records = _resolve_snapshot_records(args.snapshot, snapshots_dir, required=False)
    vt_checked, vt_flagged = audit_drive_records(records, api_key=api_key, cache_path=cache_path)

    roots = list(args.paths) if args.paths else discover_workspace_content_dirs(WORKSPACE_DIR)
    local_audits = audit_local_pdfs(roots, api_key=api_key, cache_path=cache_path)
    local_flagged = [a for a in local_audits if not a.is_safe]

    console.print(
        f"🛡️ 보안 점검 요약: 드라이브 스냅샷 {len(records)}권(VT 조회 {vt_checked}건, 위협 {len(vt_flagged)}건) · "
        f"로컬 PDF {len(local_audits)}개(주의/위협 {len(local_flagged)}개)",
        markup=False,
        soft_wrap=True,
    )
    for rec, verdict in vt_flagged:
        console.print(
            f"[드라이브 VT 경고] {rec.get('name')} (sha256={verdict.sha256}, "
            f"status={verdict.status}, malicious={verdict.malicious}, suspicious={verdict.suspicious})",
            style="red",
            markup=False,
            soft_wrap=True,
        )
    for audit in local_flagged:
        reasons = format_local_audit_reasons(audit)
        console.print(
            f"[로컬 PDF 주의] {audit.path} (sha256={audit.sha256[:16]}... · {'; '.join(reasons)})",
            style="yellow",
            markup=False,
            soft_wrap=True,
        )

    if args.strict and (vt_flagged or local_flagged):
        return 1
    return 0


@_boundary_guarded(BoundaryKind.TAXONOMY)
def cmd_layout(args: argparse.Namespace) -> int:
    """프로젝트 레이아웃 프로필(config/project_layout_profiles.yaml)을 CLI로 조회 및 원자적 수정한다."""
    layout_path = Path(args.file or PROJECT_LAYOUT_PROFILES_FILE)
    if args.layout_action == "get":
        data = load_layout_profiles(layout_path)
        value = get_layout_value(data, profile=args.profile, field=args.field)
        rendered = format_layout_value(value, fmt=args.format)
        console.print(rendered, markup=False, highlight=False, soft_wrap=True, end="")
        return 0
    if args.layout_action == "set":
        updated_profile = set_layout_field(layout_path, args.profile, args.field, args.values)
        console.print(
            f"✅ 레이아웃 프로필 갱신 완료: {args.profile}.{args.field} -> {updated_profile[args.field]}",
            markup=False,
            soft_wrap=True,
        )
        return 0
    if args.layout_action == "add":
        updated_list = add_layout_items(layout_path, args.profile, args.field, args.items)
        console.print(
            f"✅ 레이아웃 항목 추가 완료: {args.profile}.{args.field} ({len(updated_list)}개 항목)",
            markup=False,
            soft_wrap=True,
        )
        return 0
    updated_list = remove_layout_items(layout_path, args.profile, args.field, args.items)
    console.print(
        f"✅ 레이아웃 항목 제거 완료: {args.profile}.{args.field} ({len(updated_list)}개 항목)",
        markup=False,
        soft_wrap=True,
    )
    return 0


def _resolve_cache_file(args: argparse.Namespace, snapshots_dir: Path) -> Path:
    if args.cache_file:
        return Path(args.cache_file)
    if args.snapshots_dir:
        return snapshots_dir / ".local_contents_cache.json"
    if args.snapshot:
        return Path(args.snapshot).parent / ".local_contents_cache.json"
    return Path(LOCAL_CONTENTS_CACHE_FILE)


@_boundary_guarded(BoundaryKind.FILESYSTEM)
def cmd_sync(args: argparse.Namespace) -> int:
    """Step 1: 드라이브 스냅샷(SHA-256)과 로컬 워크스페이스 contents/(SHA-256)의 3-Way 정합성을 판별한다."""
    snapshots_dir = Path(args.snapshots_dir or SNAPSHOTS_DIR)
    ws_dir = Path(args.workspace_dir or WORKSPACE_DIR)
    cache_file = _resolve_cache_file(args, snapshots_dir)
    records = _resolve_snapshot_records(args.snapshot, snapshots_dir)
    local_entries, hits, recomp = scan_workspace_contents(ws_dir, cache_path=cache_file)
    comparison = compare_drive_and_local(
        records,
        local_entries,
        cache_hits=hits,
        recomputed=recomp,
    )
    report = format_sync_report(comparison, len(records), len(local_entries))
    console.print(report, markup=False, highlight=False, soft_wrap=True, end="")
    return 0


@_boundary_guarded(BoundaryKind.FILESYSTEM)
def cmd_pull(args: argparse.Namespace) -> int:
    """Step 1: 드라이브 스냅샷에서 도서를 찾아 특정 로컬 프로젝트의 contents/(및 하위 경로) 폴더로 내려받는다."""
    snapshots_dir = Path(args.snapshots_dir or SNAPSHOTS_DIR)
    ws_dir = Path(args.workspace_dir or WORKSPACE_DIR)
    cache_file = _resolve_cache_file(args, snapshots_dir)
    records = _resolve_snapshot_records(args.snapshot, snapshots_dir)
    drive = build_drive()
    if not drive.service:
        raise DriveAuthError(AUTH_MISSING_MESSAGE)
    rec, dest_path = pull_drive_pdf_to_project(
        drive,
        records,
        args.query,
        args.project,
        workspace_dir=ws_dir,
        subdir=args.subdir,
        output_name=args.output_name,
        cache_path=cache_file,
    )
    sha_preview = str(rec.get("sha256") or "")[:16]
    console.print(
        f"📥 도서 가져오기 완료: {rec['name']} ➔ {dest_path} (sha256={sha_preview}...)",
        markup=False,
        soft_wrap=True,
    )
    return 0


@_boundary_guarded(BoundaryKind.TAXONOMY)
def cmd_scaffold(args: argparse.Namespace) -> int:
    """Step 2: 로컬 프로젝트를 지정한 레이아웃 프로필(handcrafted/toolchain) 구조로 강제 생성 및 보정한다."""
    layout_path = Path(args.file or PROJECT_LAYOUT_PROFILES_FILE)
    if args.check:
        ok, missing = verify_project_scaffold(
            args.profile,
            args.path,
            layout_file=layout_path,
        )
        if not ok:
            raise ClassificationError(
                f"{args.path} 레이아웃 누락 항목 ({len(missing)}개): {', '.join(missing)}"
            )
        console.print(
            f"✅ 프로젝트 레이아웃 검증 통과: {Path(args.path).resolve()} (프로필: {args.profile})",
            markup=False,
            soft_wrap=True,
        )
        return 0

    res = scaffold_project(
        args.profile,
        args.path,
        layout_file=layout_path,
        overwrite=args.overwrite,
    )
    console.print(
        f"🏗️ 프로젝트 스캐폴딩 완료: {res.target_dir} (프로필: {res.profile} · "
        f"생성 폴더 {len(res.created_dirs)}개 · 생성 파일 {len(res.created_files)}개 · 보존 파일 {len(res.preserved_files)}개)",
        markup=False,
        soft_wrap=True,
    )
    return 0


def _discover_parse_targets(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    search_root = target / "contents" if (target / "contents").is_dir() else target
    pdfs = discover_local_pdfs([search_root])
    if not pdfs:
        raise ClassificationError(f"파싱할 PDF 파일을 찾을 수 없습니다: {target}")
    return pdfs


@_boundary_guarded(BoundaryKind.TAXONOMY)
def cmd_parse(args: argparse.Namespace) -> int:
    """Step 3: PDF를 페이지별 마크다운·크롭 PNG·Graphviz DOT SVG로 파싱하고 원본 동일성을 검증한다."""
    pdfs = _discover_parse_targets(Path(args.target))
    has_warnings = False
    for pdf_path in pdfs:
        res = parse_pdf_to_content_data(
            pdf_path,
            output_dir=args.output_dir,
            verify=args.verify or args.strict,
            dot_bin=args.dot_bin,
        )
        if res.warned_pages > 0:
            has_warnings = True
        console.print(
            f"📄 PDF 파싱 완료: {res.content_name} (총 {res.total_pages}페이지 · "
            f"PASS {res.passed_pages} · WARN {res.warned_pages} ➔ {res.index_json_path})",
            markup=False,
            soft_wrap=True,
        )
    if args.strict and has_warnings:
        return 1
    return 0


def _discover_practice_content_dirs(target: Path) -> list[Path]:
    if (target / "index.json").is_file():
        return [target]
    base = target / "content_data" if (target / "content_data").is_dir() else target
    return sorted(idx.parent for idx in base.rglob("index.json")) if base.is_dir() else []


def _run_practice_generation(args: argparse.Namespace) -> None:
    target = Path(args.target)
    content_dirs = _discover_practice_content_dirs(target)
    for cdir in content_dirs:
        res = build_practice_katas(
            cdir,
            practice_root=args.practice_dir,
            lang=args.lang,
            overwrite=args.overwrite,
            dot_bin=args.dot_bin,
        )
        console.print(
            f"🎯 실습(Kata) 생성 완료: {res.content_name} (장 {res.chapters_count}개 · "
            f"과제 {res.katas_count}개 · 언어 .{res.lang_ext} ➔ {res.practice_content_dir})",
            markup=False,
            soft_wrap=True,
        )
    practice_target = Path(args.practice_dir) if args.practice_dir else target
    backfilled = backfill_practice_pitfalls(
        practice_target,
        lang=args.lang,
        overwrite=args.overwrite,
    )
    if backfilled:
        console.print(
            f"🛠️ 기존 실습(Kata) PITFALLS.md 보강 완료: {len(backfilled)}개 절 폴더 ({practice_target.resolve()})",
            markup=False,
            soft_wrap=True,
        )
    if not content_dirs and not backfilled:
        raise ClassificationError(
            f"실습을 생성할 content_data 도서 디렉터리(index.json 포함)를 찾을 수 없습니다: {target}"
        )


@_boundary_guarded(BoundaryKind.TAXONOMY)
def cmd_practice(args: argparse.Namespace) -> int:
    """Step 4: content_data/<도서명>/ 중간 결과물을 조합해 practice/<도서명>/<장>/<절>/ 실습 코드를 생성하거나 검증(--check)한다."""
    if args.check:
        check_target = Path(args.practice_dir) if args.practice_dir else Path(args.target)
        ok, missing = verify_practice_katas(check_target)
        if not ok:
            raise ClassificationError(
                f"{check_target} 실습(Kata) 4대 표준 파일 누락 항목 ({len(missing)}개): {', '.join(missing)}"
            )
        console.print(
            f"✅ 실습(Kata) 4대 표준 파일 검증 통과: {check_target.resolve()}",
            markup=False,
            soft_wrap=True,
        )
        return 0

    _run_practice_generation(args)
    return 0


@_boundary_guarded(BoundaryKind.TAXONOMY)
def cmd_snippet(args: argparse.Namespace) -> int:
    """실습 코드(practice/) 내 [[snippet]] / @snippet 추출, 인간 검증(--verify) 및 마크다운·블로그 내보내기를 수행한다."""
    project_dir = Path(args.path).resolve()
    today_str = now_kst().strftime("%Y-%m-%d")
    acted = False

    if args.verify:
        verified = verify_snippet(
            project_dir,
            args.verify,
            reviewer=args.reviewer,
            verified_at=today_str,
        )
        console.print(
            f"✅ 스니펫 인간 검증 완료 ({len(verified)}건, reviewer={args.reviewer}): "
            + ", ".join(r.symbol for r in verified),
            markup=False,
            soft_wrap=True,
        )
        acted = True

    if args.export is not None:
        out_md = export_snippets_markdown(project_dir, args.export or None)
        console.print(f"📝 스니펫 마크다운 대시보드 내보내기 완료: {out_md}", markup=False, soft_wrap=True)
        acted = True

    if args.export_blog is not None:
        posts = export_snippets_blog_posts(
            project_dir,
            args.export_blog or None,
            date_prefix=today_str,
        )
        console.print(
            f"📰 기술 블로그 포스트 내보내기 완료 ({len(posts)}건): {posts[0].parent}",
            markup=False,
            soft_wrap=True,
        )
        acted = True

    if not acted or args.list or args.status:
        records = sync_snippet_registry(project_dir)
        report = format_snippets_report(records, query=args.query)
        console.print(report, markup=False, highlight=False, soft_wrap=True, end="")
    return 0


class DoctreeHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """한글 명령어·옵션 설명이 줄바꿈으로 잘리지 않도록 정렬 폭을 넓힌 도움말 포매터."""

    __slots__ = ()

    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=34, width=108)

    def add_argument(self, action: argparse.Action) -> None:
        if action.option_strings == ["-h", "--help"]:
            action.help = "이 도움말 메시지를 출력하고 종료"
        super().add_argument(action)


_ROOT_DESCRIPTION = """\
📚 doctree — 구글 드라이브 & 로컬 기술 도서 PDF 스냅샷·분류·레이아웃 규격·보안 감사 CLI

원본 PDF를 내려받지 않고 Google Drive 메타데이터(file_id, SHA-256, 폴더 경로, 용량)만 수집해
일별 불변 스냅샷(snapshots/YYYY-MM-DD.json)으로 기록하고, 16개 대분야·55개 세부분야 고정 분류 체계 검증,
다일차 변경 추적(Diff), 중복·파일명 린팅, 프로젝트 레이아웃 프로필 입출력, 다운로드 전·후 이중 PDF 보안 감사,
그리고 로컬 워크스페이스 4단계 파이프라인(동기화 ➔ 스캐폴딩 ➔ 페이지별 파싱·검증 ➔ 실습 Kata 생성)을 수행합니다.\
"""

_ROOT_EPILOG = """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🧭 [1] 스냅샷 · 분류 · 보안 감사 · 레이아웃 기본 워크플로
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  doctree auth                                         # 최초 OAuth 2.0 로그인 및 token.json 발급
  doctree status                                       # 구글 드라이브 계정·스토리지 사용량 대시보드 확인
  doctree update [--strict]                            # 드라이브 조회 ➔ 분류·린팅·VT 감사 ➔ 스냅샷 원자적 저장
  doctree classify [--strict] [--snapshot PATH]        # 오프라인 16대 분야 분류 트리 및 [미분류·검토 대기] 출력
  doctree audit [--strict] [PATHS...]                  # 스냅샷 SHA-256 해시 평판 + 로컬 PDF 헤더/EOF/위험 태그 점검
  doctree layout get [PROFILE] [FIELD] [--format ...]  # 레이아웃 프로필(handcrafted/research/toolchain) 조회
  doctree layout set|add|remove <PROFILE> <FIELD> ...  # 레이아웃 프로필 필드 원자적 갱신·항목 추가/제거

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 [2] Step 1~4 로컬 학습 프로젝트 파이프라인 워크플로
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  [Step 1] 도서 동기화 및 다운로드 (sync / pull)
    doctree sync [--workspace-dir DIR]                 # 드라이브 스냅샷 ⟷ 로컬 */contents/*.pdf 3-Way 정합성 대조
    doctree pull <QUERY> <PROJECT>                     # 파일 ID·해시·제목 키워드로 로컬 <PROJECT>/contents/에 다운로드

  [Step 2] 프로젝트 표준 구조 강제 (scaffold)
    doctree scaffold handcrafted <PATH>                # contents/, content_data/, practice/, scripts/*.sh, .gitignore 생성
    doctree scaffold handcrafted <PATH> --check        # 파일 생성 없이 레이아웃 프로필 규격 충족 여부만 검증

  [Step 3] 페이지별 PDF 파싱 및 5대 동일성 기계 검증 (parse)
    doctree parse <TARGET> --verify --strict           # 페이지별 .md + 크롭 .png + DOT .svg 추출 및 원본 대조 검증

  [Step 4] 장·절별 실습(Kata) 및 테스트 프레임워크 생성 (practice)
    doctree practice <TARGET> [--lang py|cpp|java|sql] # content_data/ 기반 practice/<도서>/<장>/<절>/ 과제 생성

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🗂️ [3] 핵심 설정 파일 및 산출물 경로
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • config/category_tree.yaml            : 16개 대분야·55개 세부분야 고정 분류 트리 (단일 진실 공급원)
  • config/content_category_map.yaml     : Google Drive file_id ➔ 세부분야 category_id 명시적 배정표
  • config/project_layout_profiles.yaml  : 프로젝트 유형별(handcrafted/research/toolchain) 디렉토리·필수 파일 규격
  • snapshots/YYYY-MM-DD.json            : KST 기준 일별 드라이브 PDF 메타데이터 불변 스냅샷 (.gitignore 대상)

💡 특정 명령어의 상세 옵션과 예시는 `doctree help <command>` 또는 `doctree <command> --help`로 확인하세요.
   (예: doctree help update | doctree help classify | doctree help layout | doctree help parse)\
"""


def cmd_help(args: argparse.Namespace) -> int:
    """전체 워크플로 요약 또는 지정한 서브커맨드의 상세 도움말을 출력한다."""
    key = tuple(args.topic)
    target_parser = args.topic_parsers.get(key)
    if target_parser is None:
        available = ", ".join(k[0] for k in args.topic_parsers if len(k) == 1)
        return _render_boundary_error(
            ClassificationError(
                f"알 수 없는 명령어입니다: {' '.join(args.topic)} (사용 가능: {available})"
            ),
            default_boundary=BoundaryKind.TAXONOMY,
        )
    console.print(
        target_parser.format_help(),
        markup=False,
        highlight=False,
        soft_wrap=True,
        end="",
    )
    return 0


def _register_core_subparsers(
    subparsers: argparse._SubParsersAction,
) -> dict[tuple[str, ...], argparse.ArgumentParser]:
    p_help = subparsers.add_parser(
        "help",
        help="전체 워크플로 요약 또는 특정 서브커맨드의 상세 도움말 출력",
        description="doctree 전체 워크플로 요약 또는 지정한 서브커맨드(하위 액션 포함)의 상세 도움말과 사용 예시를 출력합니다.",
        epilog=(
            "사용 예시:\n"
            "  doctree help                  # 전체 명령어 및 Step 1~4 워크플로 도움말 출력\n"
            "  doctree help update           # update 명령어의 상세 옵션 및 예시 확인\n"
            "  doctree help classify         # classify 오프라인 분류 트리 검토 옵션 확인\n"
            "  doctree help layout           # layout 서브커맨드 전체 안내 확인\n"
            "  doctree help layout get       # layout get 하위 액션의 상세 옵션 확인\n"
            "  doctree help parse            # parse 명령어의 5대 동일성 검증 옵션 확인"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_help.add_argument(
        "topic",
        nargs="*",
        help="상세 도움말을 확인할 서브커맨드 이름 (예: update, classify, layout get, parse)",
    )

    p_auth = subparsers.add_parser(
        "auth",
        help="Google OAuth 2.0 로그인 및 token.json 생성",
        description="Google Drive OAuth 2.0(drive.readonly) 인증을 수행해 token.json을 생성·갱신하고 계정 대시보드를 출력합니다.",
        epilog=(
            "사용 예시:\n"
            "  doctree auth                  # 브라우저 OAuth 2.0 승인 또는 기존 토큰 갱신\n"
            "  rm token.json && doctree auth # 테스트 모드 refresh token(7일) 만료 시 재인증"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_auth.set_defaults(func=cmd_auth)

    p_status = subparsers.add_parser(
        "status",
        help="구글 드라이브 계정·스토리지 용량 출력",
        description="현재 연동된 Google Drive 계정 정보와 전체 스토리지 한도·사용량(GB 및 비율 게이지)을 출력합니다.",
        epilog="사용 예시:\n  doctree status                # 계정 이메일 및 스토리지 사용량 대시보드 출력",
        formatter_class=DoctreeHelpFormatter,
    )
    p_status.set_defaults(func=cmd_status)

    p_classify = subparsers.add_parser(
        "classify",
        help="고정 분류표로 로컬 스냅샷 검토 (터미널 출력)",
        description=(
            "네트워크나 Google Drive API를 호출하지 않고 로컬 스냅샷(snapshots/YYYY-MM-DD.json)과\n"
            "config/{category_tree,content_category_map}.yaml만을 대조하여 16개 대분야·55개 세부분야 트리와\n"
            "[미분류·검토 대기] 목록을 터미널에 출력합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree classify                                     # 최신 스냅샷 기준 분류 트리 및 미분류 목록 출력\n"
            "  doctree classify --strict                            # 미분류 도서가 1권이라도 있으면 종료 코드 1 반환\n"
            "  doctree classify --snapshot snapshots/2026-10-27.json"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_classify.add_argument("--snapshot", type=Path, default=None, help="records 배열을 가진 JSON 스냅샷 (생략 시 최신 스냅샷)")
    p_classify.add_argument(
        "--category-tree",
        "--categories",
        dest="category_tree",
        type=Path,
        default=Path(CATEGORY_TREE_FILE),
        help="고정 분류 트리 YAML (config/category_tree.yaml)",
    )
    p_classify.add_argument(
        "--content-map",
        "--assignments",
        dest="content_map",
        type=Path,
        default=Path(CONTENT_CATEGORY_MAP_FILE),
        help="Drive 파일 ID별 분류 배정 매핑 YAML (config/content_category_map.yaml)",
    )
    p_classify.add_argument("--strict", action="store_true", help="미분류 콘텐츠가 있으면 실패 (종료 코드 1)")
    p_classify.set_defaults(func=cmd_classify)

    p_update = subparsers.add_parser(
        "update",
        help="드라이브 콘텐츠 목록을 조회해 snapshots/YYYY-MM-DD.json 갱신",
        description=(
            "Google Drive에서 전체 PDF 메타데이터(file_id, SHA-256, 폴더 경로, 용량)를 조회한 뒤,\n"
            "고정 분류 체계 검증 ➔ 중복·파일명 린팅 ➔ VirusTotal 해시 감사 ➔ 직전 스냅샷 대비 5종 Diff 계산을 거쳐\n"
            "오늘자 snapshots/YYYY-MM-DD.json에 원자적으로 저장합니다.\n"
            "검증 실패 또는 --strict 위반 시 기존 스냅샷은 수정하지 않고 그대로 보존합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree update                                       # 드라이브 조회 및 오늘자 스냅샷 갱신\n"
            "  doctree update --strict                              # 미분류 또는 VT 위협 감지 시 저장 중단 및 기존 스냅샷 보존\n"
            "  doctree update --snapshots-dir ./snapshots"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_update.add_argument("--snapshots-dir", type=Path, default=None, help="스냅샷 출력 디렉터리 (기본: settings.SNAPSHOTS_DIR)")
    p_update.add_argument(
        "--category-tree",
        "--categories",
        dest="category_tree",
        type=Path,
        default=None,
        help="고정 분류 트리 YAML (기본: settings.CATEGORY_TREE_FILE)",
    )
    p_update.add_argument(
        "--content-map",
        "--assignments",
        dest="content_map",
        type=Path,
        default=None,
        help="Drive 파일 ID별 분류 배정 매핑 YAML (기본: settings.CONTENT_CATEGORY_MAP_FILE)",
    )
    p_update.add_argument("--strict", action="store_true", help="미분류 콘텐츠가 있으면 실패하고 스냅샷을 저장하지 않음")
    p_update.set_defaults(func=cmd_update)

    p_audit = subparsers.add_parser(
        "audit",
        help="드라이브 스냅샷(SHA-256) 및 로컬 contents/*.pdf 보안·무결성 점검",
        description=(
            "원격 드라이브 스냅샷의 SHA-256 해시 평판(VirusTotal API v3 / 로컬 캐시)과\n"
            "로컬 워크스페이스(*/contents/*.pdf) 파일의 %PDF- 헤더, %%EOF 종료 마커,\n"
            "6대 위험 실행 태그(/JavaScript, /JS, /Launch, /EmbeddedFiles, /RichMedia, /XFA)를 전수 점검합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree audit                                        # 최신 스냅샷 + 워크스페이스 전체 contents/*.pdf 점검\n"
            "  doctree audit --strict                               # 위협 또는 손상 PDF 발견 시 종료 코드 1 반환\n"
            "  doctree audit ../databases/contents/book.pdf --strict"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_audit.add_argument("paths", nargs="*", type=Path, help="점검할 로컬 PDF 파일 또는 디렉터리 (생략 시 워크스페이스 전체 contents/)")
    p_audit.add_argument("--snapshot", type=Path, default=None, help="점검할 스냅샷 JSON 경로 (생략 시 최신 스냅샷)")
    p_audit.add_argument("--snapshots-dir", type=Path, default=None, help="스냅샷 디렉터리")
    p_audit.add_argument("--vt-api-key", type=str, default=None, help="VirusTotal API 키 (기본: 환경변수 VIRUSTOTAL_API_KEY)")
    p_audit.add_argument("--vt-cache", type=Path, default=None, help="VirusTotal SHA-256 결과 캐시 파일 경로")
    p_audit.add_argument("--strict", action="store_true", help="위협/손상 파일 발견 시 종료 코드 1 반환")
    p_audit.set_defaults(func=cmd_audit)

    return {
        ("help",): p_help,
        ("auth",): p_auth,
        ("status",): p_status,
        ("classify",): p_classify,
        ("update",): p_update,
        ("audit",): p_audit,
    }


def _register_layout_subparsers(
    subparsers: argparse._SubParsersAction,
) -> dict[tuple[str, ...], argparse.ArgumentParser]:
    p_layout = subparsers.add_parser(
        "layout",
        help="프로젝트 레이아웃 프로필(config/project_layout_profiles.yaml) 조회 및 원자적 수정",
        description=(
            "프로젝트 유형별(handcrafted, toolchain) 표준 디렉터리·필수 파일·gitignore 구조 규격이 정의된\n"
            "config/project_layout_profiles.yaml을 CLI로 조회(get)하거나 원자적으로 수정(set/add/remove)합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree layout get                                   # 전체 프로필 YAML 출력\n"
            "  doctree layout get handcrafted directories --format lines\n"
            "  doctree layout set handcrafted practice_dir practice\n"
            "  doctree layout add handcrafted directories benchmarks\n"
            "  doctree layout remove handcrafted directories benchmarks"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    layout_sub = p_layout.add_subparsers(dest="layout_action", metavar="<action>", required=True)

    p_layout_get = layout_sub.add_parser(
        "get",
        help="전체 레이아웃 또는 특정 프로필·필드 값을 조회",
        description="config/project_layout_profiles.yaml 전체 또는 특정 프로필·필드 값을 yaml/json/lines 포맷으로 출력합니다.",
        epilog=(
            "사용 예시:\n"
            "  doctree layout get\n"
            "  doctree layout get handcrafted\n"
            "  doctree layout get handcrafted directories --format lines"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_layout_get.add_argument("profile", nargs="?", default=None, help="조회할 프로필 이름 (예: handcrafted, toolchain)")
    p_layout_get.add_argument("field", nargs="?", default=None, help="조회할 필드 이름 (예: directories, required_files, content_dir)")
    p_layout_get.add_argument(
        "--format",
        choices=OUTPUT_FORMATS,
        default="yaml",
        help="출력 포맷 (yaml | json | lines, 기본: yaml)",
    )
    p_layout_get.add_argument("--file", type=Path, default=None, help="대상 레이아웃 YAML 경로 (기본: config/project_layout_profiles.yaml)")
    p_layout_get.set_defaults(func=cmd_layout)

    p_layout_set = layout_sub.add_parser(
        "set",
        help="특정 프로필의 스칼라 또는 목록 필드를 새 값으로 설정",
        description="지정한 레이아웃 프로필의 단일 문자열 필드 또는 목록 필드를 새 값으로 원자적 교체합니다.",
        epilog="사용 예시:\n  doctree layout set handcrafted content_dir contents",
        formatter_class=DoctreeHelpFormatter,
    )
    p_layout_set.add_argument("profile", help="대상 프로필 이름")
    p_layout_set.add_argument("field", help="대상 필드 이름 (content_dir, directories, required_files 등)")
    p_layout_set.add_argument("values", nargs="+", help="설정할 값(들)")
    p_layout_set.add_argument("--file", type=Path, default=None, help="대상 레이아웃 YAML 경로")
    p_layout_set.set_defaults(func=cmd_layout)

    p_layout_add = layout_sub.add_parser(
        "add",
        help="특정 프로필의 목록 필드(directories, required_files, gitignore_entries)에 항목 추가",
        description="지정한 레이아웃 프로필의 목록 필드에 중복 없이 새 항목을 추가하고 원자적으로 저장합니다.",
        epilog="사용 예시:\n  doctree layout add handcrafted directories benchmarks",
        formatter_class=DoctreeHelpFormatter,
    )
    p_layout_add.add_argument("profile", help="대상 프로필 이름")
    p_layout_add.add_argument("field", help="대상 목록 필드 이름")
    p_layout_add.add_argument("items", nargs="+", help="추가할 항목(들)")
    p_layout_add.add_argument("--file", type=Path, default=None, help="대상 레이아웃 YAML 경로")
    p_layout_add.set_defaults(func=cmd_layout)

    p_layout_remove = layout_sub.add_parser(
        "remove",
        help="특정 프로필의 목록 필드에서 항목 제거",
        description="지정한 레이아웃 프로필의 목록 필드에서 특정 항목을 제거하고 원자적으로 저장합니다.",
        epilog="사용 예시:\n  doctree layout remove handcrafted directories benchmarks",
        formatter_class=DoctreeHelpFormatter,
    )
    p_layout_remove.add_argument("profile", help="대상 프로필 이름")
    p_layout_remove.add_argument("field", help="대상 목록 필드 이름")
    p_layout_remove.add_argument("items", nargs="+", help="제거할 항목(들)")
    p_layout_remove.add_argument("--file", type=Path, default=None, help="대상 레이아웃 YAML 경로")
    p_layout_remove.set_defaults(func=cmd_layout)

    return {
        ("layout",): p_layout,
        ("layout", "get"): p_layout_get,
        ("layout", "set"): p_layout_set,
        ("layout", "add"): p_layout_add,
        ("layout", "remove"): p_layout_remove,
    }


def _register_pipeline_subparsers(
    subparsers: argparse._SubParsersAction,
) -> dict[tuple[str, ...], argparse.ArgumentParser]:
    p_sync = subparsers.add_parser(
        "sync",
        help="드라이브 스냅샷(SHA-256) ⟷ 로컬 프로젝트 contents/(SHA-256) 3-Way 동기화 비교",
        description=(
            "[Step 1] 드라이브 스냅샷의 SHA-256 해시와 로컬 워크스페이스(*/contents/*.pdf)의 SHA-256 해시를 대조하여\n"
            "[양쪽 일치 · 드라이브 전용 · 로컬 전용] 3-Way 동기화 현황을 출력합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree sync                                         # 최신 스냅샷과 워크스페이스 전체 contents/ 비교\n"
            "  doctree sync --workspace-dir ..                      # 상위 워크스페이스 기준 3-Way 비교"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_sync.add_argument("--snapshot", type=Path, default=None, help="비교할 드라이브 스냅샷 JSON 경로 (생략 시 최신 스냅샷)")
    p_sync.add_argument("--snapshots-dir", type=Path, default=None, help="스냅샷 디렉터리")
    p_sync.add_argument("--workspace-dir", type=Path, default=None, help="로컬 프로젝트들을 탐색할 워크스페이스 루트")
    p_sync.add_argument("--cache-file", type=Path, default=None, help="로컬 contents/ 해시 캐시 파일 경로 (기본: snapshots/.local_contents_cache.json)")
    p_sync.set_defaults(func=cmd_sync)

    p_pull = subparsers.add_parser(
        "pull",
        help="드라이브의 원하는 PDF를 특정 로컬 프로젝트의 contents/로 내려받기",
        description=(
            "[Step 1] 드라이브 스냅샷에서 파일 ID, SHA-256 해시 또는 제목 키워드로 도서를 찾아\n"
            "로컬 프로젝트의 contents/<도서 파일명>/ 디렉토리(하위 경로 미지정 시 자동 생성)로 내려받고 SHA-256 무결성을 검증합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree pull \"친절한 SQL 튜닝\" databases\n"
            "  doctree pull \"토비의 스프링\" handcrafted-java/spring/01_toby_spring\n"
            "  doctree pull 1ZyXwVuTsRqPoNmLkJiHgFeDcBa543210 databases --subdir tuning --output-name sql-tuning.pdf"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_pull.add_argument("query", help="내려받을 도서의 Drive 파일 ID, SHA-256 해시 또는 도서명 키워드")
    p_pull.add_argument("project", help="배치할 로컬 프로젝트 (contents/<도서 파일명>/ 자동 생성) 또는 <프로젝트>/<하위 경로>")
    p_pull.add_argument("--subdir", default=None, help="contents/ 아래 배치할 하위 디렉터리 경로 (생략 시 도서 파일명 디렉터리)")
    p_pull.add_argument("--output-name", default=None, help="저장할 파일명 (생략 시 드라이브 원본 파일명)")
    p_pull.add_argument("--snapshot", type=Path, default=None, help="참조할 스냅샷 JSON 경로 (생략 시 최신 스냅샷)")
    p_pull.add_argument("--snapshots-dir", type=Path, default=None, help="스냅샷 디렉터리")
    p_pull.add_argument("--workspace-dir", type=Path, default=None, help="워크스페이스 루트 디렉터리")
    p_pull.add_argument("--cache-file", type=Path, default=None, help="로컬 해시 캐시 파일 경로")
    p_pull.set_defaults(func=cmd_pull)

    p_scaffold = subparsers.add_parser(
        "scaffold",
        help="로컬 프로젝트 디렉토리·필수 파일·실행 스크립트를 레이아웃 프로필 규격으로 강제 생성",
        description=(
            "[Step 2] 지정한 레이아웃 프로필(handcrafted | research | toolchain)에 맞춰\n"
            "로컬 프로젝트의 표준 디렉토리, 필수 문서, 실행 스크립트(scripts/*.sh), .gitignore를 생성하거나 검증(--check)합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree scaffold handcrafted ../databases            # 표준 디렉토리 및 필수 파일 생성·보정\n"
            "  doctree scaffold handcrafted ../databases --check    # 파일 생성 없이 레이아웃 충족 여부만 검증\n"
            "  doctree scaffold toolchain ../my-tool --overwrite"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_scaffold.add_argument("profile", help="적용할 레이아웃 프로필 이름 (handcrafted | research | toolchain)")
    p_scaffold.add_argument("path", type=Path, help="생성·보정할 로컬 프로젝트 디렉토리 경로")
    p_scaffold.add_argument("--file", type=Path, default=None, help="레이아웃 프로필 YAML 경로 (기본: config/project_layout_profiles.yaml)")
    p_scaffold.add_argument("--overwrite", action="store_true", help="기존 필수 파일도 템플릿으로 덮어쓰기")
    p_scaffold.add_argument("--check", action="store_true", help="파일을 생성하지 않고 레이아웃 규격 충족 여부만 검증")
    p_scaffold.set_defaults(func=cmd_scaffold)

    p_parse = subparsers.add_parser(
        "parse",
        help="페이지별 PDF 파싱(.md + 크롭 .png + DOT .svg) 및 원본 PDF 5대 동일성 기계 검증",
        description=(
            "[Step 3] 로컬 PDF를 페이지별 마크다운(*-Npage.md), 크롭 이미지(.png), Graphviz DOT 다이어그램(.svg),\n"
            "및 index.json으로 파싱하고 원본 PDF와의 5대 동일성을 기계적으로 검증합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree parse ../databases --verify --strict         # 프로젝트 내 contents/*.pdf 파싱 및 엄격 검증\n"
            "  doctree parse ../databases/contents/book.pdf --verify"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_parse.add_argument("target", type=Path, help="파싱할 PDF 파일 또는 프로젝트 디렉토리 경로")
    p_parse.add_argument("--output-dir", type=Path, default=None, help="중간 결과물 출력 디렉토리 (기본: <프로젝트>/content_data/<도서명>)")
    p_parse.add_argument("--verify", action="store_true", help="원본 PDF ⟷ content_data/ 5대 동일성 기계 검증 수행")
    p_parse.add_argument("--strict", action="store_true", help="검증 경고(WARN) 페이지 발생 시 종료 코드 1 반환")
    p_parse.add_argument("--dot-bin", default="dot", help="Graphviz dot 실행 파일 경로 (기본: dot)")
    p_parse.set_defaults(func=cmd_parse)

    p_practice = subparsers.add_parser(
        "practice",
        help="중간 결과물(content_data/)을 장·절별로 묶어 실습·테스트 프레임워크(practice/) 생성 및 검증",
        description=(
            "[Step 4] 파싱된 content_data/ 중간 결과물을 장·절 단위로 조합하여\n"
            "practice/<도서명>/<장>/<절>/ 아래에 README.md, PITFALLS.md, main.<ext>, test.<ext> 4대 표준 실습(Kata) 패키지를 생성하거나 검증(--check)합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree practice ../databases                        # 프로젝트 내 전체 도서 실습 패키지 생성\n"
            "  doctree practice ../databases --check                # 4대 표준 실습 파일 존재 여부 검증\n"
            "  doctree practice ../databases/content_data/친절한-SQL-튜닝 --lang sql --overwrite"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_practice.add_argument("target", type=Path, help="content_data/<도서명> 폴더 또는 프로젝트 디렉토리 경로")
    p_practice.add_argument("--practice-dir", type=Path, default=None, help="실습 코드 루트 디렉토리 (기본: <프로젝트>/practice)")
    p_practice.add_argument("--lang", default=None, help="실습 코드 언어 강제 지정 (py | cpp | java | sql)")
    p_practice.add_argument("--overwrite", action="store_true", help="기존 실습 파일도 다시 생성하여 덮어쓰기")
    p_practice.add_argument("--check", action="store_true", help="파일을 생성하지 않고 4대 표준 실습 파일(README.md, PITFALLS.md, main.*, test.*) 충족 여부만 검증")
    p_practice.add_argument("--dot-bin", default="dot", help="Graphviz dot 실행 파일 경로 (기본: dot)")
    p_practice.set_defaults(func=cmd_practice)

    return {
        ("sync",): p_sync,
        ("pull",): p_pull,
        ("scaffold",): p_scaffold,
        ("parse",): p_parse,
        ("practice",): p_practice,
    }


def _register_snippet_subparser(
    subparsers: argparse._SubParsersAction,
) -> dict[tuple[str, ...], argparse.ArgumentParser]:
    p_snippet = subparsers.add_parser(
        "snippet",
        help="실습 코드(practice/) 내 [[snippet]] / @snippet 추출, SHA-256 인간 검증 및 블로그 내보내기",
        description=(
            "practice/ 하위 소스 코드(.py, .cpp, .cc, .c, .h, .hpp, .java, .sql)에서\n"
            "C++17 [[snippet]] / [[snippet(\"설명\")]] 및 @snippet / @snippet(\"설명\") 블록을 추출하고,\n"
            "SHA-256 해시 기반 인간 검증 레지스트리(tips/snippets_registry.json) 관리, 마크다운 대시보드(tips/snippets.md),\n"
            "및 기술 블로그 포스트(tips/posts/YYYY-MM-DD-<slug>.md) 내보내기를 수행합니다."
        ),
        epilog=(
            "사용 예시:\n"
            "  doctree snippet --path ../handcrafted-cpp --list\n"
            "  doctree snippet --path ../handcrafted-cpp --verify solve_kata --reviewer coralstay\n"
            "  doctree snippet --path ../handcrafted-cpp --export\n"
            "  doctree snippet --path ../handcrafted-cpp --export-blog"
        ),
        formatter_class=DoctreeHelpFormatter,
    )
    p_snippet.add_argument("query", nargs="?", default=None, help="검색할 심볼·설명·파일명·코드 키워드")
    p_snippet.add_argument("--path", type=Path, default=Path("."), help="대상 프로젝트 또는 practice/ 디렉토리 경로 (기본: 현재 폴더)")
    p_snippet.add_argument("--list", action="store_true", help="추출된 전체 스니펫 목록 출력")
    p_snippet.add_argument("--status", action="store_true", help="스니펫 인간 검증 상태(VERIFIED / PENDING_REVIEW / OUTDATED) 요약 출력")
    p_snippet.add_argument("--verify", default=None, metavar="SYMBOL", help="지정한 심볼 또는 설명의 스니펫을 VERIFIED 상태로 승인")
    p_snippet.add_argument("--reviewer", default="human", help="--verify 승인 시 기록할 검토자 이름 (기본: human)")
    p_snippet.add_argument("--export", nargs="?", const="", default=None, metavar="OUT_PATH", help="스니펫 마크다운 대시보드 내보내기 (기본: <프로젝트>/tips/snippets.md)")
    p_snippet.add_argument("--export-blog", nargs="?", const="", default=None, metavar="POSTS_DIR", help="기술 블로그 포스트 마크다운 내보내기 (기본: <프로젝트>/tips/posts/)")
    p_snippet.set_defaults(func=cmd_snippet)
    return {("snippet",): p_snippet}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doctree",
        description=_ROOT_DESCRIPTION,
        epilog=_ROOT_EPILOG,
        formatter_class=DoctreeHelpFormatter,
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="설치된 doctree 버전 출력",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    topic_parsers: dict[tuple[str, ...], argparse.ArgumentParser] = {(): parser}
    topic_parsers.update(_register_core_subparsers(subparsers))
    topic_parsers.update(_register_layout_subparsers(subparsers))
    topic_parsers.update(_register_pipeline_subparsers(subparsers))
    topic_parsers.update(_register_snippet_subparser(subparsers))

    topic_parsers[("help",)].set_defaults(func=cmd_help, topic_parsers=topic_parsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        console.print(
            "\n⚠️ 사용자에 의해 작업이 중단되었습니다 (Ctrl+C).",
            style="yellow",
            markup=False,
            soft_wrap=True,
        )
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
