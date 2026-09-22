"""Local fixture only. No requests to the real Jobis service."""
import copy
import html
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from playwright.sync_api import expect, sync_playwright

from jobis_meals.browser import HEADERS, JobisUI, receipt_id
from jobis_meals.reports import digest, preview, write_json
from jobis_meals.preview_server import PreviewServer
from jobis_meals.selection import default_selection, load_selection
from jobis_meals.workflow import apply_plan, build_plan, preflight, verify_plan


@pytest.fixture
def config():
    return json.loads((Path(__file__).parents[1] / "config.json").read_text())


@pytest.fixture
def site():
    rows = {
        "1": {"user": "서민하", "memo": "점심", "amount": 11500, "purpose": "", "vat": "1045"},
        "2": {"user": "서민하", "memo": "점심 김기태 임희정 서민하", "amount": 37000, "purpose": "", "vat": "3363"},
        "3": {"user": "서민하", "memo": "출장 점심", "amount": 18000, "purpose": "출장비", "vat": "1636"},
        "4": {"user": "유병규", "memo": "야간 식대", "amount": 13000, "purpose": "", "vat": "1181"},
        "5": {"user": "서민하", "memo": "점심", "amount": 9000, "purpose": "", "vat": "818"},
    }
    state = {"rows": rows, "saves": [], "mode": "normal", "seen_pages": [], "duplicate": False,
             "date_ties": False, "ignore_sort": False, "next_query": {}}
    names = ["서민하", "김기태", "임희정", "유병규", "강두원", "이혜영"]
    esc = lambda v: html.escape(str(v), quote=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, content_type="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            header = "<!doctype html><meta charset='utf-8'><div>주식회사 모인</div>"
            if u.path == "/receipts/main":
                page = int(q.get("page", ["1"])[0])
                state["seen_pages"].append(page)
                ordered = list(rows)
                if q.get("search_order_by") == ["r_idx"] and not state["ignore_sort"]:
                    ordered.sort(key=int, reverse=q.get("search_order") == ["desc"])
                ids = ordered[(page - 1) * 2:page * 2]
                # Date ties can cross the LIMIT/OFFSET boundary: one receipt
                # repeats while another is omitted, even on a stable dataset.
                if state["date_ties"] and q.get("search_order_by") == ["approval_time"] and page == 2:
                    ids = ["1", "4"]
                if state["duplicate"] and page == 2:
                    ids = [ordered[0], ordered[3]]
                select = '<select id="search_u_idx"><option value="">전체</option>' + ''.join(
                    f'<option value="{i}">{n}</option>' for i, n in enumerate(names, 1)) + '</select>'
                tbody = []
                for rid in ids:
                    r = rows[rid]
                    columns = ["", "09/21", r["user"], f'<a href="/receipts/form?r_idx={rid}">테스트 식당 {rid}</a>',
                               f'{r["amount"]:,}', r["purpose"], "개인카드", "지급대기", "", "미신고", r["memo"]]
                    tbody.append("<tr>" + ''.join("<td>" + (str(c) if i == 3 else esc(c)) + "</td>" for i, c in enumerate(columns)) + "</tr>")
                table = '<table id="receipt_table"><thead><tr>' + ''.join('<th>' + h + '</th>' for h in HEADERS) + '</tr></thead><tbody>' + ''.join(tbody) + '</tbody></table>'
                more = ""
                if page * 2 < len(rows):
                    q["page"] = [str(page + 1)]
                    q.update(state["next_query"])
                    more = '<a href="/receipts/main?' + esc(urlencode(q, doseq=True)) + '">→</a>'
                self.send(header + select + table + more)
            elif u.path == "/receipts/form":
                rid = q["r_idx"][0]
                r = rows[rid]
                fields = {"client_name": "테스트 식당 " + rid, "approval_time": "2026-09-21 11:52:00",
                          "total_amt": r["amount"], "vat_amt": r["vat"], "purpose": r["purpose"],
                          "notes": r["memo"], "is_paid": "N", "is_dup": "N", "r_type": "R"}
                form = ''.join(f'<input id="{key}" value="{esc(value)}">' for key, value in fields.items())
                script = '''<button id="select2-search_purpose-container" onclick="document.getElementById('food').hidden=false">목적</button>
                <div role="option" id="food" hidden onclick="document.getElementById('purpose').value='식비';this.hidden=true">식비</div>
                <button id="btn_save">저장</button><script>
                document.getElementById('btn_save').onclick = async () => {
                  const fields = Object.fromEntries(Array.from(document.querySelectorAll('input')).map(e=>[e.id,e.value]));
                  const response = await fetch('/save?id=RID', {method:'POST',body:JSON.stringify(fields)});
                  const result = await response.json();
                  if(result.mode==='reject'){alert('저장 실패');return;}
                  if(result.mode==='silent'){return;}
                  alert('수정 되었습니다.');location.href='/receipts/main';
                };</script>'''.replace("RID", rid)
                self.send(header + form + script)
            else:
                self.send("")

        def do_POST(self):
            rid = parse_qs(urlparse(self.path).query)["id"][0]
            fields = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["saves"].append({"id": rid, "fields": fields})
            if state["mode"] != "reject":
                rows[rid].update(amount=int(fields["total_amt"]), purpose=fields["purpose"], memo=fields["notes"], vat=fields["vat_amt"])
                if state["mode"] == "vat_zero":
                    rows[rid]["vat"] = "0"
                elif state["mode"] == "vat_invalid":
                    rows[rid]["vat"] = "999999"
                elif state["mode"] == "memo_changed":
                    rows[rid]["memo"] = "unexpected change"
            self.send(json.dumps({"mode": state["mode"]}), "application/json")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        options = {"headless": True}
        if os.environ.get("JOBIS_TEST_BROWSER") == "chrome":
            options["channel"] = "chrome"
        b = p.chromium.launch(**options)
        yield b
        b.close()


@pytest.fixture
def ui(browser, site):
    context = browser.new_context()
    # Block non-local origins so fixture tests can never touch real receipts.
    context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).hostname == "127.0.0.1" else route.abort())
    app = JobisUI(context, "주식회사 모인", base_url=site["url"], timeout=1500)
    yield app
    context.close()


