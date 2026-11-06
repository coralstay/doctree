# ---
# title: category_classifier
# description: 고정 분류 트리를 검증하고 드라이브 파일 ID 배정표로 파일을 분류한다
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""Validate the fixed taxonomy and classify Drive files by their persistent IDs.

Names and folder paths are metadata only. A file receives a category exclusively
through an explicit entry in ``content_category_map.yaml``.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from doctree.runtime_env.boundary_errors import ClassificationError
from doctree.runtime_env.layout_profile_store import (
    read_strict_yaml,
    validate_identifier,
    validate_list,
    validate_mapping_keys,
    validate_nonempty_str,
    validate_schema_version_one,
    validate_unique_item,
)
from doctree.runtime_env.workspace_settings import PathLike, normalize_nfc

__all__ = [
    "Category",
    "ClassificationCatalog",
    "ClassificationError",
    "ClassificationResult",
    "ClassifiedContent",
    "ContentAssignment",
    "Subcategory",
    "classify_records",
    "load_catalog",
]

_NODE_ID_ERROR = "분류 ID는 영문 소문자로 시작하고 영문 소문자, 숫자, '.', '_', '-'만 사용해야 합니다."


@dataclass(frozen=True, slots=True)
class Subcategory:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Category:
    id: str
    name: str
    subcategories: tuple[Subcategory, ...]


@dataclass(frozen=True, slots=True)
class ContentAssignment:
    file_id: str
    title: str
    category_id: str


@dataclass(frozen=True, slots=True)
class ClassificationCatalog:
    categories: tuple[Category, ...]
    assignments: tuple[ContentAssignment, ...]


@dataclass(frozen=True, slots=True)
class ClassifiedContent:
    record: dict[str, Any]
    category_id: str | None


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    catalog: ClassificationCatalog
    contents: tuple[ClassifiedContent, ...]

    @property
    def unclassified(self) -> tuple[ClassifiedContent, ...]:
        return tuple(content for content in self.contents if content.category_id is None)


def _load_schema_collection(path: PathLike, collection: str) -> list[Any]:
    location = str(path)
    doc = validate_mapping_keys(
        read_strict_yaml(path),
        {"schema_version", collection},
        location,
        object_label="객체",
    )
    validate_schema_version_one(doc, location)
    return validate_list(
        doc[collection],
        f"{location}.{collection}",
        nonempty=(collection == "categories"),
    )


def _parse_subcategories(
    raw_subcategories: list[Any],
    parent_location: str,
    all_node_ids: set[str],
    leaf_ids: set[str],
) -> tuple[Subcategory, ...]:
    """대분야 하위의 세부분야 목록을 검증하고 Subcategory 튜플을 반환한다."""
    subcategories: list[Subcategory] = []
    sibling_names: set[str] = set()
    for child_index, raw_subcategory in enumerate(raw_subcategories):
        child_location = f"{parent_location}.subcategories[{child_index}]"
        child = validate_mapping_keys(raw_subcategory, {"id", "name"}, child_location, object_label="객체")
        child_id = validate_identifier(child["id"], f"{child_location}.id", error_message=_NODE_ID_ERROR)
        child_name = validate_nonempty_str(child["name"], f"{child_location}.name", normalize=False)
        validate_unique_item(child_id, all_node_ids, f"{child_location}.id")
        validate_unique_item(child_name, sibling_names, f"{child_location}.name")
        leaf_ids.add(child_id)
        subcategories.append(Subcategory(id=child_id, name=child_name))
    return tuple(subcategories)


def _parse_category_tree(category_tree_path: PathLike) -> tuple[tuple[Category, ...], set[str]]:
    raw_categories = _load_schema_collection(category_tree_path, "categories")
    categories: list[Category] = []
    all_node_ids: set[str] = set()
    root_names: set[str] = set()
    leaf_ids: set[str] = set()

    for index, raw_category in enumerate(raw_categories):
        location = f"categories[{index}]"
        item = validate_mapping_keys(raw_category, {"id", "name", "subcategories"}, location, object_label="객체")
        category_id = validate_identifier(item["id"], f"{location}.id", error_message=_NODE_ID_ERROR)
        name = validate_nonempty_str(item["name"], f"{location}.name", normalize=False)
        validate_unique_item(category_id, all_node_ids, f"{location}.id")
        validate_unique_item(name, root_names, f"{location}.name")

        raw_subcategories = validate_list(item["subcategories"], f"{location}.subcategories", nonempty=True)
        subcategories = _parse_subcategories(raw_subcategories, location, all_node_ids, leaf_ids)
        categories.append(Category(id=category_id, name=name, subcategories=subcategories))

    return tuple(categories), leaf_ids


def _parse_content_assignments(
    content_category_map_path: PathLike,
    leaf_ids: set[str],
) -> tuple[ContentAssignment, ...]:
    raw_contents = _load_schema_collection(content_category_map_path, "contents")
    assignments: list[ContentAssignment] = []
    file_ids: set[str] = set()

    for index, raw_content in enumerate(raw_contents):
        location = f"contents[{index}]"
        item = validate_mapping_keys(raw_content, {"file_id", "title", "category_id"}, location, object_label="객체")
        file_id = validate_nonempty_str(item["file_id"], f"{location}.file_id", normalize=False)
        title = validate_nonempty_str(item["title"], f"{location}.title", normalize=False)
        category_id = validate_identifier(item["category_id"], f"{location}.category_id", error_message=_NODE_ID_ERROR)
        validate_unique_item(file_id, file_ids, f"{location}.file_id", duplicate_label="중복 파일 ID")
        if category_id not in leaf_ids:
            raise ClassificationError(
                f"{location}.category_id: 등록된 세부분야 ID가 아닙니다: {category_id!r}"
            )
        assignments.append(ContentAssignment(file_id=file_id, title=title, category_id=category_id))

    return tuple(assignments)


def load_catalog(category_tree_path: PathLike, content_category_map_path: PathLike) -> ClassificationCatalog:
    """Load both configuration files, rejecting invalid or ambiguous mappings."""
    categories, leaf_ids = _parse_category_tree(category_tree_path)
    assignments = _parse_content_assignments(content_category_map_path, leaf_ids)
    return ClassificationCatalog(categories=categories, assignments=assignments)


def classify_records(
    records: Iterable[dict], catalog: ClassificationCatalog, *, strict: bool = False
) -> ClassificationResult:
    """Assign known Drive IDs and leave every new ID pending explicit review."""
    by_file_id = {
        normalize_nfc(assignment.file_id): assignment.category_id
        for assignment in catalog.assignments
    }
    contents: list[ClassifiedContent] = []
    file_ids: set[str] = set()
    for index, record in enumerate(records):
        location = f"records[{index}]"
        if not isinstance(record, dict):
            raise ClassificationError(f"{location}: 객체여야 합니다.")
        file_id = validate_nonempty_str(record.get("id"), f"{location}.id", normalize=False)
        validate_nonempty_str(record.get("name"), f"{location}.name", normalize=False)
        validate_unique_item(file_id, file_ids, f"{location}.id", duplicate_label="중복 파일 ID")
        contents.append(
            ClassifiedContent(record=dict(record), category_id=by_file_id.get(normalize_nfc(file_id)))
        )

    result = ClassificationResult(catalog=catalog, contents=tuple(contents))
    if strict and result.unclassified:
        pending = ", ".join(repr(content.record["id"]) for content in result.unclassified)
        raise ClassificationError(f"미분류 콘텐츠 {len(result.unclassified)}권: {pending}")
    return result
