"""중복·파일명 점검 단위 테스트."""
import copy
import inspect
import types
import unicodedata
import unittest
from unittest import mock

from hypothesis import example, given, settings
from hypothesis import strategies as st

from doctree.content_taxonomy import duplicate_and_naming_linter as checks
from doctree.content_taxonomy.practice_kata_builder import _slugify
from doctree.runtime_env.workspace_settings import normalize_nfc


def _rec(id_, name, folder="IT/개발", sha256="m", size=1):
    return {
        "id": id_,
        "name": name,
        "folder_path": folder,
        "size_bytes": size,
        "sha256": sha256,
        "modified_time": "2026-01-01T00:00:00Z",
        "web_view_link": f"https://drive/{id_}",
    }


def _ids(w):
    return [f["id"] for f in w.files]


class TestExactDuplicates(unittest.TestCase):
    def test_groups_same_sha256(self):
        """SHA-256이 같은 파일 묶음마다 '완전 중복' 경고 하나를 만든다."""
        recs = [
            _rec("a", "클린코드.pdf", sha256="x"),
            _rec("b", "클린코드.pdf", folder="백업", sha256="x"),
            _rec("c", "리팩터링.pdf", sha256="y"),
            _rec("d", "리팩터링 2판.pdf", sha256="z"),
        ]
        ws = checks.find_exact_duplicates(recs)
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0].kind, "exact_duplicate")
        self.assertEqual(sorted(_ids(ws[0])), ["a", "b"])
        self.assertIn("완전 중복", ws[0].message)

    def test_sha256_only_names_irrelevant(self):
        """완전 중복은 SHA-256만으로 판정한다(이름 무관)."""
        recs = [
            _rec("a", "전혀다른이름.pdf", sha256="x"),
            _rec("b", "다른책.pdf", sha256="x"),
            _rec("c", "같은이름.pdf", sha256="p"),
            _rec("d", "같은이름.pdf", sha256="q"),
        ]
        ws = checks.find_exact_duplicates(recs)
        self.assertEqual([sorted(_ids(w)) for w in ws], [["a", "b"]])

    def test_empty_sha256_never_grouped(self):
        """SHA-256이 비어 있는 파일은 중복으로 묶지 않는다."""
        recs = [_rec("a", "a.pdf", sha256=""), _rec("b", "b.pdf", sha256=""),
                _rec("c", "c.pdf", sha256=None)]
        self.assertEqual(checks.find_exact_duplicates(recs), [])


class TestSameTitleDifferentContent(unittest.TestCase):
    def test_same_title_key_different_sha256(self):
        """제목 키가 같지만 SHA-256이 다른 묶음을 '동명 다른 파일'로 보고한다."""
        recs = [
            _rec("a", "Clean Code.pdf", sha256="p"),
            _rec("b", "Copy of clean-code.PDF", folder="백업", sha256="q"),
            _rec("c", "리팩터링.pdf", sha256="r"),
        ]
        ws = checks.find_same_title_different_content(recs)
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0].kind, "same_title_different_content")
        self.assertEqual(sorted(_ids(ws[0])), ["a", "b"])
        self.assertIn("동명 다른 파일", ws[0].message)

    def test_all_same_sha256_is_exact_duplicate_only(self):
        """묶음 전체가 SHA-256 하나를 공유하면 완전 중복으로만 보고한다."""
        recs = [_rec("a", "클린코드.pdf", sha256="x"),
                _rec("b", "클린 코드.pdf", folder="백업", sha256="x")]
        self.assertEqual(checks.find_same_title_different_content(recs), [])

    def test_mixed_group_reported_whole(self):
        """SHA-256 값이 2종 이상인 묶음은 묶음 전체를 보고한다."""
        recs = [_rec("a", "클린코드.pdf", sha256="x"),
                _rec("b", "클린코드.pdf", folder="백업", sha256="x"),
                _rec("c", "클린코드.pdf", folder="기타", sha256="y")]
        ws = checks.find_same_title_different_content(recs)
        self.assertEqual([sorted(_ids(w)) for w in ws], [["a", "b", "c"]])


