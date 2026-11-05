"""스냅샷 저장과 직전 스냅샷 비교 검증."""
import json
import shutil
import tempfile
import unicodedata
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from doctree.drive_sync.snapshot_diff_tracker import (
    KST,
    Change,
    Changes,
    change_summary,
    diff_snapshots,
    load_latest_snapshot,
    load_snapshot_records,
    record_run,
    save_snapshot,
    snapshot_date,
)
from doctree.runtime_env.boundary_errors import SnapshotStorageError


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


class TestSaveSnapshot(unittest.TestCase):
    def setUp(self):
        self.snapshots = Path(tempfile.mkdtemp())

    def test_saves_dated_json(self):
        """snapshots/YYYY-MM-DD.json에 레코드를 UTF-8 JSON으로 저장한다."""
        recs = [_rec("a", "클린코드.pdf")]
        path = save_snapshot(recs, self.snapshots, datetime(2026, 10, 21, 9, 30))
        self.assertEqual(path, self.snapshots / "2026-10-21.json")
        raw = path.read_text(encoding="utf-8")
        self.assertIn("클린코드.pdf", raw)
        data = json.loads(raw)
        self.assertEqual(data["records"], recs)
        self.assertEqual(data["taken_at"], "2026-10-21T09:30:00")
        self.assertEqual(data["count"], 1)

    def test_stable_output(self):
        """같은 입력은 레코드 순서·키 순서와 무관하게 같은 바이트로 저장된다."""
        a, b = _rec("a", "A.pdf"), _rec("b", "B.pdf")
        now = datetime(2026, 10, 21, 9, 30)
        p1 = save_snapshot([a, b], self.snapshots, now).read_bytes()
        reordered = dict(reversed(list(b.items())))
        p2 = save_snapshot([reordered, a], self.snapshots, now).read_bytes()
        self.assertEqual(p1, p2)

    def test_same_day_overwrites(self):
        """같은 날 재실행하면 같은 파일을 덮어쓴다."""
        save_snapshot([_rec("a", "A.pdf")], self.snapshots, datetime(2026, 10, 21, 9, 0))
        save_snapshot([_rec("b", "B.pdf")], self.snapshots, datetime(2026, 10, 21, 18, 0))
        files = list(self.snapshots.iterdir())
        self.assertEqual([f.name for f in files], ["2026-10-21.json"])
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual([r["id"] for r in data["records"]], ["b"])


class TestLoadLatestSnapshot(unittest.TestCase):
    def setUp(self):
        self.snapshots = Path(tempfile.mkdtemp())

    def test_none_when_missing(self):
        """스냅샷이 하나도 없으면 None."""
        self.assertIsNone(load_latest_snapshot(self.snapshots))

    def test_latest_by_date(self):
        """비교 기준은 존재하는 스냅샷 중 날짜가 가장 최근인 것이다."""
        save_snapshot([_rec("old", "O.pdf")], self.snapshots, datetime(2026, 9, 1))
        save_snapshot([_rec("new", "N.pdf")], self.snapshots, datetime(2026, 10, 20))
        save_snapshot([_rec("mid", "M.pdf")], self.snapshots, datetime(2026, 10, 2))
        day, recs = load_latest_snapshot(self.snapshots)
        self.assertEqual(day, date(2026, 10, 20))
        self.assertEqual([r["id"] for r in recs], ["new"])

    def test_includes_same_day_earlier_run(self):
        """같은 날 앞선 실행이 남긴 오늘 스냅샷도 비교 기준이 된다."""
        save_snapshot([_rec("y", "Y.pdf")], self.snapshots, datetime(2026, 10, 20))
        save_snapshot([_rec("t", "T.pdf")], self.snapshots, datetime(2026, 10, 21, 9))
        day, recs = load_latest_snapshot(self.snapshots)
        self.assertEqual(day, date(2026, 10, 21))
        self.assertEqual([r["id"] for r in recs], ["t"])

    def test_ignores_non_snapshot_files(self):
        """YYYY-MM-DD.json 형식이 아닌 파일은 무시한다."""
        save_snapshot([_rec("a", "A.pdf")], self.snapshots, datetime(2026, 10, 1))
        (self.snapshots / "notes.json").write_text("{}", encoding="utf-8")
        (self.snapshots / "2026-13-99.json").write_text("{}", encoding="utf-8")
        day, _ = load_latest_snapshot(self.snapshots)
        self.assertEqual(day, date(2026, 10, 1))


