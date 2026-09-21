from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .browser import month_range, receipt_id
from .reports import digest, journal_writer, preview, write_json
from .rules import NameCounter, Receipt, decide


def now():
    return datetime.now().astimezone().isoformat()


def make_counter(employees, config):
    extra = config.get("extra_employees", [])
    if any(not str(e["id"]).startswith("extra:") for e in extra):
        raise ValueError("추가 직원의 ID는 extra:로 시작해야 합니다.")
    all_employees = employees + extra
    ids = [str(e["id"]) for e in all_employees]
    if len(set(ids)) != len(ids):
        raise ValueError("직원 ID가 중복되었습니다.")
    return NameCounter(all_employees, config.get("aliases", {}))


def build_plan(receipts, employees, config, month):
    counter = make_counter(employees, config)
    return {"schema": 1, "created_at": now(), "month": month, "company": config["company"],
            "config_hash": digest(config), "employees": employees,
            "entries": [{"receipt": asdict(r), "decision": decide(r, config, counter).to_dict()}
                        for r in receipts]}


def scan_to_folder(ui, config, month, folder):
    receipts, employees = ui.scan(month)
    plan = build_plan(receipts, employees, config, month)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    write_json(folder / "plan.json", plan)
    counts = preview(folder, plan)
    return plan, counts


def validate_plan(plan, config, base_url):
    if plan.get("schema") != 1:
        raise ValueError("지원하지 않는 계획 파일입니다.")
    if plan["company"] != config["company"] or plan["config_hash"] != digest(config):
        raise ValueError("설정이 검사 당시와 다릅니다. 새로 검사하세요.")
    start, end = month_range(plan["month"])
    seen = set()
    for entry in plan["entries"]:
        r = Receipt(**entry["receipt"])
        if r.id in seen or receipt_id(r.url, base_url) != r.id or not start <= r.date <= end:
            raise ValueError("계획 파일의 영수증 ID·URL·기간이 잘못되었습니다.")
        if not isinstance(r.amount, int) or isinstance(r.amount, bool):
            raise ValueError("계획 파일의 금액이 정수가 아닙니다.")
        seen.add(r.id)


def same_unchanged_fields(before: Receipt, current: Receipt):
    return all(getattr(before, k) == getattr(current, k) for k in asdict(before)
               if k not in ("url", "amount", "purpose"))


def preflight(ui, config, plan, only=None):
    validate_plan(plan, config, ui.base_url)
    current, employees = ui.scan(plan["month"])
    current_by_id = {r.id: r for r in current}
    counter = make_counter(employees, config)
    changes = {e["receipt"]["id"]: e for e in plan["entries"] if e["decision"]["action"] == "change"}
    if only is not None:
        if not only or set(only) - set(changes):
            raise ValueError("--only에는 수정 예정인 영수증 ID만 지정하세요.")
        changes = {key: value for key, value in changes.items() if key in only}
    ready = []
    for entry in changes.values():
        r = Receipt(**entry["receipt"])
        d = decide(r, config, counter)
        if d.action != "change" or d.to_dict() != entry["decision"]:
            raise RuntimeError(f"{r.id}: 이름 판정 또는 처리 규칙이 바뀌었습니다. 새로 검사하세요.")
        actual = current_by_id.get(r.id)
        if actual is None or not same_unchanged_fields(r, actual):
            raise RuntimeError(f"{r.id}: 검사 후 내역이 바뀌거나 사라졌습니다. 새로 검사하세요.")
        old = (actual.amount, actual.purpose) == (r.amount, r.purpose)
        done = (actual.amount, actual.purpose) == (d.target, "식비")
        if not old and not done:
            raise RuntimeError(f"{r.id}: 검사 후 금액 또는 목적이 바뀌었습니다. 새로 검사하세요.")
        ready.append((r, d))
    return ready


def apply_plan(ui, config, plan, folder, only=None):
    folder = Path(folder)
    log = journal_writer(folder / "audit.jsonl")
    result = {"started_at": now(), "items": [], "success": False}
    result_path = folder / ("apply-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
    try:
        ready = preflight(ui, config, plan, only)
        log("preflight_passed", {"count": len(ready)})
        for r, d in ready:
            print(f"  {r.id} {r.user}: {r.amount:,} → {d.target:,}원 / 식비", flush=True)
            outcome = ui.apply_one(r, d.target, log)
            result["items"].append({"id": r.id, "status": outcome, "amount": d.target, "purpose": "식비"})
            write_json(result_path, result)
        result["success"] = True
        log("apply_completed", {"count": len(ready)})
    except Exception as exc:
        result["error"] = str(exc)
        log("stopped", {"error": str(exc)})
        raise
    finally:
        result["finished_at"] = now()
        write_json(result_path, result)
    return result


def verify_plan(ui, config, plan, folder):
    validate_plan(plan, config, ui.base_url)
    receipts, employees = ui.scan(plan["month"])
    current = {r.id: r for r in receipts}
    problems = []
    for entry in plan["entries"]:
        r, d = Receipt(**entry["receipt"]), entry["decision"]
        actual = current.get(r.id)
        if actual is None:
            problems.append({"id": r.id, "reason": "목록에서 사라짐"})
        elif not same_unchanged_fields(r, actual):
            problems.append({"id": r.id, "reason": "메모·사용자·날짜·지급상태 등 변경됨"})
        elif d["action"] in ("keep", "change") and (actual.amount, actual.purpose) != (d["target"], "식비"):
            problems.append({"id": r.id, "reason": "예정 금액/식비와 다름", "amount": actual.amount, "purpose": actual.purpose})
        elif d["action"] in ("excluded", "review") and (actual.amount, actual.purpose) != (r.amount, r.purpose):
            problems.append({"id": r.id, "reason": "제외/보류 항목의 금액 또는 목적 변경됨"})
    new_plan = build_plan(receipts, employees, config, plan["month"])
    remaining = [e["receipt"]["id"] for e in new_plan["entries"] if e["decision"]["action"] == "change"]
    result = {"checked_at": now(), "problems": problems, "remaining_changes": remaining,
              "counts": dict(Counter(e["decision"]["action"] for e in new_plan["entries"])),
              "success": not problems and not remaining}
    write_json(Path(folder) / ("verify-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json"), result)
    return result