class TestFilenameChecks(unittest.TestCase):
    def _kinds(self, name):
        return [w.kind for w in checks.find_filename_issues([_rec("a", name)])]

    def test_missing_pdf_extension(self):
        """이름이 .pdf(대소문자 무시)로 끝나지 않으면 확장자 누락 경고."""
        self.assertEqual(self._kinds("클린코드"), ["missing_pdf_extension"])
        self.assertEqual(self._kinds("클린코드.PDF"), [])
        self.assertEqual(self._kinds("클린코드.pdf"), [])

    def test_copy_of_prefix(self):
        """'Copy of '로 시작하는 이름은 접두어 경고."""
        self.assertEqual(self._kinds("Copy of 클린코드.pdf"), ["copy_of_prefix"])
        self.assertEqual(self._kinds("Copyof 클린코드.pdf"), [])

    def test_surrounding_whitespace(self):
        """이름 앞뒤 공백은 공백 경고."""
        self.assertEqual(self._kinds(" 클린코드.pdf"), ["surrounding_whitespace"])
        self.assertEqual(self._kinds("클린코드.pdf "),
                         ["missing_pdf_extension", "surrounding_whitespace"])
        self.assertEqual(self._kinds("클린 코드.pdf"), [])

    def test_warning_carries_file_and_message(self):
        """파일명 경고는 대상 파일 하나와 한국어 메시지를 가진다."""
        rec = _rec("a", "Copy of 클린코드")
        ws = checks.find_filename_issues([rec])
        self.assertEqual([w.kind for w in ws], ["missing_pdf_extension", "copy_of_prefix"])
        for w in ws:
            self.assertEqual(w.files, [rec])
            self.assertIn("IT/개발/Copy of 클린코드", w.message)