class TestDiffSnapshots(unittest.TestCase):
    def test_no_changes(self):
        """같은 목록이면 변경이 없다."""
        recs = [_rec("a", "A.pdf"), _rec("b", "B.pdf")]
        ch = diff_snapshots(recs, [dict(r) for r in recs])
        self.assertTrue(ch.is_empty)
        self.assertEqual(ch, Changes())

    def test_added_and_removed(self):
        """새 id는 추가, 사라진 id는 삭제로 구분한다."""
        ch = diff_snapshots([_rec("a", "A.pdf")], [_rec("b", "B.pdf")])
        self.assertEqual([r["id"] for r in ch.added], ["b"])
        self.assertEqual([r["id"] for r in ch.removed], ["a"])
        self.assertEqual((ch.renamed, ch.moved, ch.content_changed), ([], [], []))
        self.assertFalse(ch.is_empty)

    def test_renamed(self):
        """이름이 바뀌면 이름 변경(old→new)으로 기록한다."""
        ch = diff_snapshots([_rec("a", "A.pdf")], [_rec("a", "A2.pdf")])
        self.assertEqual(ch.renamed, [Change("a", "A2.pdf", "A.pdf", "A2.pdf")])
        self.assertEqual((ch.added, ch.removed, ch.moved), ([], [], []))

    def test_moved(self):
        """folder_path가 바뀌면 폴더 이동(old→new)으로 기록한다."""
        ch = diff_snapshots([_rec("a", "A.pdf", folder="IT")], [_rec("a", "A.pdf", folder="IT/개발")])
        self.assertEqual(ch.moved, [Change("a", "A.pdf", "IT", "IT/개발")])
        self.assertEqual(ch.renamed, [])

    def test_content_changed_by_sha256(self):
        """SHA-256이 바뀌면 내용 변경으로 기록한다."""
        ch = diff_snapshots([_rec("a", "A.pdf", sha256="x")], [_rec("a", "A.pdf", sha256="y")])
        self.assertEqual(ch.content_changed, [Change("a", "A.pdf", "x", "y")])

    def test_content_same_sha256_different_size_ignored(self):
        """SHA-256이 둘 다 있고 같으면 크기·수정시각이 달라도 내용 변경이 아니다."""
        old = _rec("a", "A.pdf", sha256="x", size=1)
        new = dict(_rec("a", "A.pdf", sha256="x", size=2), modified_time="2026-02-02T00:00:00Z")
        self.assertTrue(diff_snapshots([old], [new]).is_empty)

    def test_content_changed_by_size_when_sha256_missing(self):
        """SHA-256이 비어 있으면 크기로 내용 변경을 판단한다."""
        ch = diff_snapshots([_rec("a", "A.pdf", sha256="", size=1)], [_rec("a", "A.pdf", sha256="", size=2)])
        self.assertEqual(ch.content_changed, [Change("a", "A.pdf", "1", "2")])
        same = diff_snapshots([_rec("a", "A.pdf", sha256="", size=1)], [_rec("a", "A.pdf", sha256="", size=1)])
        self.assertTrue(same.is_empty)

    def test_identity_is_id_only(self):
        """같은 id면 이름·경로가 모두 바뀌어도 추가+삭제가 아니라 같은 책이다."""
        ch = diff_snapshots([_rec("a", "A.pdf", folder="X")], [_rec("a", "Z.pdf", folder="Y")])
        self.assertEqual((ch.added, ch.removed), ([], []))

    def test_same_name_different_id_is_add_remove(self):
        """이름·경로가 같아도 id가 다르면 다른 책(삭제+추가)이다."""
        ch = diff_snapshots([_rec("a", "A.pdf")], [_rec("b", "A.pdf")])
        self.assertEqual([r["id"] for r in ch.added], ["b"])
        self.assertEqual([r["id"] for r in ch.removed], ["a"])

    def test_rename_and_move_both_recorded(self):
        """이름 변경과 폴더 이동이 동시에 일어나면 둘 다 기록한다."""
        ch = diff_snapshots([_rec("a", "A.pdf", folder="X")], [_rec("a", "Z.pdf", folder="Y")])
        self.assertEqual(ch.renamed, [Change("a", "Z.pdf", "A.pdf", "Z.pdf")])
        self.assertEqual(ch.moved, [Change("a", "Z.pdf", "X", "Y")])

    def test_sorted_output(self):
        """각 목록은 입력 순서와 무관하게 정렬된다."""
        new = [_rec("b", "나.pdf"), _rec("a", "가.pdf")]
        ch1 = diff_snapshots([], new)
        ch2 = diff_snapshots([], list(reversed(new)))
        self.assertEqual(ch1, ch2)
        self.assertEqual([r["name"] for r in ch1.added], ["가.pdf", "나.pdf"])


