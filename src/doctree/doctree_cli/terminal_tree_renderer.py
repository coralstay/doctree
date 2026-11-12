# ---
# title: terminal_tree_renderer
# description: 분류 트리와 드라이브 스토리지 현황을 터미널(Rich)에 그리는 렌더러
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""터미널 분류 트리 포맷터 및 구글 드라이브 스토리지 Rich 대시보드 렌더러."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

from doctree.content_taxonomy.category_classifier import ClassificationResult, ClassifiedContent
from doctree.pdf_threat_audit.pdf_byte_inspector import LocalPdfAudit
from doctree.runtime_env.workspace_settings import format_record_path, normalize_nfc

__all__ = [
    "console",
    "format_classification_summary",
    "format_local_audit_reasons",
    "print_account_dashboard",
]

console = Console()  # ---- 1


def format_local_audit_reasons(audit: LocalPdfAudit) -> list[str]:
    """로컬 PDF 점검 결과에서 비정상·주의 사유 문자열 목록을 생성한다."""
    reasons: list[str] = []
    if not audit.valid_pdf_header:
        reasons.append("PDF 헤더(%PDF-) 누락")
    if not audit.valid_pdf_eof:
        reasons.append("PDF 종료(%%EOF) 누락/손상")
    if audit.dangerous_tags:
        reasons.append("위험 태그: " + ", ".join(audit.dangerous_tags))
    vt = audit.vt_verdict
    if vt and (vt.status in ("malicious", "suspicious") or vt.malicious > 0 or vt.suspicious > 0):
        count = vt.malicious if vt.malicious > 0 else vt.suspicious
        reasons.append(f"VT={vt.status}({count})")
    return reasons


def _single_line(value: str) -> str:  # ---- 2
    cleaned = "".join(" " if not char.isprintable() else char for char in normalize_nfc(value))
    return " ".join(cleaned.split())


def _content_key(content: ClassifiedContent) -> tuple[str, str]:
    return (normalize_nfc(content.record["name"]).casefold(), content.record["id"])


def _tree_branch(is_last: bool) -> str:
    return "└── " if is_last else "├── "


def _tree_indent(is_last: bool) -> str:
    return "    " if is_last else "│   "


def _group_classified_contents(result: ClassificationResult) -> dict[str, list[ClassifiedContent]]:  # ---- 3
    """분류 카탈로그의 세부분야 ID별로 분류된 콘텐츠 목록을 그룹화한다."""
    grouped: dict[str, list[ClassifiedContent]] = {
        subcategory.id: []
        for category in result.catalog.categories
        for subcategory in category.subcategories
    }
    for content in result.contents:
        if content.category_id is not None:
            if content.category_id not in grouped:
                raise ValueError("분류표에 없는 세부분야 ID: " + content.category_id)
            grouped[content.category_id].append(content)
    return grouped


def format_classification_summary(result: ClassificationResult) -> str:  # ---- 4
    """고정 분류 트리 순서(빈 세부분야 포함)와 미분류 검토 목록을 터미널 출력용 텍스트로 만든다."""
    grouped = _group_classified_contents(result)

    unclassified = result.unclassified
    lines = [
        f"파일 {len(result.contents)}개 · 분류 {len(result.contents) - len(unclassified)}권 · 미분류 {len(unclassified)}권",
        "",
        "콘텐츠 목록",
    ]
    categories = result.catalog.categories
    for category_number, category in enumerate(categories, start=1):
        category_last = category_number == len(categories)
        lines.append(f"{_tree_branch(category_last)}{category_number}. {_single_line(category.name)}")
        category_prefix = _tree_indent(category_last)
        for subcategory_number, subcategory in enumerate(category.subcategories, start=1):
            subcategory_last = subcategory_number == len(category.subcategories)
            contents = sorted(grouped[subcategory.id], key=_content_key)
            lines.append(
                f"{category_prefix}{_tree_branch(subcategory_last)}"
                f"{_single_line(subcategory.name)} ({len(contents)})"
            )
            content_prefix = category_prefix + _tree_indent(subcategory_last)
            for content_number, content in enumerate(contents, start=1):
                content_last = content_number == len(contents)
                lines.append(
                    f"{content_prefix}{_tree_branch(content_last)}"
                    f"{_single_line(content.record['name'])}"
                )

    lines.extend(["", "[미분류·검토 대기]"])
    if unclassified:
        lines.extend(
            f"- {_single_line(content.record['name'])} "
            f"(id={_single_line(content.record['id'])}, path={_single_line(format_record_path(content.record))})"
            for content in sorted(unclassified, key=_content_key)
        )
    else:
        lines.append("미분류 콘텐츠가 없습니다.")
    return "\n".join(lines) + "\n"


def print_account_dashboard(account_info: dict | None):  # ---- 5
    """구글 계정 및 드라이브 스토리지 현황을 rich 대시보드로 출력합니다."""
    if not account_info:
        console.print("[yellow]⚠️ 구글 계정 정보 조회를 건너뜁니다 (인증 필요).[/yellow]")
        return

    table = Table(title="📊 [bold cyan]Google Drive 계정 & 스토리지 상태[/bold cyan]", show_header=True, header_style="bold magenta")
    table.add_column("항목", style="cyan", width=20)
    table.add_column("상태 / 정보", style="green")

    table.add_row("👤 사용자", account_info.get("name", "Unknown"))
    table.add_row("📧 계정 이메일", account_info.get("email", "Unknown"))

    limit_gb = account_info.get("limit_gb")
    usage_gb = account_info.get("usage_gb", 0)
    usage_percent = account_info.get("usage_percent", 0.0)

    if limit_gb:
        filled_blocks = min(20, max(0, int(usage_percent // 5)))
        gauge_bar = f"[{'█' * filled_blocks}{'░' * (20 - filled_blocks)}] {usage_percent}%"
        table.add_row("💾 전체 스토리지", f"{usage_gb} GB / {limit_gb} GB ({gauge_bar})")
    else:
        table.add_row("💾 전체 스토리지 사용량", f"{usage_gb} GB (무제한/기관)")

    table.add_row("📁 드라이브 순수 사용", f"{account_info.get('drive_usage_gb', 0)} GB")

    console.print(table)


# ======== 노트 ========
# 1 공용 Rich 콘솔
#   설명: 출력용 Console 인스턴스를 모듈 하나에 둬서 command_dispatcher와 테스트가 같은 객체를 바꿔 끼울 수 있게 한다.
#   관련: src/doctree/doctree_cli/command_dispatcher.py
# 2 한 줄 정화
#   설명: 비출력 제어문자를 공백으로 바꾸고 공백을 접는다. 파일명에 줄바꿈이 있어도 트리 출력이 깨지지 않는다.
#   개념: 터미널 출력 정화
# 3 세부분야별 묶기
#   설명: 분류 카탈로그에 없는 세부분야 ID가 나오면 ValueError로 막는다(분류기가 이미 걸렀어야 할 불변식).
# 4 분류 트리 출력
#   설명: 고정 분류 트리 순서 그대로, 빈 세부분야도 (0)으로 보여 주고 마지막에 [미분류·검토 대기] 목록을 붙인다. 이름 정렬은 casefold 기준이다.
#   관련: src/doctree/content_taxonomy/category_classifier.py
# 5 계정 대시보드
#   설명: account_info가 None(인증 안 됨)이면 경고만 내고 건너뛴다. 이메일 등 계정 정보는 화면에만 출력하고 저장하지 않는다.
