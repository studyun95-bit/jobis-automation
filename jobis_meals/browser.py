from __future__ import annotations

import calendar
import re
from dataclasses import asdict
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .rules import Receipt, clean


BASE_URL = "https://service.jobisbiz.co"
HEADERS = ["", "일자", "사용자", "업체명", "금액", "목적", "구분", "지급", "중복", "세무신고", "메모"]


def month_range(month: str):
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("조회 월은 YYYY-MM 형식이어야 합니다.")
    year, number = map(int, month.split("-"))
    start = date(year, number, 1)
    return start.isoformat(), date(year, number, calendar.monthrange(year, number)[1]).isoformat()


def receipt_id(url: str, base_url: str = BASE_URL):
    u, base = urlparse(url), urlparse(base_url)
    ids = parse_qs(u.query).get("r_idx", [])
    if (u.scheme, u.netloc, u.path) != (base.scheme, base.netloc, "/receipts/form") or len(ids) != 1 or not ids[0].isdigit():
        raise ValueError("유효한 자비스 상세 영수증 URL이 아닙니다.")
    return ids[0]


class JobisUI:
    def __init__(self, context, company: str, base_url: str = BASE_URL, timeout=15000):
        self.context = context
        self.company = company
        self.base_url = base_url.rstrip("/")
        self.page = context.new_page()
        self.page.set_default_timeout(timeout)
        self.page.set_default_navigation_timeout(timeout * 2)
        self.timeout = timeout

    def check_company(self, page=None):
        page = page or self.page
        page.get_by_text(self.company, exact=True).first.wait_for(state="visible")

    def open_list(self, month: str):
        start, end = month_range(month)
        query = urlencode({"search_start_date": start, "search_end_date": end,
                           "per_page": "100", "search_order_by": "approval_time", "search_order": "desc"})
        self.page.goto(self.base_url + "/receipts/main?" + query, wait_until="domcontentloaded")
        self.check_company()
        self.page.locator("#receipt_table").wait_for(state="visible")

    def roster(self):
        rows = self.page.locator("#search_u_idx option").evaluate_all(
            "es => es.map(e => ({id:e.value, name:e.textContent.trim()}))")
        result = [r for r in rows if r["id"] and not r["name"].startswith("(삭제)")]
        if not result:
            raise RuntimeError("직원 목록을 읽지 못했습니다. 로그인/회사/화면 구조를 확인하세요.")
        return list({r["id"]: r for r in result}.values())

    def scan_once(self, month: str):
        self.open_list(month)
        employees = self.roster()
        result = {}
        visited = set()
        start, end = month_range(month)
        for _ in range(1000):
            url = self.page.url
            if url in visited:
                raise RuntimeError("같은 목록 페이지가 반복되었습니다.")
            visited.add(url)
            self.page.locator("#receipt_table").wait_for(state="visible")
            self.page.locator('#receipt_table tbody a[href*="/receipts/form?"]').first.wait_for(state="attached")
            headers = self.page.locator("#receipt_table th").all_inner_texts()
            if [clean(h) for h in headers] != HEADERS:
                raise RuntimeError("자비스 목록 컬럼이 변경되었습니다. 저장 없이 중단합니다.")
            rows = self.page.locator("#receipt_table tbody tr").evaluate_all("""es => es.map(e => ({
                cells: Array.from(e.querySelectorAll('td')).map(c=>c.innerText.trim()),
                url: e.querySelector('a[href*="/receipts/form?"]')?.href
            })).filter(r=>r.url)""")
            for item in rows:
                c = item["cells"]
                if len(c) != len(HEADERS):
                    raise RuntimeError("영수증 행의 컬럼 수가 다릅니다.")
                rid = receipt_id(item["url"], self.base_url)
                day = date.fromisoformat(month[:4] + "-" + c[1].replace("/", "-"))
                if not start <= day.isoformat() <= end:
                    raise RuntimeError("조회 기간 밖의 영수증이 표시되었습니다.")
                if not re.fullmatch(r"-?\d[\d,]*", c[4]):
                    raise RuntimeError("금액을 읽을 수 없습니다: " + c[4])
                r = Receipt(rid, item["url"], day.isoformat(), clean(c[2]), clean(c[3]),
                            int(c[4].replace(",", "")), clean(c[5]), clean(c[7]), clean(c[10]),
                            clean(c[6]), clean(c[8]), clean(c[9]))
                if rid in result:
                    raise RuntimeError("페이지 사이에 같은 영수증이 반복됩니다. 새로 검사하세요.")
                result[rid] = r
            print(f"  {len(visited)}페이지 확인 · {len(result)}건 읽음", flush=True)
            next_link = self.page.get_by_role("link", name="→", exact=True)
            if next_link.count() == 0:
                return list(result.values()), employees
            href = urljoin(self.page.url, next_link.get_attribute("href") or "")
            dest = urlparse(href)
            current = parse_qs(urlparse(url).query)
            query = parse_qs(dest.query)
            if (dest.scheme, dest.netloc, dest.path) != (*urlparse(self.base_url)[:2], "/receipts/main"):
                raise RuntimeError("다음 페이지 링크의 목적지가 다릅니다.")
            for key, expected in (("search_start_date", start), ("search_end_date", end), ("per_page", "100")):
                if query.get(key) != [expected]:
                    raise RuntimeError("다음 페이지에서 조회 조건이 달라졌습니다.")
            if int(query.get("page", ["1"])[0]) <= int(current.get("page", ["1"])[0]):
                raise RuntimeError("다음 페이지 번호가 증가하지 않습니다.")
            # 관찰한 실제 링크로 이동하고 table까지 기다려 빈 페이지 수집을 막는다.
            self.page.goto(href, wait_until="domcontentloaded")
            self.check_company()
        raise RuntimeError("페이지 수 제한을 넘었습니다.")

    def scan(self, month: str, attempts=3):
        """연속 두 번 동일한 목록을 얻어 페이지 이동 중 신규 등록 누락을 탐지한다."""
        previous = None
        for attempt in range(attempts):
            print(f"목록 확인 {attempt + 1}회차", flush=True)
            receipts, employees = self.scan_once(month)
            signature = ({r.id: asdict(r) for r in receipts}, sorted((r["id"], r["name"]) for r in employees))
            if signature == previous:
                return receipts, employees
            previous = signature
        raise RuntimeError("조회 중 목록이 계속 바뀌었습니다. 신규 등록이 멈춘 뒤 다시 검사하세요.")

    def detail(self, url: str):
        receipt_id(url, self.base_url)
        self.page.goto(url, wait_until="domcontentloaded")
        self.check_company()
        self.page.locator("#total_amt").wait_for(state="visible")
        return self.read_detail(self.page)

    @staticmethod
    def read_detail(page):
        fields = ("client_name", "approval_time", "total_amt", "vat_amt", "purpose", "notes", "is_paid", "is_dup", "r_type")
        return {field: page.locator("#" + field).input_value() for field in fields}

    @staticmethod
    def assert_baseline(r: Receipt, actual: dict, target: int):
        expected_paid = {"지급대기": "N", "지급완료": "Y", "지급거절": "R"}.get(r.status)
        expected_type = {"개인카드": "R", "법인카드": "T", "기타영수증": "E"}.get(r.evidence)
        expected_dup = "P" if "의심" in r.duplicate else "Y" if "확정" in r.duplicate else "N"
        checks = [clean(actual["notes"]) == clean(r.memo), clean(actual["client_name"]) == r.merchant,
                  actual["approval_time"][:10] == r.date, actual["is_paid"] == expected_paid,
                  actual["is_dup"] == expected_dup]
        if expected_type:
            checks.append(actual["r_type"] == expected_type)
        if not all(checks):
            raise RuntimeError(f"{r.id}: 목록과 상세내역이 다릅니다. 새 검사 필요")
        already = int(actual["total_amt"]) == target and actual["purpose"] == "식비"
        if not already and (int(actual["total_amt"]) != r.amount or actual["purpose"] != r.purpose):
            raise RuntimeError(f"{r.id}: 미리보기 이후 금액 또는 목적이 변경됨")
        return already

    def apply_one(self, r: Receipt, target: int, journal):
        before = self.detail(r.url)
        if self.assert_baseline(r, before, target):
            return "already_correct"
        if target > r.amount:
            raise RuntimeError("기존 결제 금액보다 올릴 수 없습니다.")
        if int(before["total_amt"]) != target:
            self.page.locator("#total_amt").fill(str(target))
        if before["purpose"] != "식비":
            self.page.locator("#select2-search_purpose-container").click()
            self.page.get_by_role("option", name="식비", exact=True).click()
        staged = self.read_detail(self.page)
        for field, value in before.items():
            if field not in ("total_amt", "purpose") and staged[field] != value:
                raise RuntimeError("저장 전 다른 필드가 바뀌었습니다: " + field)
        if staged["total_amt"] != str(target) or staged["purpose"] != "식비":
            raise RuntimeError("금액 또는 식비 입력 결과가 다릅니다.")
        messages = []

        def on_dialog(dialog):
            messages.append(dialog.message)
            if dialog.type == "alert" and "수정 되었습니다" in dialog.message:
                dialog.accept()
            else:
                dialog.dismiss()

        self.page.on("dialog", on_dialog)
        journal("submission_started", {"id": r.id, "before": before, "target": target})
        try:
            try:
                self.page.locator("#btn_save").click()
                self.page.wait_for_url(re.compile(re.escape(self.base_url) + r"/receipts/main(?:\?|$)"), timeout=self.timeout)
            except PlaywrightTimeout:
                # 응답 유실 후에도 저장 버튼을 다시 누르지 않고 서버에 반영된 폼을 확인한다.
                pass
            verify = self.context.new_page()
            verify.set_default_timeout(self.timeout)
            try:
                verify.goto(r.url, wait_until="domcontentloaded")
                self.check_company(verify)
                verify.locator("#total_amt").wait_for(state="visible")
                after = self.read_detail(verify)
            finally:
                verify.close()
            journal("save_result_observed", {"id": r.id, "after": after, "dialogs": messages})
            if int(after["total_amt"]) != target or after["purpose"] != "식비":
                raise RuntimeError("저장 결과 불일치; 자동 재저장하지 않음. 알림: " + " / ".join(messages))
            for field in before:
                if field not in ("total_amt", "purpose", "vat_amt") and before[field] != after[field]:
                    raise RuntimeError("저장 후 다른 필드가 변경됨: " + field)
            # Jobis can change VAT on the server when saving amount/purpose.
            # We do not edit VAT; retain and explicitly report the site's value.
            vat_changed = before["vat_amt"] != after["vat_amt"]
            if vat_changed:
                if not re.fullmatch(r"\d+", after["vat_amt"]) or not 0 <= int(after["vat_amt"]) <= target:
                    raise RuntimeError("저장 후 부가세 값 확인 필요: " + after["vat_amt"])
                journal("site_changed_vat", {"id": r.id, "before_vat": before["vat_amt"],
                                             "after_vat": after["vat_amt"]})
                print(f"    자비스 저장 후 부가세 변경: {int(before['vat_amt']):,} → {int(after['vat_amt']):,}원 (이력 기록)", flush=True)
            journal("verified", {"id": r.id, "after": after, "dialogs": messages})
            return "updated_with_vat_change" if vat_changed else "updated"
        finally:
            self.page.remove_listener("dialog", on_dialog)