class TestRecordRun(unittest.TestCase):
    def setUp(self):
        self.snapshots = Path(tempfile.mkdtemp())

    def test_record_run_without_prior_snapshot(self):
        """직전 스냅샷이 없으면 record_run이 first_run=True로 스냅샷만 만든다."""
        recs = [_rec("a", "A.pdf"), _rec("b", "B.pdf"), _rec("c", "C.pdf")]
        res = record_run(recs, self.snapshots, datetime(2026, 10, 21, 9, 0))
        self.assertTrue(res.first_run)
        self.assertEqual(res.total, 3)
        self.assertTrue(res.changes.is_empty)
        self.assertEqual(res.snapshot_path, self.snapshots / "2026-10-21.json")
        self.assertTrue(res.snapshot_path.is_file())
        self.assertEqual(change_summary(res), "최초 등록 3권")
        self.assertEqual([p.name for p in self.snapshots.iterdir()], ["2026-10-21.json"])

    def test_rerun_no_changes(self):
        """같은 목록으로 같은 날 재실행하면 스냅샷은 덮어쓰고 요약은 '변경 없음'."""
        recs = [_rec("a", "A.pdf")]
        record_run(recs, self.snapshots, datetime(2026, 10, 21, 9, 0))
        res = record_run(recs, self.snapshots, datetime(2026, 10, 21, 18, 0))
        self.assertFalse(res.first_run)
        self.assertTrue(res.changes.is_empty)
        self.assertEqual(change_summary(res), "변경 없음")
        self.assertEqual(len(list(self.snapshots.iterdir())), 1)

    def test_same_day_rerun_compares_with_earlier_run(self):
        """같은 날 재실행은 앞선 실행이 남긴 오늘 스냅샷과 비교한다(덮어쓰기 전에 읽음)."""
        record_run([_rec("y", "Y.pdf")], self.snapshots, datetime(2026, 10, 20, 9, 0))
        record_run([_rec("y", "Y.pdf"), _rec("m", "M.pdf")], self.snapshots, datetime(2026, 10, 21, 9, 0))
        res = record_run(
            [_rec("y", "Y.pdf"), _rec("m", "M.pdf"), _rec("e", "E.pdf")],
            self.snapshots,
            datetime(2026, 10, 21, 18, 0),
        )
        self.assertEqual([r["id"] for r in res.changes.added], ["e"])
        self.assertEqual(change_summary(res), "추가 1")
        data = json.loads(res.snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(r["id"] for r in data["records"]), ["e", "m", "y"])

    def test_mutations_and_anomalies_in_diff_summary_and_directory_masquerading(self):
        """change_summary 5대 항목 전체 출력, 한쪽만 sha256이 있을 때 size_bytes 폴백(and vs or 뮤테이션), 디렉터리 위장 파일 무시, 불변성 및 입력 비오염 검증."""
        import dataclasses

        from doctree.drive_sync.snapshot_diff_tracker import RunResult

        # 1) YYYY-MM-DD.json 이름을 가진 하위 디렉터리는 파일이 아니므로 무시해야 한다 (p.is_file() 뮤테이션 검증)
        (self.snapshots / "2099-12-31.json").mkdir(parents=True)
        self.assertIsNone(load_latest_snapshot(self.snapshots))

        # 2) 한쪽만 sha256이 있고 다른 쪽은 sha256이 비어 있을 때: size_bytes가 같으면 변경 아님, 다르면 크기 변경 기록 (oh and nh vs oh or nh 뮤테이션 검증)
        old_one_sha = [_rec("a", "A.pdf", sha256="sha-present", size=500)]
        new_no_sha_same_size = [_rec("a", "A.pdf", sha256="", size=500)]
        self.assertTrue(diff_snapshots(old_one_sha, new_no_sha_same_size).is_empty)

        new_no_sha_diff_size = [_rec("a", "A.pdf", sha256="", size=999)]
        ch_size = diff_snapshots(old_one_sha, new_no_sha_diff_size)
        self.assertEqual(ch_size.content_changed, [Change("a", "A.pdf", "500", "999")])

        # 3) change_summary에서 추가·삭제·이름 변경·폴더 이동·내용 변경 5가지가 모두 발생했을 때 정확한 문자열 포맷 검증
        ch_all = Changes(
            added=[_rec("1", "a.pdf")],
            removed=[_rec("2", "b.pdf"), _rec("3", "c.pdf")],
            renamed=[Change("4", "d2.pdf", "d1.pdf", "d2.pdf")] * 3,
            moved=[Change("5", "e.pdf", "f1", "f2")] * 4,
            content_changed=[Change("6", "f.pdf", "s1", "s2")] * 5,
        )
        run_all = RunResult(
            changes=ch_all,
            total=10,
            first_run=False,
            snapshot_path=self.snapshots / "2026-10-21.json",
        )
        self.assertEqual(
            change_summary(run_all),
            "추가 1 · 삭제 2 · 이름 변경 3 · 폴더 이동 4 · 내용 변경 5",
        )

        # 4) Change, Changes 및 RunResult 불변(frozen) 검증 및 diff_snapshots 반환 객체 수정 시 원본 입력 비오염 검증
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ch_size.content_changed[0].new = "mutated"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ch_size.added = []  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            run_all.total = 999  # type: ignore[misc]

        bad_elem_snap = self.snapshots / "2026-10-22.json"
        bad_elem_snap.write_text('{"count": 1, "records": ["not-a-dict"]}', encoding="utf-8")
        with self.assertRaises(SnapshotStorageError):
            load_snapshot_records(bad_elem_snap)

        with self.assertRaises(TypeError):
            snapshot_date(date(2026, 10, 21))  # type: ignore[arg-type]

        for bad_records in (
            ["not-a-mapping"],
            [{"name": "no_id.pdf"}],
            [{"id": 123, "name": "non_str_id.pdf"}],
            [_rec("dup", "A.pdf"), _rec("dup", "B.pdf")],
        ):
            with self.assertRaises(SnapshotStorageError):
                diff_snapshots(bad_records, [])  # type: ignore[arg-type]

        caller_new = [_rec("x", "Orig.pdf")]
        diff_res = diff_snapshots([], caller_new)
        diff_res.added[0]["name"] = "MutatedInResult.pdf"
        self.assertEqual(caller_new[0]["name"], "Orig.pdf")


