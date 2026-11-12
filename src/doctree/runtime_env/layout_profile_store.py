# ---
# title: layout_profile_store
# description: 프로젝트 레이아웃 프로필 YAML의 스키마 검증과 원자적 입출력 저장소
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""프로젝트 레이아웃 프로필(config/project_layout_profiles.yaml) 스키마 검증, 공통 YAML 검증기 및 원자적 입출력 저장소."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from doctree.runtime_env.boundary_errors import ClassificationError, SnapshotStorageError
from doctree.runtime_env.workspace_settings import PathLike, atomic_write_text, normalize_nfc

__all__ = [
    "ALL_PROFILE_FIELDS",
    "IDENTIFIER_RE",
    "LIST_FIELDS",
    "MANDATORY_PROFILE_DIRECTORIES",
    "OUTPUT_FORMATS",
    "REQUIRED_PROFILE_FIELDS",
    "SCALAR_FIELDS",
    "add_layout_items",
    "format_layout_value",
    "get_layout_value",
    "load_layout_profiles",
    "read_strict_yaml",
    "remove_layout_items",
    "save_layout_profiles",
    "set_layout_field",
    "validate_identifier",
    "validate_layout_profiles",
    "validate_list",
    "validate_mapping_keys",
    "validate_nonempty_str",
    "validate_schema_version_one",
    "validate_unique_item",
]


class _UniqueKeySafeLoader(yaml.SafeLoader):  # ---- 1
    """YAML 기본 안전 타입을 유지하면서 중복 키 덮어쓰기를 ClassificationError로 차단한다."""

    __slots__ = ()

    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in result:
                    raise ClassificationError(
                        f"YAML 중복 키 {key!r} (줄 {key_node.start_mark.line + 1})"
                    )
                result[key] = self.construct_object(value_node, deep=deep)
            except TypeError as exc:
                raise ClassificationError("YAML 매핑 키는 단일 값이어야 합니다.") from exc
        return result


IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")

SCALAR_FIELDS: Final[tuple[str, ...]] = ("content_dir", "content_data_dir", "practice_dir")
LIST_FIELDS: Final[tuple[str, ...]] = ("directories", "required_files", "gitignore_entries")
ALL_PROFILE_FIELDS: Final[frozenset[str]] = frozenset(SCALAR_FIELDS + LIST_FIELDS)
REQUIRED_PROFILE_FIELDS: Final[frozenset[str]] = frozenset(LIST_FIELDS)
OUTPUT_FORMATS: Final[tuple[str, ...]] = ("yaml", "json", "lines")

MANDATORY_PROFILE_DIRECTORIES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({  # ---- 2
    "handcrafted": ("content_data", "contents", "practice", "scripts", "tips", ".opensource"),
})


