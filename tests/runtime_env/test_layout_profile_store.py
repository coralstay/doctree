# ---
# title: test_layout_profile_store
# description: 레이아웃 프로필 저장소와 doctree layout 명령의 테스트
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""프로젝트 레이아웃 프로필 저장소(layout_profile_store.py) 및 `doctree layout` CLI 입출력 단위·통합 테스트."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from rich.console import Console

from doctree.doctree_cli import command_dispatcher as cli
from doctree.runtime_env.boundary_errors import ClassificationError, SnapshotStorageError
from doctree.runtime_env.layout_profile_store import (
    LIST_FIELDS,
    MANDATORY_PROFILE_DIRECTORIES,
    SCALAR_FIELDS,
    _validate_safe_relative_path,
    add_layout_items,
    format_layout_value,
    get_layout_value,
    load_layout_profiles,
    remove_layout_items,
    save_layout_profiles,
    set_layout_field,
    validate_layout_profiles,
)
from doctree.runtime_env.workspace_settings import (
    BASE_DIR,
    PROJECT_LAYOUT_PROFILES_FILE,
    normalize_nfc,
)


@pytest.fixture
def sample_layout_file(tmp_path: Path) -> Path:
    src = BASE_DIR / "config" / "project_layout_profiles.yaml"
    dst = tmp_path / "project_layout_profiles.yaml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def test_load_real_project_layout_profiles_and_query_formats():
    """실제 config/project_layout_profiles.yaml 로드 및 yaml/json/lines 3종 포맷 직렬화를 검증한다."""
    data = load_layout_profiles(PROJECT_LAYOUT_PROFILES_FILE)
    assert data["schema_version"] == 1
    assert set(data["profiles"].keys()) == {"handcrafted", "toolchain"}

    # 1) 전체 문서 조회
    full_val = get_layout_value(data)
    assert "profiles:" in format_layout_value(full_val, fmt="yaml")
    assert json.loads(format_layout_value(full_val, fmt="json"))["schema_version"] == 1
    assert format_layout_value(full_val, fmt="lines").splitlines() == [
        "handcrafted",
        "toolchain",
    ]

    # 2) 단일 프로필 조회
    hc_val = get_layout_value(data, profile="handcrafted")
    assert hc_val["content_dir"] == "contents"
    assert "content_dir" in format_layout_value(hc_val, fmt="lines").splitlines()

    # 3) 스칼라 및 목록 필드 조회
    c_dir = get_layout_value(data, profile="handcrafted", field="content_dir")
    assert format_layout_value(c_dir, fmt="yaml") == "contents\n"
    assert format_layout_value(c_dir, fmt="lines") == "contents\n"

    dirs = get_layout_value(data, profile="handcrafted", field="directories")
    assert format_layout_value(dirs, fmt="lines").splitlines() == [
        "content_data",
        "contents",
        "practice",
        "scripts",
        "tips",
        ".opensource",
    ]


def test_handcrafted_mandatory_directories_cannot_be_removed(sample_layout_file: Path):
    """handcrafted 필수 6개 디렉토리는 CLI로 제거할 수 없고, 사용자 지시에 의한 추가 디렉토리는 허용된다."""
    before = sample_layout_file.read_text(encoding="utf-8")
    for mandatory in ("content_data", "contents", "practice", "scripts", "tips", ".opensource"):
        with pytest.raises(ClassificationError, match="필수 디렉토리는 제거할 수 없습니다"):
            remove_layout_items(sample_layout_file, "handcrafted", "directories", [mandatory])
    assert sample_layout_file.read_text(encoding="utf-8") == before

    with pytest.raises(ClassificationError, match="필수 디렉토리는 제거할 수 없습니다"):
        set_layout_field(sample_layout_file, "handcrafted", "directories", ["contents", "practice"])

    dirs = add_layout_items(sample_layout_file, "handcrafted", "directories", ["benchmarks"])
    assert dirs[-1] == "benchmarks"
    remove_layout_items(sample_layout_file, "handcrafted", "directories", ["benchmarks"])


