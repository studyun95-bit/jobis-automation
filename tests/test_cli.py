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

    def open_file(path):
        assert order[-1] == "browser_closed"
        assert Path(path).is_file()
        order.append("preview_open")
        return True

    monkeypatch.setattr(cli, "browser_session", session)
    monkeypatch.setattr(cli, "scan_to_folder", scan)
    monkeypatch.setattr(cli, "open_preview_file", open_file)
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

    def open_file(preview_path):
        assert preview_path == path.parent / "preview.html"
        assert "테스트 회사" in preview_path.read_text()
        return True

    monkeypatch.setattr(cli, "browser_session", no_browser)
    monkeypatch.setattr(cli, "open_preview_file", open_file)
    assert cli.execute("preview", {}, "chromium") == 0


def test_menu_5_reopens_saved_preview(monkeypatch):
    answers = iter(["5", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    reopened = []
    monkeypatch.setattr(cli, "reopen_preview", lambda: reopened.append(True))
    assert cli.menu({}, "chromium") == 0
    assert reopened == [True]