def read_strict_yaml(path: PathLike) -> Any:  # ---- 3
    """중복 키를 허용하지 않는 안전 YAML 로더(_UniqueKeySafeLoader)로 설정 파일을 읽는다."""
    target = Path(path)
    try:
        with target.open(encoding="utf-8") as stream:
            return yaml.load(stream, Loader=_UniqueKeySafeLoader)
    except ClassificationError as exc:
        raise ClassificationError(f"{target}: {exc}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ClassificationError(f"{target}: 레이아웃 YAML 설정을 읽을 수 없습니다: {exc}") from exc


def validate_mapping_keys(
    value: Any,
    required_keys: set[str] | frozenset[str],
    location: str,
    *,
    allowed_keys: set[str] | frozenset[str] | None = None,
    object_label: str = "매핑 객체",
) -> dict[str, Any]:
    """입력 값이 dict이며 필수 키와 허용 키 집합을 정확히 충족하는지 검증한다."""
    if not isinstance(value, dict):
        raise ClassificationError(f"{location}: {object_label}여야 합니다.")
    permitted = required_keys if allowed_keys is None else allowed_keys
    actual = set(value.keys())
    missing = required_keys - actual
    extra = actual - permitted
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"필수 필드 누락: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"허용하지 않는 필드: {', '.join(sorted(map(str, extra)))}")
        raise ClassificationError(f"{location}: {'; '.join(details)}")
    return value


def validate_schema_version_one(mapping: Mapping[str, Any], location: str) -> None:
    """YAML 문서의 schema_version 필드가 정확히 정수 1(bool 제외)인지 검증한다."""
    if type(mapping.get("schema_version")) is not int or mapping["schema_version"] != 1:
        raise ClassificationError(f"{location}.schema_version: 지원하는 버전은 정수 1입니다.")


def validate_nonempty_str(
    value: Any,
    location: str,
    *,
    normalize: bool = True,
) -> str:
    """비어 있지 않은 문자열인지 검증하고 옵션에 따라 NFC 정규화·앞뒤 공백 제거를 수행한다."""
    if not isinstance(value, str) or not value.strip():
        raise ClassificationError(f"{location}: 비어 있지 않은 문자열이어야 합니다.")
    return normalize_nfc(value.strip()) if normalize else value


def validate_identifier(
    value: Any,
    location: str,
    *,
    error_message: str,
    normalize: bool = False,
) -> str:
    """영문 소문자 시작 식별자 정규식(IDENTIFIER_RE)을 충족하는지 검증한다."""
    text = validate_nonempty_str(value, location, normalize=normalize)
    if not IDENTIFIER_RE.fullmatch(text):
        raise ClassificationError(f"{location}: {error_message}")
    return text


def validate_list(
    value: Any,
    location: str,
    *,
    nonempty: bool = False,
    list_label: str = "목록",
) -> list[Any]:
    """입력 값이 리스트(및 nonempty 지정 시 비어 있지 않음)인지 검증한다."""
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "비어 있지 않은 " if nonempty else ""
        raise ClassificationError(f"{location}: {qualifier}{list_label}이어야 합니다.")
    return value


def validate_unique_item(
    value: str,
    seen: set[str],
    location: str,
    *,
    duplicate_label: str = "중복 값",
) -> None:
    """NFC 정규화 기준으로 집합 내 중복 여부를 검사하고 seen에 등록한다."""
    normalized = normalize_nfc(value)
    if normalized in seen:
        raise ClassificationError(f"{location}: {duplicate_label} {value!r}")
    seen.add(normalized)


def _validate_safe_relative_path(value: Any, location: str) -> str:  # ---- 4
    """프로젝트 루트 내부의 안전한 상대 경로인지 검증한다 (절대 경로·상위 디렉터리 탈출 '..'·단독 '.'·빈 세그먼트·역슬래시·제어문자 차단)."""
    text = validate_nonempty_str(value, location)
    if (
        "\\" in text
        or not text.isprintable()
        or any(part != part.strip() or not part.strip(".") for part in text.split("/"))
    ):
        raise ClassificationError(
            f"{location}: 프로젝트 루트 내부의 안전한 상대 경로여야 합니다: {value!r}"
        )
    return text


def _validate_unique_str_list(
    raw_list: Any,
    location: str,
    *,
    require_safe_relative_path: bool = False,
) -> list[str]:
    items = validate_list(raw_list, location, nonempty=True, list_label="문자열 목록")
    seen: set[str] = set()
    clean_list: list[str] = []
    for idx, item in enumerate(items):
        item_loc = f"{location}[{idx}]"
        norm_item = (
            _validate_safe_relative_path(item, item_loc)
            if require_safe_relative_path
            else validate_nonempty_str(item, item_loc)
        )
        validate_unique_item(norm_item, seen, item_loc, duplicate_label="중복 항목")
        clean_list.append(norm_item)
    return clean_list


def _validate_single_profile(raw_profile: Any, profile_location: str) -> dict[str, Any]:
    validate_mapping_keys(
        raw_profile,
        REQUIRED_PROFILE_FIELDS,
        profile_location,
        allowed_keys=ALL_PROFILE_FIELDS,
        object_label="프로필 정의는 매핑 객체",
    )
    clean_profile: dict[str, Any] = {}
    for scalar_field in SCALAR_FIELDS:
        if scalar_field in raw_profile:
            clean_profile[scalar_field] = _validate_safe_relative_path(
                raw_profile[scalar_field], f"{profile_location}.{scalar_field}"
            )
    for list_field in LIST_FIELDS:
        clean_profile[list_field] = _validate_unique_str_list(
            raw_profile[list_field],
            f"{profile_location}.{list_field}",
            require_safe_relative_path=(list_field != "gitignore_entries"),
        )
    return clean_profile


def _validate_mandatory_directories(profile_name: str, profile: Mapping[str, Any], location: str) -> None:
    """MANDATORY_PROFILE_DIRECTORIES에 고정된 최상위 디렉토리가 프로필 directories에서 빠지지 않았는지 검증한다."""
    mandatory = MANDATORY_PROFILE_DIRECTORIES.get(profile_name, ())
    missing = [d for d in mandatory if d not in profile["directories"]]
    if missing:
        raise ClassificationError(
            f"{location}.directories: {profile_name} 프로필의 필수 디렉토리는 제거할 수 없습니다: {', '.join(missing)}"
        )


def validate_layout_profiles(data: Any, location: str = "project_layout_profiles.yaml") -> dict[str, Any]:  # ---- 5
    """레이아웃 프로필 YAML 문서를 검증하고 NFC 정규화된 사전을 반환한다."""
    root = validate_mapping_keys(
        data,
        {"schema_version", "profiles"},
        location,
        object_label="최상위 루트는 매핑 객체",
    )
    validate_schema_version_one(root, location)

    raw_profiles = root["profiles"]
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ClassificationError(f"{location}.profiles: 비어 있지 않은 프로필 매핑이어야 합니다.")

    normalized_profiles: dict[str, dict[str, Any]] = {}
    seen_profiles: set[str] = set()
    for raw_name, raw_profile in raw_profiles.items():
        profile_name = validate_nonempty_str(raw_name, f"{location}.profiles")
        profile_location = f"{location}.profiles.{profile_name}"
        validate_identifier(
            profile_name,
            profile_location,
            error_message="프로필 이름은 영문 소문자로 시작하고 소문자, 숫자, '.', '_', '-'만 사용해야 합니다.",
            normalize=True,
        )
        validate_unique_item(profile_name, seen_profiles, profile_location, duplicate_label="중복 프로필")
        normalized_profiles[profile_name] = _validate_single_profile(raw_profile, profile_location)
        _validate_mandatory_directories(profile_name, normalized_profiles[profile_name], profile_location)

    return {
        "schema_version": 1,
        "profiles": normalized_profiles,
    }


def load_layout_profiles(path: PathLike) -> dict[str, Any]:
    """project_layout_profiles.yaml 파일을 읽어 중복 키 및 스키마를 검증한 뒤 반환한다."""
    target = Path(path)
    raw = read_strict_yaml(target)
    return validate_layout_profiles(raw, str(target))


def save_layout_profiles(data: dict[str, Any], path: PathLike) -> Path:  # ---- 6
    """레이아웃 프로필 데이터를 검증한 뒤 임시 파일(.tmp) + replace로 원자적으로 저장한다."""
    target = Path(path)
    validated = validate_layout_profiles(data, str(target))
    rendered = (
        "# 프로젝트 유형별 디렉토리 및 파일 구조 규격 정의서\n"
        + yaml.safe_dump(validated, allow_unicode=True, sort_keys=False)
    )
    try:
        return atomic_write_text(target, rendered)
    except OSError as exc:
        raise SnapshotStorageError(f"{target}: 레이아웃 설정 저장 실패: {exc}") from exc


def get_layout_value(
    data: dict[str, Any],
    profile: str | None = None,
    field: str | None = None,
) -> Any:
    """전체 레이아웃 문서, 특정 프로필, 또는 특정 프로필의 필드 값을 조회한다."""
    validated = validate_layout_profiles(data)
    if profile is None:
        return validated
    norm_profile = validate_nonempty_str(profile, "profile")
    profiles = validated["profiles"]
    if norm_profile not in profiles:
        raise ClassificationError(f"등록되지 않은 레이아웃 프로필입니다: {norm_profile!r}")
    profile_obj = profiles[norm_profile]
    if field is None:
        return profile_obj
    norm_field = validate_nonempty_str(field, "field")
    if norm_field not in profile_obj:
        raise ClassificationError(
            f"프로필 {norm_profile!r}에 존재하지 않는 필드입니다: {norm_field!r}"
        )
    return profile_obj[norm_field]


def _format_as_lines(value: Any) -> str:
    """조회된 레이아웃 값을 줄바꿈 구분 텍스트(lines) 포맷으로 변환한다."""
    if isinstance(value, str):
        return f"{value}\n"
    if isinstance(value, list):
        return "".join(f"{item}\n" for item in value)
    keys = value["profiles"] if ("profiles" in value and "schema_version" in value) else value
    return "".join(f"{key}\n" for key in keys)


def format_layout_value(value: Any, fmt: str = "yaml") -> str:
    """조회된 레이아웃 값을 yaml, json, lines 포맷 문자열로 직렬화한다."""
    if fmt not in OUTPUT_FORMATS:
        raise ClassificationError(f"지원하지 않는 출력 포맷입니다: {fmt!r} (지원: {', '.join(OUTPUT_FORMATS)})")
    if fmt == "json":
        return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if fmt == "lines":
        return _format_as_lines(value)
    if isinstance(value, str):
        return f"{value}\n"
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def _load_and_prepare_mutation(  # ---- 7
    path: PathLike,
    profile: str,
    field: str,
    items: Iterable[str],
    *,
    list_only_action: str | None = None,
    empty_message: str,
) -> tuple[dict[str, Any], str, str, list[str]]:
    """레이아웃 수정(set/add/remove) 공통 전처리: 문서 로드, 프로필·필드 검증, 입력값 NFC 정규화."""
    data = load_layout_profiles(path)
    norm_profile = validate_nonempty_str(profile, "profile")
    if norm_profile not in data["profiles"]:
        raise ClassificationError(f"등록되지 않은 레이아웃 프로필입니다: {norm_profile!r}")
    norm_field = validate_nonempty_str(field, "field")
    if list_only_action is not None and norm_field not in LIST_FIELDS:
        raise ClassificationError(
            f"{norm_profile}.{norm_field}: {list_only_action}는 목록 필드({', '.join(LIST_FIELDS)})에만 사용할 수 있습니다."
        )
    normalized_items = [validate_nonempty_str(v, f"{norm_profile}.{norm_field}") for v in items]
    if not normalized_items:
        raise ClassificationError(f"{norm_profile}.{norm_field}: {empty_message}")
    return data, norm_profile, norm_field, normalized_items


def set_layout_field(
    path: PathLike,
    profile: str,
    field: str,
    values: Iterable[str],
) -> dict[str, Any]:
    """특정 프로필의 스칼라 또는 목록 필드를 새 값으로 교체하고 원자적으로 저장한다."""
    data, norm_profile, norm_field, value_list = _load_and_prepare_mutation(
        path,
        profile,
        field,
        values,
        empty_message="설정할 값을 1개 이상 지정해야 합니다.",
    )
    if norm_field in SCALAR_FIELDS:
        if len(value_list) != 1:
            raise ClassificationError(
                f"{norm_profile}.{norm_field}: 단일 문자열 필드는 값을 1개만 지정해야 합니다."
            )
        data["profiles"][norm_profile][norm_field] = value_list[0]
    elif norm_field in LIST_FIELDS:
        data["profiles"][norm_profile][norm_field] = value_list
    else:
        raise ClassificationError(f"허용하지 않는 레이아웃 필드입니다: {norm_field!r}")

    save_layout_profiles(data, path)
    return data["profiles"][norm_profile]


def add_layout_items(
    path: PathLike,
    profile: str,
    field: str,
    items: Iterable[str],
) -> list[str]:
    """특정 프로필의 목록 필드(directories, required_files, gitignore_entries)에 항목을 추가하고 원자적으로 저장한다."""
    data, norm_profile, norm_field, normalized_items = _load_and_prepare_mutation(
        path,
        profile,
        field,
        items,
        list_only_action="항목 추가(add)",
        empty_message="추가할 항목을 1개 이상 지정해야 합니다.",
    )
    current: list[str] = list(data["profiles"][norm_profile][norm_field])
    for item in normalized_items:
        if item not in current:
            current.append(item)
    data["profiles"][norm_profile][norm_field] = current
    save_layout_profiles(data, path)
    return current


def remove_layout_items(
    path: PathLike,
    profile: str,
    field: str,
    items: Iterable[str],
) -> list[str]:
    """특정 프로필의 목록 필드에서 항목을 제거하고 원자적으로 저장한다."""
    data, norm_profile, norm_field, normalized_items = _load_and_prepare_mutation(
        path,
        profile,
        field,
        items,
        list_only_action="항목 제거(remove)",
        empty_message="제거할 항목을 1개 이상 지정해야 합니다.",
    )
    current: list[str] = list(data["profiles"][norm_profile][norm_field])
    for item in normalized_items:
        if item not in current:
            raise ClassificationError(
                f"프로필 {norm_profile!r}.{norm_field}에 존재하지 않는 항목입니다: {item!r}"
            )
    remove_set = set(normalized_items)
    updated = [entry for entry in current if entry not in remove_set]
    data["profiles"][norm_profile][norm_field] = updated
    save_layout_profiles(data, path)
    return updated


# ======== 노트 ========
# 1 YAML 중복 키 차단
#   설명: PyYAML 기본 동작은 중복 키를 마지막 값으로 조용히 덮어쓴다. 설정 오타가 묻히지 않도록 construct_mapping에서 ClassificationError로 막는다.
#   개념: SafeLoader 서브클래싱
# 2 필수 최상위 디렉토리
#   설명: 프로필별로 CLI가 반드시 생성해야 하는 최상위 디렉토리이며 YAML 편집으로 제거할 수 없다. handcrafted는 기준 모델 handcrafted-cpp이고 도서는 contents/<도서명>/에 둔다. 이 목록 밖의 디렉토리는 사용자가 `doctree layout add`로 명시적으로 추가하기 전까지 생성·삭제하지 않는다.
#   관련: src/doctree/runtime_env/project_scaffolder.py
# 3 엄격 YAML 읽기 진입점
#   설명: 모든 설정 YAML(레이아웃, 분류 트리)이 이 함수를 지나 중복 키 검사를 받는다.
#   관련: src/doctree/content_taxonomy/category_classifier.py
# 4 경로 탈출 차단
#   설명: 절대 경로, '..', 단독 '.', 빈 세그먼트, 역슬래시, 제어문자를 거부해 프로필 값이 프로젝트 루트 밖을 가리키지 못하게 한다.
#   개념: path traversal
# 5 프로필 문서 검증
#   설명: 최상위 키, schema_version==1, 프로필 이름 식별자, 중복 프로필, 필수 디렉토리를 차례로 검증하고 NFC 정규화된 사전을 돌려준다. 저장 전에도 같은 검증을 다시 통과해야 한다.
# 6 원자적 저장
#   설명: 검증을 통과한 데이터만 atomic_write_text로 쓴다. 헤더 주석 한 줄을 붙여 safe_dump하므로 사용자가 YAML에 적은 주석은 저장 때 사라진다.
#   관련: src/doctree/runtime_env/workspace_settings.py
# 7 set/add/remove 공통 전처리
#   설명: 수정 명령 세 개가 같은 검증을 공유한다. 등록되지 않은 프로필, 목록 전용 액션에 스칼라 필드, 빈 입력을 여기서 모두 막는다.