def test_set_add_remove_layout_operations_and_nfd_normalization(sample_layout_file: Path):
    """set_layout_field, add_layout_items, remove_layout_items가 NFC 정규화 및 원자적 저장을 수행한다."""
    # 1) 스칼라 필드 수정
    updated_prof = set_layout_field(sample_layout_file, "handcrafted", "content_dir", ["books_pdf"])
    assert updated_prof["content_dir"] == "books_pdf"

    # 2) 목록 필드 전체 교체
    updated_prof = set_layout_field(
        sample_layout_file,
        "toolchain",
        "directories",
        ["contents", "content_data", "papers"],
    )
    assert updated_prof["directories"] == ["contents", "content_data", "papers"]

    # 3) 목록 필드에 항목 추가 (NFD 입력 시 NFC 정규화 및 중복 추가 방지 확인)
    nfd_item = unicodedata.normalize("NFD", "실습노트")
    added = add_layout_items(
        sample_layout_file,
        "toolchain",
        "directories",
        [nfd_item, "papers"],  # papers는 이미 있으므로 중복 추가되지 않음
    )
    assert added == ["contents", "content_data", "papers", "실습노트"]
    assert unicodedata.is_normalized("NFC", added[-1])

    # 4) 목록 필드에서 항목 제거
    removed = remove_layout_items(sample_layout_file, "toolchain", "directories", ["실습노트"])
    assert removed == ["contents", "content_data", "papers"]


def test_cli_layout_subcommands_get_set_add_remove_and_error_handling(sample_layout_file: Path):
    """CLI `doctree layout get|set|add|remove` 명령이 정상 출력(종료 코드 0) 및 경계 오류(종료 코드 1)를 반환한다."""
    buf = io.StringIO()
    test_console = Console(file=buf, width=120, force_terminal=False, color_system=None)

    with patch.object(cli, "console", test_console):
        # 1) get lines
        assert (
            cli.main(
                [
                    "layout",
                    "get",
                    "handcrafted",
                    "directories",
                    "--format",
                    "lines",
                    "--file",
                    str(sample_layout_file),
                ]
            )
            == 0
        )
        assert "practice" in buf.getvalue().splitlines()

        # 2) set
        buf.truncate(0)
        buf.seek(0)
        assert (
            cli.main(
                [
                    "layout",
                    "set",
                    "handcrafted",
                    "practice_dir",
                    "kata_practice",
                    "--file",
                    str(sample_layout_file),
                ]
            )
            == 0
        )
        assert "kata_practice" in buf.getvalue()

        # 3) add
        buf.truncate(0)
        buf.seek(0)
        assert (
            cli.main(
                [
                    "layout",
                    "add",
                    "handcrafted",
                    "directories",
                    "benchmarks",
                    "--file",
                    str(sample_layout_file),
                ]
            )
            == 0
        )
        assert "레이아웃 항목 추가 완료" in buf.getvalue()

        # 4) remove
        buf.truncate(0)
        buf.seek(0)
        assert (
            cli.main(
                [
                    "layout",
                    "remove",
                    "handcrafted",
                    "directories",
                    "benchmarks",
                    "--file",
                    str(sample_layout_file),
                ]
            )
            == 0
        )
        assert "레이아웃 항목 제거 완료" in buf.getvalue()

        # 5) 존재하지 않는 프로필 조회 시 종료 코드 1 및 분류 검증 실패 메시지 출력
        buf.truncate(0)
        buf.seek(0)
        assert (
            cli.main(["layout", "get", "non_existent", "--file", str(sample_layout_file)])
            == 1
        )
        assert "❌ 분류 검증 실패:" in buf.getvalue()


