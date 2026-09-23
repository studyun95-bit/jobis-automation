import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobis_meals import __main__ as cli


def saved_plan(root, name="2026-09-test"):
    folder = root / "runs" / name
    folder.mkdir(parents=True)
    plan = {"company": "테스트 회사", "month": "2026-09", "created_at": "2026-09-22T10:00:00+09:00", "entries": []}
    path = folder / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def test_scan_opens_preview_after_automation_browser_closes(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    order = []

    @contextmanager
    def session(*args):
        order.append("browser_open")
        try:
            yield object()
        finally:
            order.append("browser_closed")

    def scan(ui, config, month, folder):
        order.append("scan")
        folder.mkdir(parents=True)
        (folder / "preview.html").write_text("<h1>result</h1>")
        return {}, {"keep": 3}

    def open_file(path, config):
        assert order[-1] == "browser_closed"
        assert path.name == "plan.json"
        assert path.with_name("preview.html").is_file()
        order.append("preview_open")
        return 0

    monkeypatch.setattr(cli, "browser_session", session)
    monkeypatch.setattr(cli, "scan_to_folder", scan)
    monkeypatch.setattr(cli, "reopen_preview", open_file)
    assert cli.execute("scan", {}, "chromium", month="2026-09", open_preview=True) == 0
    assert order == ["browser_open", "scan", "browser_closed", "preview_open"]


@pytest.mark.parametrize("chrome_result", [1, OSError("missing Chrome"), subprocess.TimeoutExpired("open", 15)])
def test_mac_falls_back_to_safari(monkeypatch, tmp_path, chrome_result):
    path = tmp_path / "검사 결과 공백" / "preview.html"
    path.parent.mkdir()
    path.write_text("hello")
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    calls = []

    def launch(args, **kwargs):
        calls.append(args)
        if args[2] == "Google Chrome":
            if isinstance(chrome_result, Exception):
                raise chrome_result
            return SimpleNamespace(returncode=chrome_result)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", launch)
    assert cli.open_preview_file(path)
    assert calls == [["/usr/bin/open", "-a", "Google Chrome", str(path)],
                     ["/usr/bin/open", "-a", "Safari", str(path)]]


def test_open_failure_keeps_file_and_explains_recovery(monkeypatch, tmp_path, capsys):
    path = tmp_path / "preview.html"
    path.write_text("saved result")
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    assert not cli.open_preview_file(path)
    message = capsys.readouterr().out
    assert "자동 열기에 실패" in message and "메뉴 5번" in message
    assert str(path) in message
    assert path.read_text() == "saved result"


def test_latest_preview_is_newest_run_not_highest_query_month(monkeypatch, tmp_path):
    older = saved_plan(tmp_path, "2026-09-old")
    newer = saved_plan(tmp_path, "2026-08-new")
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    assert cli.latest_plan() == newer


def test_reopen_rebuilds_missing_html_without_logging_in(monkeypatch, tmp_path):
    path = saved_plan(tmp_path)
    monkeypatch.setattr(cli, "ROOT", tmp_path)

    def no_browser(*args):
        pytest.fail("Saved preview must not open an automation session")

    def open_file(plan_path, config):
        assert plan_path == path
        assert "테스트 회사" in path.with_name("preview.html").read_text()
        return 0

    monkeypatch.setattr(cli, "browser_session", no_browser)
    monkeypatch.setattr(cli, "start_preview", open_file)
    assert cli.execute("preview", {}, "chromium") == 0


def test_menu_5_reopens_saved_preview(monkeypatch):
    answers = iter(["5", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    reopened = []
    monkeypatch.setattr(cli, "reopen_preview", lambda **kwargs: reopened.append(True))
    assert cli.menu({}, "chromium") == 0
    assert reopened == [True]


def test_empty_selection_never_opens_jobis(monkeypatch, tmp_path, capsys):
    path = saved_plan(tmp_path)
    monkeypatch.setattr(cli, "browser_session", lambda *a: pytest.fail("No selected receipts"))
    assert cli.execute("apply", {}, "chromium", plan_path=path) == 0
    assert "0건" in capsys.readouterr().out


@pytest.mark.parametrize("confirmation", ["y", "Y", " y "])
def test_menu_applies_the_selection_shown_before_confirmation(monkeypatch, tmp_path, confirmation):
    from jobis_meals.reports import write_json
    from jobis_meals.selection import default_selection
    path = saved_plan(tmp_path)
    _, plan = cli.load_plan(path)
    plan["entries"] = [{"receipt": {"id": "1"}, "decision": {"action": "change"}}]
    write_json(path, plan)
    original = default_selection(plan)
    write_json(path.with_name("selection.json"), original)
    monkeypatch.setattr(cli, "latest_plan", lambda: path)
    answers = iter(["3", "", confirmation, "0"])

    def answer(_):
        value = next(answers)
        if value == confirmation:
            write_json(path.with_name("selection.json"), {**original, "selected_ids": []})
        return value

    received = []
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(cli, "execute", lambda *a, **kw: received.append(kw["selection"]))
    cli.menu({}, "chromium")
    assert received == [original]
