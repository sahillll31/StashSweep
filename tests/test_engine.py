"""Tests for the PA engine. All fixtures create mutable temp dirs; nothing touches real user files."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from pa.analyzers import find_duplicates, find_junk_dirs, find_large_old
from pa.config import Config, SizeRule
from pa.executor import build_plan
from pa.quarantine import (
    list_quarantined,
    purge,
    quarantine_item,
    quarantine_root,
    restore,
)


@pytest.fixture()
def workdir(tmp_path):
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    qbase = tmp_path / "quarantine"
    yield tmp_path
    shutil.rmtree(tmp_path, ignore_errors=True)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ duplicates

def test_find_duplicates_with_copy(workdir):
    a = _write(workdir / "files/a.bin", b"same content")
    b = _write(workdir / "files/b.bin", b"same content")
    c = _write(workdir / "files/c.bin", b"differs")

    import datetime
    entries = [
        type("E", (), {"path": a, "size": a.stat().st_size, "mtime": datetime.datetime.now(), "category": "x", "hash": None})(),
        type("E", (), {"path": b, "size": b.stat().st_size, "mtime": datetime.datetime.now(), "category": "x", "hash": None})(),
        type("E", (), {"path": c, "size": c.stat().st_size, "mtime": datetime.datetime.now(), "category": "x", "hash": None})(),
    ]
    findings = find_duplicates(entries)
    rules = {f.rule for f in findings}
    assert "duplicates" in rules
    flagged_paths = [e.path for f in findings for e in f.entries]
    assert len(flagged_paths) == 1
    assert flagged_paths[0] in {a, b}


def test_find_duplicates_no_copies(workdir):
    a = _write(workdir / "files/x.bin", b"one")
    import datetime

    class E:
        path, size, mtime, category = a, a.stat().st_size, datetime.datetime.now(), "x"

    findings = find_duplicates([E()])
    assert findings == []


# ------------------------------------------------------------------ bloat

def test_find_large_old(tmp_path):
    big_old = _write(tmp_path / "big.bin", b"x" * (110 * 1024 * 1024))
    old_time = time.time() - 800 * 24 * 3600
    os.utime(big_old, (old_time, old_time))

    small_new = _write(tmp_path / "small.bin", b"y" * (100 * 1024 * 1024))

    import datetime
    now = datetime.datetime.now()

    class E1:
        path, size, mtime, category = big_old, big_old.stat().st_size, datetime.datetime.fromtimestamp(old_time), "x"

    class E2:
        path, size, mtime, category = small_new, small_new.stat().st_size, now, "x"

    cfg = Config()
    cfg.size_rules = [SizeRule(name="sw", description="test", min_size_mb=100, max_age_days=730)]
    findings = find_large_old([E1(), E2()], cfg, now=now)
    assert len(findings) == 1
    assert findings[0].entries[0].path == big_old


# ------------------------------------------------------------------ junk dirs

def test_find_junk_dirs(workdir):
    _write(workdir / "files/node_modules/somelib/index.js", b"code")
    _write(workdir / "files/node_modules/somelib/in.zip", b"x" * 1024)

    cfg = Config()
    cfg.junk_dir_rules = [type("R", (), {"name": "j", "description": "d", "patterns": ["node_modules"]})()]
    findings = find_junk_dirs(workdir / "files", cfg)
    assert len(findings) == 1
    assert findings[0].entries[0].path.name == "node_modules"
    assert findings[0].reclaimable_bytes == 1024 + 4


def test_find_junk_dirs_skips_nested(workdir):
    _write(workdir / "files/node_modules/lib/.cache/blob", b"x" * 256)
    _write(workdir / "files/node_modules/lib/pkg.py", b"y")

    cfg = Config()
    cfg.junk_dir_rules = [
        type("R", (), {"name": "j", "description": "d", "patterns": ["node_modules", ".cache"]})()
    ]
    findings = find_junk_dirs(workdir / "files", cfg)
    assert len(findings) == 1  # .cache folded into the parent node_modules finding


# ------------------------------------------------------------------ executor / quarantine

def test_quarantine_restore_roundtrip(workdir):
    src = _write(workdir / "files/something.txt", b"hi")
    record = quarantine_item(src, base=workdir / "quarantine")

    assert not src.exists()
    quarantined = Path(record["quarantined"])
    assert quarantined.exists()

    ok = restore(record["id"], base=workdir / "quarantine")
    assert ok
    assert src.exists()
    assert src.read_bytes() == b"hi"
    assert not quarantined.exists()


def test_quarantine_restore_on_name_collision_uses_suffix(workdir):
    src = _write(workdir / "files/a.txt", b"one")
    record = quarantine_item(src, base=workdir / "quarantine")

    _write(workdir / "files/a.txt", b"newer content")
    ok = restore(record["id"], base=workdir / "quarantine")
    assert ok
    assert (workdir / "files/a.txt").read_bytes() == b"newer content"
    restored = list((workdir / "files").glob("a_restored_*.txt"))
    assert len(restored) == 1
    assert restored[0].read_bytes() == b"one"


def test_quarantine_purge_frees_space(workdir):
    src = _write(workdir / "files/a.txt", b"x" * 4096)
    record = quarantine_item(src, base=workdir / "quarantine")
    assert len(list_quarantined(base=workdir / "quarantine")) == 1

    freed = purge(record["id"], base=workdir / "quarantine")
    assert freed == 4096
    assert not Path(record["quarantined"]).exists()
    assert list_quarantined(base=workdir / "quarantine") == []


def test_build_plan_filters_deny_paths(tmp_path):
    import datetime

    target = _write(tmp_path / "target.txt", b"data")
    declined = _write(tmp_path / "declined.txt", b"data")

    cfg = Config()
    cfg.deny_paths = [str(declined)]

    class E1:
        path, size, mtime, category = target, target.stat().st_size, datetime.datetime.now(), "x"

    class E2:
        path, size, mtime, category = declined, declined.stat().st_size, datetime.datetime.now(), "x"

    finding = type("F", (), {"rule": "r", "description": "d", "entries": [E1(), E2()], "reclaimable_bytes": 8})()
    plan = build_plan([finding], cfg)
    assert len(plan.actions) == 1
    assert plan.actions[0][0].path == target


def test_quarantine_root_is_created_and_used_under_base(workdir):
    qbase = workdir / "quarantine"
    root = quarantine_root(qbase)
    assert root.exists() and root.is_dir()
    assert root == qbase