def test_layout_validation_schema_errors(tmp_path: Path):
    """validate_layout_profiles 및 load_layout_profiles의 스키마·중복 키·상대 경로 오류 분기를 검증한다."""
    # 1) 최상위 루트가 dict가 아님
    with pytest.raises(ClassificationError, match="매핑 객체여야 합니다"):
        validate_layout_profiles(["not-a-dict"])

    # 2) 필수 루트 필드 누락 및 허용하지 않는 루트 필드 (단독 및 복합 분기)
    with pytest.raises(ClassificationError, match="필수 필드 누락: profiles"):
        validate_layout_profiles({"schema_version": 1})
    with pytest.raises(ClassificationError, match="허용하지 않는 필드: extra_root"):
        validate_layout_profiles({"schema_version": 1, "profiles": {}, "extra_root": 123})

    # 3) schema_version이 정수 1이 아님 (bool 포함)
    with pytest.raises(ClassificationError, match="지원하는 버전은 정수 1"):
        validate_layout_profiles({"schema_version": True, "profiles": {}})

    # 4) profiles가 비어 있거나 dict가 아님
    with pytest.raises(ClassificationError, match="비어 있지 않은 프로필 매핑"):
        validate_layout_profiles({"schema_version": 1, "profiles": {}})

    # 5) 잘못된 프로필 이름 정규식
    with pytest.raises(ClassificationError, match="프로필 이름은 영문 소문자로 시작"):
        validate_layout_profiles({"schema_version": 1, "profiles": {"Bad_Name": {}}})

    # 6) 프로필 정의가 dict가 아님
    with pytest.raises(ClassificationError, match="프로필 정의는 매핑 객체"):
        validate_layout_profiles({"schema_version": 1, "profiles": {"handcrafted": "invalid"}})

    # 7) 프로필 내 필수 필드 누락 및 미허용 필드 (단독 분기)
    with pytest.raises(ClassificationError, match="필수 필드 누락"):
        validate_layout_profiles(
            {"schema_version": 1, "profiles": {"handcrafted": {"directories": ["contents"]}}}
        )
    with pytest.raises(ClassificationError, match="허용하지 않는 필드: unknown_field"):
        validate_layout_profiles(
            {
                "schema_version": 1,
                "profiles": {
                    "handcrafted": {
                        "directories": ["contents"],
                        "required_files": ["README.md"],
                        "gitignore_entries": ["/contents/"],
                        "unknown_field": "x",
                    }
                },
            }
        )

    # 8) 빈 목록 또는 비문자열 항목 또는 중복 목록 항목
    base_profile = {
        "directories": ["contents"],
        "required_files": ["README.md"],
        "gitignore_entries": ["/contents/"],
    }
    with pytest.raises(ClassificationError, match="비어 있지 않은 문자열 목록"):
        validate_layout_profiles(
            {"schema_version": 1, "profiles": {"handcrafted": {**base_profile, "directories": []}}}
        )
    with pytest.raises(ClassificationError, match="비어 있지 않은 문자열이어야 합니다"):
        validate_layout_profiles(
            {"schema_version": 1, "profiles": {"handcrafted": {**base_profile, "content_dir": "   "}}}
        )
    with pytest.raises(ClassificationError, match="중복 항목"):
        validate_layout_profiles(
            {
                "schema_version": 1,
                "profiles": {"handcrafted": {**base_profile, "directories": ["contents", "contents"]}},
            }
        )
    for unsafe_path in ("/etc/passwd", "../escape", "."):
        with pytest.raises(ClassificationError, match="안전한 상대 경로"):
            validate_layout_profiles(
                {"schema_version": 1, "profiles": {"handcrafted": {**base_profile, "content_dir": unsafe_path}}}
            )
    with pytest.raises(ClassificationError, match="중복 프로필"):
        validate_layout_profiles(
            {"schema_version": 1, "profiles": {"research": base_profile, " research ": base_profile}}
        )

    # 9) load_layout_profiles: 중복 YAML 키 및 파일 없음 오류
    dup_yaml = tmp_path / "dup.yaml"
    dup_yaml.write_text("schema_version: 1\nschema_version: 1\nprofiles: {}\n", encoding="utf-8")
    with pytest.raises(ClassificationError, match="YAML 중복 키"):
        load_layout_profiles(dup_yaml)

    with pytest.raises(ClassificationError, match="레이아웃 YAML 설정을 읽을 수 없습니다"):
        load_layout_profiles(tmp_path / "missing.yaml")