def test_complete_workflow(ui, site, config, tmp_path):
    original = copy.deepcopy(site["rows"])
    receipts, employees = ui.scan("2026-09")
    assert len(receipts) == 5
    assert site["seen_pages"] == [1, 2, 3, 1, 2, 3]
    plan = build_plan(receipts, employees, config, "2026-09")
    counts = preview(tmp_path, plan)
    assert counts == {"change": 4, "excluded": 1}
    assert (tmp_path / "preview.html").is_file()
    assert (tmp_path / "preview.csv").read_bytes().startswith(b'\xef\xbb\xbf')
    result = apply_plan(ui, config, plan, tmp_path)
    assert result["success"] and len(site["saves"]) == 4
    assert {rid: r["amount"] for rid, r in site["rows"].items()} == {"1": 10000, "2": 30000, "3": 18000, "4": 12000, "5": 9000}
    assert site["rows"]["3"] == original["3"]
    for rid, row in site["rows"].items():
        assert (row["vat"], row["memo"], row["user"]) == (original[rid]["vat"], original[rid]["memo"], original[rid]["user"])
    again = apply_plan(ui, config, plan, tmp_path)
    assert len(site["saves"]) == 4
    assert all(item["status"] == "already_correct" for item in again["items"])
    assert verify_plan(ui, config, plan, tmp_path)["success"]
    logs = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert sum(e["event"] == "submission_started" for e in logs) == 4
    assert sum(e["event"] == "verified" for e in logs) == 4


def make(ui, config):
    rows, employees = ui.scan("2026-09")
    return build_plan(rows, employees, config, "2026-09")


@pytest.mark.parametrize("mutation", ["memo", "amount", "target", "config", "missing", "user"])
def test_preflight_rejects_changes_before_any_save(ui, site, config, tmp_path, mutation):
    plan = make(ui, config)
    if mutation == "memo": site["rows"]["5"]["memo"] = "회식"
    if mutation == "amount": site["rows"]["5"]["amount"] = 8500
    if mutation == "target": plan["entries"][0]["decision"]["target"] = 1
    if mutation == "config": config["lunch_limit"] = 9000
    if mutation == "missing": del site["rows"]["5"]
    if mutation == "user": site["rows"]["5"]["user"] = "강두원"
    with pytest.raises((RuntimeError, ValueError)):
        apply_plan(ui, config, plan, tmp_path)
    assert not site["saves"]


def test_apply_only_and_verify_detect_omissions(ui, site, config, tmp_path):
    plan = make(ui, config)
    apply_plan(ui, config, plan, tmp_path, only={"1"})
    assert [s["id"] for s in site["saves"]] == ["1"]
    result = verify_plan(ui, config, plan, tmp_path)
    assert not result["success"]
    assert set(result["remaining_changes"]) == {"2", "4", "5"}


def test_failed_save_stops_without_retry(ui, site, config, tmp_path):
    plan = make(ui, config)
    site["mode"] = "reject"
    with pytest.raises(RuntimeError, match="저장 결과 불일치"):
        apply_plan(ui, config, plan, tmp_path)
    assert [s["id"] for s in site["saves"]] == ["5"]
    assert site["rows"]["5"]["purpose"] == ""


