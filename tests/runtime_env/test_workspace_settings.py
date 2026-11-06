# ---
# title: test_workspace_settings
# description: 런타임 환경 설정과 프로젝트 대칭 구조·스냅샷 격리의 테스트
# authors: [coralstay]
# license: AGPL-3.0-only
# status: draft
# created_date: '2026-11-06'
# updated_date: '2026-11-06'
# ---
"""런타임 환경 설정(doctree.runtime_env.workspace_settings) 및 프로젝트 대칭 구조·스냅샷 격리 검증."""
import ast
import dataclasses
import importlib
import os
import subprocess
import unicodedata
import unittest
from pathlib import Path
from types import MappingProxyType

import hypothesis
from hypothesis import example, given
from hypothesis import strategies as st

import doctree.runtime_env.workspace_settings as settings
from doctree.content_taxonomy.practice_kata_builder import SUPPORTED_LANG_MAP
from doctree.runtime_env.workspace_settings import (
    BASE_DIR,
    DEFAULT_TOC_CHAPTER,
    WorkspaceSettings,
    build_default_toc_entry,
    format_record_path,
    normalize_nfc,
    sanitize_dot_label,
)

ROOT = Path(__file__).resolve().parents[2]
_CONTROL_FLOW_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try)


def _max_control_nesting(node: ast.AST, current: int = 0) -> int:
    """AST 노드의 최대 제어문(if/for/while/try) 중첩 깊이를 계산한다(elif 체인은 동일 깊이로 평탄화)."""
    best = current
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        is_elif = isinstance(node, ast.If) and isinstance(child, ast.If) and node.orelse == [child]
        next_depth = current if is_elif else current + int(isinstance(child, _CONTROL_FLOW_NODES))
        best = max(best, _max_control_nesting(child, next_depth))
    return best