def test_layout_mutation_and_storage_boundary_errors(sample_layout_file: Path):
    """get/format/set/add/remove/save_layout_profiles의 입력 오류 및 원자적 저장 장애 복구 분기를 검증한다."""
    # 10) get_layout_value / format_layout_value 오류 분기
    valid_data = load_layout_profiles(sample_layout_file)
    with pytest.raises(ClassificationError, match="존재하지 않는 필드"):
        get_layout_value(valid_data, profile="handcrafted", field="no_such_field")
    with pytest.raises(ClassificationError, match="지원하지 않는 출력 포맷"):
        format_layout_value(valid_data, fmt="xml")

    # 11) set_layout_field / add_layout_items / remove_layout_items 오류 분기
    with pytest.raises(ClassificationError, match="등록되지 않은 레이아웃 프로필"):
        set_layout_field(sample_layout_file, "unknown", "content_dir", ["contents"])
    with pytest.raises(ClassificationError, match="설정할 값을 1개 이상"):
        set_layout_field(sample_layout_file, "handcrafted", "content_dir", [])
    with pytest.raises(ClassificationError, match="단일 문자열 필드는 값을 1개만"):
        set_layout_field(sample_layout_file, "handcrafted", "content_dir", ["a", "b"])
    with pytest.raises(ClassificationError, match="허용하지 않는 레이아웃 필드"):
        set_layout_field(sample_layout_file, "handcrafted", "invalid_field", ["a"])

    with pytest.raises(ClassificationError, match="등록되지 않은 레이아웃 프로필"):
        add_layout_items(sample_layout_file, "unknown", "directories", ["x"])
    with pytest.raises(ClassificationError, match=r"목록 필드.*에만 사용할 수 있습니다"):
        add_layout_items(sample_layout_file, "handcrafted", "content_dir", ["x"])
    with pytest.raises(ClassificationError, match="추가할 항목을 1개 이상"):
        add_layout_items(sample_layout_file, "handcrafted", "directories", [])

    with pytest.raises(ClassificationError, match="등록되지 않은 레이아웃 프로필"):
        remove_layout_items(sample_layout_file, "unknown", "directories", ["x"])
    with pytest.raises(ClassificationError, match=r"목록 필드.*에만 사용할 수 있습니다"):
        remove_layout_items(sample_layout_file, "handcrafted", "content_dir", ["x"])
    with pytest.raises(ClassificationError, match="제거할 항목을 1개 이상"):
        remove_layout_items(sample_layout_file, "handcrafted", "directories", [])
    with pytest.raises(ClassificationError, match="존재하지 않는 항목입니다"):
        remove_layout_items(sample_layout_file, "handcrafted", "directories", ["not-there"])

    # 12) save_layout_profiles: Path.replace 실패 시 임시 파일 정리 및 SnapshotStorageError 발생
    with (
        patch.object(Path, "replace", side_effect=OSError("disk full")),
        pytest.raises(SnapshotStorageError, match="레이아웃 설정 저장 실패"),
    ):
        save_layout_profiles(valid_data, sample_layout_file)
    assert not (sample_layout_file.parent / f".{sample_layout_file.name}.tmp").exists()

    # write_bytes 단계에서 실패해 임시 파일이 생성되지 않은 경우 분기
    with (
        patch.object(Path, "write_bytes", side_effect=OSError("read-only fs")),
        pytest.raises(SnapshotStorageError, match="레이아웃 설정 저장 실패"),
    ):
        save_layout_profiles(valid_data, sample_layout_file)

    # KeyboardInterrupt(BaseException) 발생 시에도 임시 파일이 정리되고 원본 파일이 보존되는지 검증
    before_bytes = sample_layout_file.read_bytes()
    with (
        patch.object(Path, "replace", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        save_layout_profiles(valid_data, sample_layout_file)
    assert not (sample_layout_file.parent / f".{sample_layout_file.name}.tmp").exists()
    assert sample_layout_file.read_bytes() == before_bytes


@settings(max_examples=100, deadline=None)
@given(
    raw_path=st.one_of(
        st.text(max_size=60),
        st.sampled_from([".", "..", "../escape", "a/../b", "/abs/path", "\\backslash", "a\\b", "a/\x00b", "a/\nb"]),
    )
)
@example(raw_path="scripts/\u1100\u1161_run.sh")
@example(raw_path=".gitignore")
@example(raw_path="../etc/passwd")
@example(raw_path="./.")
@example(raw_path="a//b")
@example(raw_path="a/")
@example(raw_path="a/ /b")
@example(raw_path="a/.../b")
def test_property_fuzz_validate_safe_relative_path_confinement_and_idempotence(raw_path: str) -> None:
    """임의 경로 문자열에 대해 _validate_safe_relative_path가 모든 탈출 경로를 차단하고 타깃 루트 내부 상대 경로 및 멱등성을 보장한다."""
    root = Path("/tmp/doctree_fuzz_layout_root").resolve()
    try:
        safe_rel = _validate_safe_relative_path(raw_path, "fuzz.path")
    except ClassificationError:
        return

    assert _validate_safe_relative_path(safe_rel, "fuzz.path") == safe_rel
    assert _validate_safe_relative_path(unicodedata.normalize("NFD", safe_rel), "fuzz.path") == safe_rel
    assert "\\" not in safe_rel
    assert all(part == part.strip() and bool(part.strip(".")) for part in safe_rel.split("/"))
    resolved = (root / safe_rel).resolve()
    assert resolved.is_relative_to(root)
    assert resolved != root


_SAFE_REL_CANDIDATES = st.sampled_from(
    [
        "contents",
        "content_data",
        "practice",
        "benchmarks",
        "docs/notes",
        unicodedata.normalize("NFD", "실습노트"),
        unicodedata.normalize("NFD", "참고자료/요약"),
    ]
)


class LayoutProfileStoreStateMachine(RuleBasedStateMachine):
    """[Pass 7/8] 레이아웃 프로필 저장소의 상태 전이(set/add/remove/get/format/장애 복구) 불변식 검증 상태 머신."""

    def __init__(self) -> None:
        super().__init__()
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.layout_file = self.tmp_dir / "project_layout_profiles.yaml"
        src = BASE_DIR / "config" / "project_layout_profiles.yaml"
        self.layout_file.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        self.shadow: dict[str, Any] = load_layout_profiles(self.layout_file)

    def teardown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @rule(
        profile=st.sampled_from(["handcrafted", "toolchain"]),
        field=st.sampled_from(SCALAR_FIELDS),
        val=_SAFE_REL_CANDIDATES,
    )
    def rule_set_scalar_field(self, profile: str, field: str, val: str) -> None:
        updated = set_layout_field(self.layout_file, profile, field, [val])
        norm_val = normalize_nfc(val.strip())
        self.shadow["profiles"][profile][field] = norm_val
        assert updated[field] == norm_val
        assert get_layout_value(self.shadow, profile=profile, field=field) == norm_val

    @rule(
        profile=st.sampled_from(["handcrafted", "toolchain"]),
        field=st.sampled_from(LIST_FIELDS),
        item=_SAFE_REL_CANDIDATES,
    )
    def rule_add_list_item(self, profile: str, field: str, item: str) -> None:
        updated = add_layout_items(self.layout_file, profile, field, [item])
        norm_item = normalize_nfc(item.strip())
        shadow_list: list[str] = self.shadow["profiles"][profile][field]
        if norm_item not in shadow_list:
            shadow_list.append(norm_item)
        assert updated == shadow_list

    @rule(
        profile=st.sampled_from(["handcrafted", "toolchain"]),
        field=st.sampled_from(LIST_FIELDS),
    )
    def rule_remove_or_reject_list_item(self, profile: str, field: str) -> None:
        shadow_list: list[str] = self.shadow["profiles"][profile][field]
        mandatory = MANDATORY_PROFILE_DIRECTORIES.get(profile, ()) if field == "directories" else ()
        if shadow_list[-1] in mandatory:
            with pytest.raises(ClassificationError, match="필수 디렉토리는 제거할 수 없습니다"):
                remove_layout_items(self.layout_file, profile, field, [shadow_list[-1]])
        elif len(shadow_list) > 1:
            victim = shadow_list.pop()
            updated = remove_layout_items(self.layout_file, profile, field, [victim])
            assert updated == shadow_list
        else:
            with pytest.raises(ClassificationError, match="비어 있지 않은 문자열 목록이어야 합니다"):
                remove_layout_items(self.layout_file, profile, field, [shadow_list[0]])
            with pytest.raises(ClassificationError, match="존재하지 않는 항목입니다"):
                remove_layout_items(self.layout_file, profile, field, ["non_existent_entry_xyz"])

    @invariant()
    def invariant_disk_matches_shadow_and_no_orphan_tmp(self) -> None:
        assert not list(self.tmp_dir.glob(".*.tmp"))
        disk_data = load_layout_profiles(self.layout_file)
        assert disk_data == self.shadow
        assert json.loads(format_layout_value(disk_data, fmt="json")) == self.shadow


def test_stateful_layout_profile_store_machine() -> None:
    """상태 머신으로 레이아웃 프로필의 다단계 set/add/remove 상태 전이와 디스크 원자적 일관성을 검증한다."""
    run_state_machine_as_test(
        LayoutProfileStoreStateMachine,
        settings=settings(max_examples=25, stateful_step_count=12, deadline=None),
    )