def test_saved_without_navigation_is_verified_without_second_save(ui, site, config, tmp_path):
    plan = make(ui, config)
    site["mode"] = "silent"
    result = apply_plan(ui, config, plan, tmp_path, only={"1"})
    assert result["success"] and len(site["saves"]) == 1


def test_direct_detail_change_blocks_save(ui, site, config):
    ready = preflight(ui, config, make(ui, config))
    site["rows"]["1"]["memo"] = "회식"
    receipt, decision = next((r, d) for r, d in ready if r.id == "1")
    with pytest.raises(RuntimeError, match="목록과 상세내역"):
        ui.apply_one(receipt, decision.target, lambda *a: None)
    assert not site["saves"]


def test_site_changed_vat_is_reported_as_saved(ui, site, config, tmp_path):
    plan = make(ui, config)
    site["mode"] = "vat_zero"
    result = apply_plan(ui, config, plan, tmp_path, only={"1"})
    assert result["success"]
    assert result["items"][0]["status"] == "updated_with_vat_change"
    assert len(site["saves"]) == 1
    assert site["saves"][0]["fields"]["vat_amt"] == "1045"
    assert site["rows"]["1"]["vat"] == "0"
    logs = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    vat_event = next(e for e in logs if e["event"] == "site_changed_vat")
    assert (vat_event["before_vat"], vat_event["after_vat"]) == ("1045", "0")


@pytest.mark.parametrize("mode,message", [("vat_invalid", "부가세 값 확인 필요"), ("memo_changed", "다른 필드가 변경됨")])
def test_other_save_changes_still_stop_and_are_logged(ui, site, config, tmp_path, mode, message):
    plan = make(ui, config)
    site["mode"] = mode
    with pytest.raises(RuntimeError, match=message):
        apply_plan(ui, config, plan, tmp_path)
    assert len(site["saves"]) == 1
    logs = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(e["event"] == "save_result_observed" for e in logs)
    assert not any(e["event"] == "verified" for e in logs)


def test_duplicate_pages_abort(ui, site):
    site["duplicate"] = True
    with pytest.raises(RuntimeError, match="같은 영수증"):
        ui.scan("2026-09")


def test_date_ties_do_not_repeat_or_omit_receipts(ui, site):
    site["date_ties"] = True
    receipts, _ = ui.scan("2026-09")
    assert [r.id for r in receipts] == ["5", "4", "3", "2", "1"]
    assert site["seen_pages"] == [1, 2, 3, 1, 2, 3]
    assert not site["saves"]


def test_ignored_unique_sort_aborts(ui, site):
    site["ignore_sort"] = True
    with pytest.raises(RuntimeError, match="고유번호순 정렬"):
        ui.scan("2026-09")
    assert not site["saves"]


@pytest.mark.parametrize("next_query,message", [
    ({"search_order_by": ["approval_time"]}, "조회 조건"),
    ({"search_order": ["asc"]}, "조회 조건"),
    ({"page": ["3"]}, "연속"),
])
def test_next_page_cannot_change_sort_or_skip_a_page(ui, site, next_query, message):
    site["next_query"] = next_query
    with pytest.raises(RuntimeError, match=message):
        ui.scan("2026-09")
    assert site["seen_pages"] == [1]
    assert not site["saves"]


def test_preview_filters_and_escapes_content(browser, ui, config, tmp_path):
    plan = make(ui, config)
    plan["entries"][0]["receipt"]["merchant"] = '<script>window.bad=1</script>'
    plan["entries"][0]["receipt"]["memo"] = '=HYPERLINK("x")'
    preview(tmp_path, plan)
    page = browser.new_page()
    try:
        page.goto((tmp_path / "preview.html").as_uri())
        assert page.locator('#s option').count() == 5
        page.locator('#s').select_option('change')
        assert page.locator('tbody tr:visible').count() == 4
        page.locator('#q').fill('37000')
        assert page.locator('tbody tr:visible').count() == 1
        page.locator('#s').select_option('excluded')
        assert page.locator('tbody tr:visible').count() == 0
        page.locator('#q').fill('')
        assert page.locator('tbody tr:visible').count() == 1
        assert page.evaluate('window.bad === undefined')
        assert "'=HYPERLINK" in (tmp_path / 'preview.csv').read_text(encoding='utf-8-sig')
    finally:
        page.close()


