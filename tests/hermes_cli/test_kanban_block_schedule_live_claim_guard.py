"""CLI surface of the block/schedule live-claim fence (sibling of the
``complete`` guard in test_kanban_complete_live_claim_guard.py).

A claim-less ``hermes kanban block`` used to clear a live worker's claim and
close its run — observed when a delegate_task reviewer child stripped the
delegated-child env fence and blocked its parent worker's task. The DB-level
fence raises ``LiveClaimError``; these tests pin the CLI surface: the default
refusal with an actionable message, and ``--force`` as the operator override.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    yield home


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    return parser


def _live_running_task(kanban_home) -> tuple[str, int]:
    """A claimed running card whose worker process is alive (this process)."""
    from hermes_cli import kanban_db_dispatch as kbd

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="live", assignee="coder")
        assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
        kbd._set_worker_pid(conn, tid, os.getpid())
        run_id = kb._current_run_id(conn, tid)
    assert run_id is not None
    return tid, int(run_id)


def _task_status(tid: str) -> str:
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    return str(task.status)


def test_block_cli_refuses_live_worker_claim(kanban_home, capsys):
    tid, run_id = _live_running_task(kanban_home)

    args = _parser().parse_args(["kanban", "block", tid, "child tried to block"])
    assert kc.kanban_command(args) != 0

    err = capsys.readouterr().err
    assert "live worker" in err and "--force" in err

    # Nothing moved: run still open, card still running.
    with kbc.connect_closing() as conn:
        run = conn.execute("SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["ended_at"] is None
    assert _task_status(tid) == "running"


def test_block_cli_force_overrides_live_worker_claim(kanban_home):
    tid, run_id = _live_running_task(kanban_home)

    args = _parser().parse_args(["kanban", "block", "--force", tid, "operator override"])
    assert kc.kanban_command(args) == 0

    with kbc.connect_closing() as conn:
        run = conn.execute("SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["ended_at"] is not None and run["outcome"] == "blocked"
    assert _task_status(tid) == "blocked"


def test_block_cli_without_live_worker_unchanged(kanban_home):
    """Human flow on a card nobody is running (or whose worker is gone) blocks as before."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="admin", assignee="coder")
        assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
        # no _set_worker_pid: a library/CLI claim protects no live run

    args = _parser().parse_args(["kanban", "block", tid, "manual"])
    assert kc.kanban_command(args) == 0
    assert _task_status(tid) == "blocked"


def test_schedule_cli_refuses_then_forces(kanban_home, capsys):
    tid, run_id = _live_running_task(kanban_home)

    args = _parser().parse_args(["kanban", "schedule", tid, "later"])
    assert kc.kanban_command(args) != 0
    err = capsys.readouterr().err
    assert "live worker" in err and "--force" in err

    with kbc.connect_closing() as conn:
        run = conn.execute("SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["ended_at"] is None
    assert _task_status(tid) == "running"

    args = _parser().parse_args(["kanban", "schedule", "--force", tid, "operator override"])
    assert kc.kanban_command(args) == 0
    with kbc.connect_closing() as conn:
        run = conn.execute("SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["ended_at"] is not None and run["outcome"] == "scheduled"
    assert _task_status(tid) == "scheduled"