def _iter_routine_nodes(root_dir: Path) -> list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """지정 디렉터리 하위 모든 파이썬 파일에서 함수·비동기 함수 AST 노드를 추출한다."""
    routines: list[tuple[Path, ast.FunctionDef | ast.AsyncFunctionDef]] = []
    for py_file in sorted(root_dir.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        routines.extend(
            (py_file, node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
    return routines


class TestSettings(unittest.TestCase):
    def test_base_dir_path(self):
        self.assertIsInstance(BASE_DIR, Path)
        self.assertTrue(BASE_DIR.exists())
        self.assertEqual(BASE_DIR, ROOT)

    def test_workspace_settings_is_frozen_slotted_dataclass(self):
        """WorkspaceSettings는 불변(frozen) + slots 기반 데이터클래스이며 from_env()로 격리 생성된다."""
        env_input = {
            "DOCTREE_SNAPSHOTS_DIR": "/tmp/custom-snaps",
            "GOOGLE_SERVICE_ACCOUNT_FILE": "/tmp/sa.json",
            "GOOGLE_DRIVE_FOLDER_ID": "folder-xyz",
            "DOCTREE_CATEGORY_TREE_FILE": "/tmp/cats.yaml",
            "DOCTREE_CONTENT_CATEGORY_MAP_FILE": "/tmp/assigns.yaml",
            "DOCTREE_PROJECT_LAYOUT_PROFILES_FILE": "/tmp/layout.yaml",
            "FREERIDE_WORKSPACE_DIR": "/tmp/ws",
            "VIRUSTOTAL_API_KEY": "vt-secret",
        }
        cfg = WorkspaceSettings.from_env(env=env_input, base_dir=ROOT)
        env_input["DOCTREE_SNAPSHOTS_DIR"] = "/tmp/mutated-after"
        self.assertEqual(cfg.base_dir, ROOT)
        self.assertEqual(cfg.snapshots_dir, "/tmp/custom-snaps")
        self.assertEqual(cfg.vt_cache_file, "/tmp/custom-snaps/.vt_cache.json")
        self.assertEqual(cfg.local_contents_cache_file, "/tmp/custom-snaps/.local_contents_cache.json")
        self.assertEqual(cfg.google_service_account_file, "/tmp/sa.json")
        self.assertEqual(cfg.google_drive_folder_id, "folder-xyz")
        self.assertEqual(cfg.category_tree_file, "/tmp/cats.yaml")
        self.assertEqual(cfg.content_category_map_file, "/tmp/assigns.yaml")
        self.assertEqual(cfg.project_layout_profiles_file, "/tmp/layout.yaml")
        self.assertEqual(cfg.workspace_dir, Path("/tmp/ws"))
        self.assertEqual(cfg.virustotal_api_key, "vt-secret")
        self.assertTrue(hasattr(WorkspaceSettings, "__slots__"))
        self.assertFalse(hasattr(cfg, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.snapshots_dir = "/tmp/mutated"  # type: ignore[misc]
        for bad_chunk in (0, -1, True):
            with self.assertRaises(ValueError):
                settings.compute_sha256(__file__, chunk_size=bad_chunk)  # type: ignore[arg-type]
        self.assertEqual(settings.normalize_nfc(None), "")
        self.assertEqual(settings.normalize_nfc(0), "0")
        self.assertEqual(settings.normalize_nfc(False), "False")
        self.assertEqual(settings.format_record_path({"folder_path": "   /  ", "name": "a.pdf"}), "a.pdf")
        self.assertEqual(settings.sanitize_dot_label("A|{B}<C>\u200b"), "A/(B)[C] ")

    def test_all_domain_classes_define_explicit_memory_slots(self):
        """src/doctree/ 내 모든 클래스(StrEnum·TypedDict 제외)는 __slots__ 또는 slots=True를 명시하여 불필요한 슬롯 낭비를 막는다."""
        pkg_root = ROOT / "src" / "doctree"
        for py_file in sorted(pkg_root.rglob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                bases = {ast.unparse(b) for b in node.bases}
                if bases & {"StrEnum", "TypedDict"}:
                    continue
                decorators = [ast.unparse(d) for d in node.decorator_list]
                has_dataclass_slots = any("slots=True" in dec for dec in decorators)
                has_explicit_slots = any(
                    isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "__slots__" for t in stmt.targets)
                    for stmt in node.body
                )
                self.assertTrue(
                    has_dataclass_slots or has_explicit_slots,
                    f"{py_file.name}:{node.lineno} class {node.name} must define __slots__ or @dataclass(..., slots=True)",
                )

    def test_all_routines_respect_code_complete_parameter_and_statement_bounds(self):
        """전 함수·메서드는 매개변수 7개 이하·제어문 중첩 3단 이하·도메인 루틴 길이 65줄 이하(Code Complete 2 Ch.7)를 준수한다."""
        pkg_root = ROOT / "src" / "doctree"
        for py_file, node in _iter_routine_nodes(pkg_root) + _iter_routine_nodes(ROOT / "tests"):
            param_count = (
                len(node.args.posonlyargs)
                + len(node.args.args)
                + len(node.args.kwonlyargs)
                + int(node.args.vararg is not None)
                + int(node.args.kwarg is not None)
            )
            self.assertLessEqual(
                param_count,
                7,
                f"{py_file.name}:{node.lineno} {node.name}() has {param_count} parameters (> 7)",
            )
            if not py_file.is_relative_to(pkg_root):
                continue
            nesting = _max_control_nesting(node)
            self.assertLessEqual(
                nesting,
                3,
                f"{py_file.name}:{node.lineno} {node.name}() has control nesting depth {nesting} (> 3)",
            )
            if not node.name.startswith("_register_"):
                line_span = (node.end_lineno or node.lineno) - node.lineno + 1
                self.assertLessEqual(
                    line_span,
                    65,
                    f"{py_file.name}:{node.lineno} {node.name}() spans {line_span} lines (> 65)",
                )

    def test_dotenv_import_error_fallback(self):
        """python-dotenv가 없어도 workspace_settings가 정상 로드된다."""
        import sys

        saved = sys.modules.get("dotenv")
        try:
            sys.modules["dotenv"] = None  # type: ignore[assignment]
            importlib.reload(settings)
            self.assertEqual(settings.BASE_DIR, ROOT)
        finally:
            if saved is not None:
                sys.modules["dotenv"] = saved
            else:
                sys.modules.pop("dotenv", None)
            importlib.reload(settings)

    def test_snapshots_dir_default_and_env(self):
        """SNAPSHOTS_DIR 기본값은 <repo>/snapshots이고 DOCTREE_SNAPSHOTS_DIR로 바꿀 수 있다."""
        old = os.environ.pop("DOCTREE_SNAPSHOTS_DIR", None)
        try:
            importlib.reload(settings)
            self.assertEqual(Path(settings.SNAPSHOTS_DIR), settings.BASE_DIR / "snapshots")
            os.environ["DOCTREE_SNAPSHOTS_DIR"] = "/tmp/x-snapshots"
            importlib.reload(settings)
            self.assertEqual(Path(settings.SNAPSHOTS_DIR), Path("/tmp/x-snapshots"))
        finally:
            os.environ.pop("DOCTREE_SNAPSHOTS_DIR", None)
            if old is not None:
                os.environ["DOCTREE_SNAPSHOTS_DIR"] = old
            importlib.reload(settings)


class TestSelfDocumentingNamingAndSymmetry(unittest.TestCase):
    def test_config_dir_has_only_yaml_files_and_pypa_src_layout(self):
        """루트 config/에는 순수 YAML만 있고, src/ 아래에는 doctree 패키지 하나만 존재한다(PyPA 표준 src-layout)."""
        # content_category_map.yaml은 개인 Drive 파일 ID가 담긴 로컬 전용(.gitignore) 파일이라 있을 수도 없을 수도 있다.
        config_files = sorted(
            p.name for p in (ROOT / "config").iterdir()
            if not p.name.startswith(".") and p.name != "content_category_map.yaml"
        )
        self.assertEqual(
            config_files,
            ["category_tree.yaml", "content_category_map.example.yaml", "project_layout_profiles.yaml"],
        )
        src_top = sorted(
            p.name for p in (ROOT / "src").iterdir()
            if not p.name.startswith(".") and not p.name.endswith(".egg-info") and p.name != "__pycache__"
        )
        self.assertEqual(src_top, ["doctree"])

    def test_src_doctree_and_tests_mirror_self_documenting_structure(self):
        """src/doctree/와 tests/는 동일한 하위 디렉터리 및 1:1 모듈 구조를 갖는다."""
        pkg_root = ROOT / "src" / "doctree"
        src_dirs = sorted(
            str(p.relative_to(pkg_root))
            for p in pkg_root.rglob("*")
            if p.is_dir() and p.name != "__pycache__"
        )
        test_dirs = sorted(
            str(p.relative_to(ROOT / "tests"))
            for p in (ROOT / "tests").rglob("*")
            if p.is_dir() and p.name != "__pycache__"
        )
        self.assertEqual(
            src_dirs,
            [
                "content_taxonomy",
                "doctree_cli",
                "drive_sync",
                "pdf_threat_audit",
                "runtime_env",
            ],
        )
        self.assertEqual(src_dirs, test_dirs)

        src_modules = sorted(
            str(p.relative_to(pkg_root))
            for p in pkg_root.rglob("*.py")
            if p.name != "__init__.py"
        )
        expected_tests = sorted(
            str(Path(rel).parent / f"test_{Path(rel).name}") for rel in src_modules
        )
        actual_tests = sorted(
            str(p.relative_to(ROOT / "tests"))
            for p in (ROOT / "tests").rglob("*.py")
            if p.name != "__init__.py"
        )
        self.assertEqual(actual_tests, expected_tests)

    def test_all_modules_and_facades_importable(self):
        """모든 도메인 모듈과 패키지 Facade(__all__)가 정상 import된다."""
        for name in (
            "doctree",
            "doctree.runtime_env",
            "doctree.runtime_env.workspace_settings",
            "doctree.runtime_env.layout_profile_store",
            "doctree.runtime_env.project_scaffolder",
            "doctree.drive_sync",
            "doctree.drive_sync.drive_pdf_fetcher",
            "doctree.drive_sync.snapshot_diff_tracker",
            "doctree.drive_sync.workspace_sync_engine",
            "doctree.content_taxonomy",
            "doctree.content_taxonomy.category_classifier",
            "doctree.content_taxonomy.duplicate_and_naming_linter",
            "doctree.content_taxonomy.practice_kata_builder",
            "doctree.content_taxonomy.snippet_registry",
            "doctree.pdf_threat_audit",
            "doctree.pdf_threat_audit.pdf_byte_inspector",
            "doctree.pdf_threat_audit.pdf_page_ingestor",
            "doctree.pdf_threat_audit.threat_intel_client",
            "doctree.doctree_cli",
            "doctree.doctree_cli.command_dispatcher",
            "doctree.doctree_cli.terminal_tree_renderer",
        ):
            mod = importlib.import_module(name)
            if name == "doctree" or name.count(".") in (1, 2):
                self.assertTrue(hasattr(mod, "__all__"), f"{name} must define __all__")
            if hasattr(mod, "__all__"):
                for exported in mod.__all__:
                    self.assertTrue(hasattr(mod, exported), f"{name}.{exported}")
                    if exported != "__version__":
                        self.assertFalse(exported.startswith("_"), f"{name}.{exported} must be public")
            if name.count(".") == 1:
                self.assertTrue(set(mod.__all__).issubset(set(dir(mod))))
                with self.assertRaises(AttributeError):
                    _ = mod.non_existent_symbol_xyz

    def _assert_import_node_respects_boundaries(
        self,
        py_file: Path,
        layer: str,
        node: ast.ImportFrom,
    ) -> None:
        if not node.module or not node.module.startswith("doctree"):
            return
        target_parts = node.module.split(".")
        target_layer = target_parts[1] if len(target_parts) > 1 else "root"

        if layer == "runtime_env":
            self.assertEqual(
                target_layer,
                "runtime_env",
                f"runtime_env 모듈({py_file.name})이 상위 계층({node.module})을 참조함",
            )
        if layer in {"drive_sync", "content_taxonomy", "pdf_threat_audit"}:
            self.assertIn(
                target_layer,
                {layer, "runtime_env"},
                f"도메인 모듈({layer}/{py_file.name})이 허용되지 않은 계층({node.module})을 참조함",
            )

        target_mod = importlib.import_module(node.module)
        target_all = set(getattr(target_mod, "__all__", ()))
        for alias in node.names:
            if alias.name != "__version__":
                self.assertFalse(
                    alias.name.startswith("_"),
                    f"{py_file.name}에서 비공개 심볼 {node.module}.{alias.name}를 import함",
                )
            self.assertIn(
                alias.name,
                target_all,
                f"{py_file.name}에서 __all__ 밖의 심볼 {node.module}.{alias.name}를 import함",
            )

    def test_module_encapsulation_and_layer_import_boundaries(self):
        """모든 모듈은 공개 심볼(__all__)만 외부에 노출·참조하며 계층 간 단방향 의존성, 무결합 및 모듈 전역 불변성을 지킨다."""
        self.assertIsInstance(SUPPORTED_LANG_MAP, MappingProxyType)
        with self.assertRaises(TypeError):
            SUPPORTED_LANG_MAP["rb"] = "Ruby"  # type: ignore[index]

        pkg_root = ROOT / "src" / "doctree"
        for py_file in sorted(pkg_root.rglob("*.py")):
            rel_parts = py_file.relative_to(pkg_root).parts
            layer = rel_parts[0] if len(rel_parts) > 1 else "root"
            mod_name = ".".join(py_file.relative_to(ROOT / "src").with_suffix("").parts)
            mod = importlib.import_module(mod_name)
            for attr_name, attr_val in vars(mod).items():
                if not attr_name.startswith("__"):
                    self.assertNotIsInstance(
                        attr_val,
                        (list, dict, set),
                        f"{mod_name}.{attr_name} must use immutable container (tuple/MappingProxyType/frozenset)",
                    )

            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self._assert_import_node_respects_boundaries(py_file, layer, node)


class TestSnapshotsIgnoredAndNoLedger(unittest.TestCase):
    def test_no_ledger_directory(self):
        """ledger/ 디렉터리는 존재하지 않고 snapshots/만 사용한다."""
        self.assertFalse((ROOT / "ledger").exists())

    def test_gitignore_has_snapshots(self):
        """.gitignore에 snapshots/가 있다."""
        lines = [line.strip() for line in (ROOT / ".gitignore").read_text().splitlines()]
        self.assertIn("snapshots/", lines)

    def test_snapshots_outputs_ignored_by_git(self):
        """스냅샷 산출물(snapshots/*)은 git이 무시해 커밋되지 않는다."""
        result = subprocess.run(
            ["git", "check-ignore", "-q", "snapshots/2026-10-27.json"],
            cwd=ROOT,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


@hypothesis.settings(max_examples=80, deadline=None)
@given(
    raw_val=st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=60)),
    folder=st.text(max_size=40),
    name=st.text(max_size=40),
    page_num=st.integers(min_value=1, max_value=5000),
)
@example(raw_val=None, folder="", name="", page_num=1)
@example(
    raw_val="A|{B}<C>\"'\\\n\r\t\x00\u200b```",
    folder="/   /  /",
    name="  한글.pdf  ",
    page_num=1,
)
@example(raw_val=False, folder="CS/DB", name="튜닝.pdf", page_num=12)
def test_property_fuzz_workspace_settings_string_and_path_helpers(
    raw_val: object,
    folder: str,
    name: str,
    page_num: int,
) -> None:
    """normalize_nfc, sanitize_dot_label, format_record_path, build_default_toc_entry의 멱등성·구분자 제거·경로 정규화 불변식을 검증한다."""
    nfc = normalize_nfc(raw_val)
    assert normalize_nfc(nfc) == nfc
    assert unicodedata.is_normalized("NFC", nfc)

    label = sanitize_dot_label(str(raw_val or ""))
    assert sanitize_dot_label(label) == label
    assert all(ch not in label for ch in ("|", "{", "}", "<", ">", '"', "\\", "\n", "\r", "\t"))
    assert "```" not in label
    assert all(ch.isprintable() for ch in label)

    rec_path = format_record_path({"folder_path": folder, "name": name})
    assert unicodedata.is_normalized("NFC", rec_path)
    assert not rec_path.startswith("/")

    toc = build_default_toc_entry(nfc, page_num, chapter_title="", section_title="   ")
    assert toc["page"] == page_num
    assert toc["chapter"] == DEFAULT_TOC_CHAPTER
    assert toc["section"] == f"{page_num:02d}_페이지_핵심"


if __name__ == "__main__":
    unittest.main()