_SNAPSHOT_ENTRY_STRATEGY = st.tuples(
    st.text(max_size=24),
    st.text(max_size=24),
    st.sampled_from(["", "a" * 64, "B" * 64, "  c" + "0" * 63 + "  "]),
    st.integers(min_value=0, max_value=100_000),
)
_SNAPSHOT_DICT_STRATEGY = st.dictionaries(
    keys=st.sampled_from([f"파일-{idx}" for idx in range(10)]),
    values=_SNAPSHOT_ENTRY_STRATEGY,
    max_size=8,
)


def _build_snapshot_from_map(raw_map: dict[str, tuple[str, str, str, int]]) -> list[dict]:
    return [
        _rec(file_id, name, folder=folder, sha256=sha, size=size)
        for file_id, (name, folder, sha, size) in raw_map.items()
    ]


@settings(max_examples=100, deadline=None)
@given(map_a=_SNAPSHOT_DICT_STRATEGY, map_b=_SNAPSHOT_DICT_STRATEGY)
def test_property_fuzz_diff_snapshots_algebraic_invariants(
    map_a: dict[str, tuple[str, str, str, int]],
    map_b: dict[str, tuple[str, str, str, int]],
) -> None:
    """임의의 두 스냅샷(A, B)에 대해 반사성(Reflexivity)·대칭성(Symmetry)·상호 배타성(Mutual Exclusivity) 대수적 불변식을 검증한다."""
    snap_a = _build_snapshot_from_map(map_a)
    snap_b = _build_snapshot_from_map(map_b)
    snap_a_nfd = [
        {
            **r,
            "id": f"  {unicodedata.normalize('NFD', r['id'])}  ",
            "name": unicodedata.normalize("NFD", r["name"]),
            "folder_path": unicodedata.normalize("NFD", r["folder_path"]),
            "sha256": r["sha256"].upper(),
        }
        for r in snap_a
    ]

    # 1) 반사성: 동일 스냅샷(및 NFD/대문자 해시 동치 변형본) 비교 시 변경 없음
    assert diff_snapshots(snap_a, snap_a).is_empty is True
    assert diff_snapshots(snap_a, snap_a_nfd).is_empty is True

    # 2) 대칭성: diff(A, B)와 diff(B, A)의 added/removed 및 변경 전후 값(old/new) 역전
    ab = diff_snapshots(snap_a, snap_b)
    ba = diff_snapshots(snap_b, snap_a)
    assert [r["id"] for r in ab.added] == [r["id"] for r in ba.removed]
    assert [r["id"] for r in ab.removed] == [r["id"] for r in ba.added]
    assert {(c.id, c.old, c.new) for c in ab.renamed} == {(c.id, c.new, c.old) for c in ba.renamed}
    assert {(c.id, c.old, c.new) for c in ab.moved} == {(c.id, c.new, c.old) for c in ba.moved}
    assert {(c.id, c.old, c.new) for c in ab.content_changed} == {
        (c.id, c.new, c.old) for c in ba.content_changed
    }

    # 3) 상호 배타성 및 기수 보존 법칙
    added_ids = {r["id"] for r in ab.added}
    removed_ids = {r["id"] for r in ab.removed}
    mutated_ids = {c.id for c in (*ab.renamed, *ab.moved, *ab.content_changed)}
    assert added_ids.isdisjoint(removed_ids)
    assert mutated_ids.isdisjoint(added_ids | removed_ids)
    assert len(snap_a) + len(added_ids) - len(removed_ids) == len(snap_b)