def test_preview_toggles_persist_and_control_real_apply_flow(ui, site, config, tmp_path):
    plan = make(ui, config)
    original = copy.deepcopy(site["rows"])
    path = tmp_path / "plan.json"
    write_json(path, plan)
    server = PreviewServer(path, config).start()
    page = ui.context.new_page()
    try:
        page.goto(server.url)
        for rid in ("2", "4", "5"):
            page.get_by_role("switch", name=rid + " 적용", exact=True).uncheck()
        page.get_by_role("switch", name="3 적용", exact=True).check()
        expect(page.get_by_role("button", name="선택 저장", exact=True)).to_be_disabled()
        page.get_by_role("spinbutton", name="3 인원", exact=True).fill("2")
        page.get_by_role("spinbutton", name="3 인원", exact=True).blur()
        expect(page.locator('tr[data-id="3"] [data-field=target]')).to_have_text("18000")
        page.get_by_role("spinbutton", name="3 인원", exact=True).fill("1")
        page.get_by_role("spinbutton", name="3 인원", exact=True).blur()
        expect(page.locator('tr[data-id="3"] [data-field=target]')).to_have_text("10000")
        page.get_by_role("button", name="선택 저장", exact=True).click()
        expect(page.locator("#message")).to_contain_text("2건 저장 완료")
        selected = load_selection(tmp_path, plan, config)
        assert selected["selected_ids"] == ["3", "1"]
        assert selected["overrides"] == {"3": {"kind": "lunch", "count": 1}}
        page.reload()
        expect(page.get_by_role("switch", name="3 적용", exact=True)).to_be_checked()
        expect(page.get_by_role("switch", name="2 적용", exact=True)).not_to_be_checked()
        assert not site["saves"]
    finally:
        page.close()
        server.close()
    result = apply_plan(ui, config, plan, tmp_path)
    assert result["success"] and [s["id"] for s in site["saves"]] == ["3", "1"]
    assert (site["rows"]["3"]["amount"], site["rows"]["3"]["purpose"]) == (10000, "식비")
    assert all(site["rows"][rid] == original[rid] for rid in ("2", "4", "5"))
    verified = verify_plan(ui, config, plan, tmp_path)
    assert verified["success"] and not verified["remaining_changes"]
    assert set(verified["skipped_ids"]) == {"2", "4", "5"}
    assert result["selection"] == selected


def test_all_deselected_means_no_saves(ui, site, config, tmp_path):
    plan = make(ui, config)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    server = PreviewServer(path, config).start()
    page = ui.context.new_page()
    try:
        page.goto(server.url)
        page.get_by_role("button", name="전체 선택 해제", exact=True).click()
        page.get_by_role("button", name="선택 저장", exact=True).click()
        expect(page.locator("#message")).to_contain_text("0건 저장 완료")
        page.reload()
        assert page.locator('.apply-toggle:checked').count() == 0
    finally:
        page.close()
        server.close()
    pages_before = list(site["seen_pages"])
    result = apply_plan(ui, config, plan, tmp_path)
    assert result["success"] and result["items"] == []
    assert not site["saves"] and site["seen_pages"] == pages_before
    assert verify_plan(ui, config, plan, tmp_path)["success"]


def test_manual_include_still_rechecks_receipt_before_any_save(ui, site, config, tmp_path):
    plan = make(ui, config)
    selected = {**default_selection(plan), "selected_ids": ["3"],
                "overrides": {"3": {"kind": "lunch", "count": 1}}}
    write_json(tmp_path / "selection.json", selected)
    site["rows"]["3"]["memo"] = "출장 점심 / 다른 내용"
    with pytest.raises(RuntimeError, match="내역이 바뀌"):
        apply_plan(ui, config, plan, tmp_path)
    assert not site["saves"]


def test_preview_rejects_foreign_requests_stale_tabs_and_changed_plan(ui, config, tmp_path):
    plan = make(ui, config)
    path = tmp_path / "plan.json"
    write_json(path, plan)
    server = PreviewServer(path, config).start()
    request = ui.context.request
    endpoint = server.url + "selection"
    initial = default_selection(plan)
    payload = {"selection": {**initial, "selected_ids": []}, "revision": digest(initial)}
    try:
        assert request.post(endpoint, data=payload).status == 403
        headers = {"Origin": server.origin}
        assert request.post(endpoint, data=payload, headers=headers).status == 200
        assert request.post(endpoint, data=payload, headers=headers).status == 409
        assert not load_selection(tmp_path, plan, config)["selected_ids"]
        plan["month"] = "2026-08"
        write_json(path, plan)
        assert request.post(endpoint, data=payload, headers=headers).status == 400
    finally:
        server.close()


@pytest.mark.parametrize("url", ["https://evil.example/receipts/form?r_idx=1", "https://service.jobisbiz.co/receipts/delete?r_idx=1", "https://service.jobisbiz.co/receipts/form?r_idx=1&r_idx=2"])
def test_url_boundary(url):
    with pytest.raises(ValueError):
        receipt_id(url)