class TestRunChecks(unittest.TestCase):
    def _records(self):
        return [
            _rec("a", "클린코드.pdf", sha256="x"),
            _rec("b", "다른이름.pdf", folder="백업", sha256="x"),
            _rec("c", "리팩터링.pdf", sha256="p"),
            _rec("d", "리팩터링.pdf", folder="백업", sha256="q"),
            _rec("e", "Copy of 테스트주도개발", sha256="t"),
            _rec("f", " 객체지향.pdf", sha256="u"),
        ]

    def test_structured_list_in_kind_order(self):
        """종류·대상 파일·메시지를 가진 경고 목록을 종류 순 → 경로 순으로 반환한다."""
        ws = checks.run_checks(self._records())
        self.assertTrue(all(isinstance(w, checks.CheckWarning) for w in ws))
        self.assertEqual(
            [(w.kind, _ids(w)) for w in ws],
            [
                ("exact_duplicate", ["a", "b"]),
                ("same_title_different_content", ["c", "d"]),
                ("missing_pdf_extension", ["e"]),
                ("copy_of_prefix", ["e"]),
                ("surrounding_whitespace", ["f"]),
            ],
        )
        for w in ws:
            self.assertIsInstance(w.kind, str)
            self.assertIsInstance(w.files, list)
            self.assertTrue(w.message)
        self.assertEqual(checks.run_checks(list(reversed(self._records()))), ws)

    def test_empty_records(self):
        """레코드가 없으면 빈 목록."""
        self.assertEqual(checks.run_checks([]), [])

    def test_does_not_mutate_records(self):
        """점검은 입력 레코드를 수정하지 않는다."""
        recs = self._records()
        before = copy.deepcopy(recs)
        checks.run_checks(recs)
        self.assertEqual(recs, before)

    def test_never_touches_drive_api(self):
        """점검은 드라이브 API를 호출하지 않는다(경고만 만든다)."""
        src = inspect.getsource(checks)
        self.assertNotIn("googleapiclient", src)
        self.assertNotIn("drive_pdf_fetcher", src)
        self.assertNotIn("doctree.drive_sync", src)
        for value in vars(checks).values():
            if isinstance(value, types.ModuleType):
                self.assertFalse(value.__name__.startswith(("googleapiclient", "doctree.drive_sync")))
        with mock.patch("googleapiclient.discovery.build") as build:
            checks.run_checks(self._records())
        build.assert_not_called()

    def test_title_key_pipeline_order_and_root_folder_path_formatting(self):
        """title_key의 정규화 단계 순서(NFC → .pdf 제거 → Copy of 제거 → 공백/하이픈 제거 → 소문자)와 루트 폴더 경로 포맷을 검증한다."""
        # NFD 한글 자모('ᄀ'+'ᅡ') + 'Copy of ' + 하이픈/공백 + 대문자 '.PDF'
        self.assertEqual(checks.title_key("Copy of \u1100\u1161 - 나 다.PDF"), "가나다")
        # 끝에 .pdf가 두 번 있으면 마지막 확장자 하나만 제거됨
        self.assertEqual(checks.title_key("archive.pdf.PDF"), "archive.pdf")
        # None 또는 빈 문자열 또는 공백/하이픈만 있는 파일명은 빈 title_key가 되어 동명 그룹에서 제외됨
        self.assertEqual(checks.title_key(""), "")
        self.assertEqual(
            checks.find_same_title_different_content(
                [_rec("e1", "---.pdf", sha256="s1"), _rec("e2", "   .pdf", sha256="s2")]
            ),
            [],
        )
        # folder_path가 빈 문자열("")인 루트 파일은 앞에 '/'가 붙지 않는다
        root_rec = _rec("root1", "루트파일", folder="")
        ws = checks.find_filename_issues([root_rec])
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0].message, "파일명에 .pdf 확장자가 없음: 루트파일")

    @settings(max_examples=80, deadline=None)
    @given(raw_text=st.text(max_size=60), idx=st.integers(min_value=1, max_value=99))
    @example(raw_text="Copy of \u1100\u1161\u11a8 - \t\n 리팩터링.PDF", idx=1)
    @example(raw_text="___///\x00\x7f___", idx=7)
    @example(raw_text="---.pdf\n", idx=3)
    def test_property_fuzz_unicode_normalization_and_slug_idempotence(self, raw_text: str, idx: int) -> None:
        """임의 유니코드(NFD 자모·제어문자·공백·특수문자)에 대해 normalize_nfc·_slugify 멱등성과 title_key·run_checks 순열 불변성을 검증한다."""
        nfd_text = unicodedata.normalize("NFD", raw_text)
        nfc_text = normalize_nfc(raw_text)
        self.assertEqual(normalize_nfc(nfc_text), nfc_text)
        self.assertEqual(normalize_nfc(nfd_text), nfc_text)
        self.assertEqual(checks.title_key(nfd_text), checks.title_key(nfc_text))

        slug = _slugify(raw_text, idx)
        self.assertEqual(_slugify(slug, idx), slug)
        self.assertEqual(_slugify(nfd_text, idx), slug)

        recs = [_rec("r1", nfd_text, sha256="sha-1"), _rec("r2", nfc_text, folder="   / ", sha256="sha-2")]
        self.assertEqual(checks.run_checks(recs), checks.run_checks(list(reversed(recs))))

    def test_metamorphic_record_cloning_and_title_noise_invariance(self) -> None:
        """메타모픽 관계: 동일 SHA-256 복제 시 완전 중복만 증가하고, 제목 노이즈 주입 시 동명 이본 그룹은 불변이다."""
        base_records = [
            _rec("id-1", "알고리즘개론.pdf", sha256="sha-alpha"),
            _rec("id-2", "클린아키텍처.pdf", sha256="sha-beta-1"),
            _rec("id-3", "클린아키텍처.pdf", folder="백업", sha256="sha-beta-2"),
        ]
        before_exact = checks.find_exact_duplicates(base_records)
        before_same_title = checks.find_same_title_different_content(base_records)
        self.assertEqual(len(before_exact), 0)
        self.assertEqual(len(before_same_title), 1)

        cloned_records = [*base_records, _rec("id-1-clone", "복사본.pdf", folder="백업", sha256="sha-alpha")]
        after_exact = checks.find_exact_duplicates(cloned_records)
        after_same_title = checks.find_same_title_different_content(cloned_records)
        self.assertEqual(len(after_exact), 1)
        self.assertEqual(_ids(after_exact[0]), ["id-1", "id-1-clone"])
        self.assertEqual(after_same_title, before_same_title)

        noisy_title = unicodedata.normalize("NFD", "Copy of 클린 - 아키텍처.PDF")
        noisy_records = [
            _rec("id-2", noisy_title, sha256="sha-beta-1"),
            _rec("id-3", "클린아키텍처.pdf", folder="백업", sha256="sha-beta-2"),
        ]
        noisy_same_title = checks.find_same_title_different_content(noisy_records)
        self.assertEqual([_ids(w) for w in noisy_same_title], [["id-2", "id-3"]])


if __name__ == "__main__":
    unittest.main()