class SnapshotTimelineStateMachine(RuleBasedStateMachine):
    """[Pass 7 & 8] 다일차 스냅샷 기록(record_run)·최신 로드·원자적 쓰기 장애 주입(Fault Injection) 상태 머신."""

    def __init__(self) -> None:
        super().__init__()
        self.snapshots_dir = Path(tempfile.mkdtemp())
        self.current_time = datetime(2026, 10, 1, 9, 0, tzinfo=KST)
        self.latest_records: list[dict] | None = None

    def teardown(self) -> None:
        shutil.rmtree(self.snapshots_dir, ignore_errors=True)

    @rule(
        raw_map=_SNAPSHOT_DICT_STRATEGY,
        advance_days=st.integers(min_value=0, max_value=3),
        advance_hours=st.integers(min_value=1, max_value=6),
    )
    def rule_record_snapshot_run(
        self,
        raw_map: dict[str, tuple[str, str, str, int]],
        advance_days: int,
        advance_hours: int,
    ) -> None:
        self.current_time += timedelta(days=advance_days, hours=advance_hours)
        new_records = _build_snapshot_from_map(raw_map)
        expected_first = self.latest_records is None
        expected_changes = (
            Changes() if self.latest_records is None else diff_snapshots(self.latest_records, new_records)
        )
        run = record_run(new_records, self.snapshots_dir, self.current_time)
        assert run.first_run is expected_first
        assert run.changes == expected_changes
        assert run.total == len(new_records)
        self.latest_records = sorted((dict(r) for r in new_records), key=lambda r: str(r.get("id", "")))

    @rule(raw_map=_SNAPSHOT_DICT_STRATEGY)
    def rule_injected_io_fault_preserves_prior_snapshot(
        self,
        raw_map: dict[str, tuple[str, str, str, int]],
    ) -> None:
        new_records = _build_snapshot_from_map(raw_map)
        with (
            patch.object(Path, "replace", side_effect=OSError("simulated disk full")),
            pytest.raises(OSError, match="simulated disk full"),
        ):
            save_snapshot(new_records, self.snapshots_dir, self.current_time)

    @invariant()
    def invariant_latest_snapshot_matches_shadow_and_clean_tmp(self) -> None:
        assert not list(self.snapshots_dir.glob(".*.tmp"))
        loaded = load_latest_snapshot(self.snapshots_dir)
        if self.latest_records is None:
            assert loaded is None
        else:
            assert loaded == (snapshot_date(self.current_time), self.latest_records)


def test_stateful_snapshot_timeline_and_fault_injection() -> None:
    """다일차 스냅샷 상태 전이와 디스크 쓰기 장애 주입 시의 이전 스냅샷 보존 불변식을 검증한다."""
    run_state_machine_as_test(
        SnapshotTimelineStateMachine,
        settings=settings(max_examples=25, stateful_step_count=12, deadline=None),
    )


if __name__ == "__main__":
    unittest.main()
