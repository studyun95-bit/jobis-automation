from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from .reports import digest
from .rules import Decision, Receipt


@contextmanager
def selection_lock(folder):
    import fcntl
    with (Path(folder) / "selection.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def default_selection(plan):
    return {"schema": 1, "plan_hash": digest(plan),
            "selected_ids": [e["receipt"]["id"] for e in plan["entries"]
                             if e["decision"]["action"] == "change"], "overrides": {}}


def can_include(receipt, config):
    return receipt["status"] in config["eligible_statuses"] and receipt["amount"] > 0


def manual_decision(receipt, choice, config):
    r = Receipt(**receipt)
    if not can_include(receipt, config):
        raise ValueError(f"{r.id}: 지급상태 또는 금액 때문에 적용할 수 없습니다.")
    if not isinstance(choice, dict) or set(choice) != {"kind", "count"}:
        raise ValueError(f"{r.id}: 식대 종류와 인원을 확인하세요.")
    kind, count = choice["kind"], choice["count"]
    if kind not in ("lunch", "night") or type(count) is not int or not 1 <= count <= 100:
        raise ValueError(f"{r.id}: 식대 종류와 인원(1~100명)을 확인하세요.")
    if kind == "night" and (r.user not in config["night_users"] or count != 1):
        raise ValueError(f"{r.id}: 지정된 야간식대는 해당 직원의 1명 식대만 가능합니다.")
    limit = config["night_limit"] if kind == "night" else config["lunch_limit"] * count
    target = min(r.amount, limit)
    action = "keep" if (r.amount, r.purpose) == (target, "식비") else "change"
    label = "야간" if kind == "night" else "점심"
    return Decision(action, kind, target, count, (), f"사용자가 {label} {count}명으로 확인 / 한도 {limit:,}원")


def validate_selection(plan, selection, config):
    if not isinstance(selection, dict) or selection.get("schema") != 1 or selection.get("plan_hash") != digest(plan):
        raise ValueError("선택 정보가 이 검사 결과와 다릅니다. 해당 미리보기를 다시 여세요.")
    ids, overrides = selection.get("selected_ids"), selection.get("overrides")
    if not isinstance(ids, list) or not all(isinstance(v, str) for v in ids) or len(ids) != len(set(ids)):
        raise ValueError("선택한 영수증 ID가 잘못되었습니다.")
    if not isinstance(overrides, dict):
        raise ValueError("제외 항목의 식대 종류와 인원을 확인하세요.")
    entries = {e["receipt"]["id"]: e for e in plan["entries"]}
    manual_ids = set()
    for rid in ids:
        entry = entries.get(rid)
        if entry is None or entry["decision"]["action"] not in ("change", "excluded"):
            raise ValueError(f"{rid}: 적용 대상으로 선택할 수 없는 항목입니다.")
        if entry["decision"]["action"] == "excluded":
            manual_decision(entry["receipt"], overrides.get(rid), config)
            manual_ids.add(rid)
    if set(overrides) != manual_ids:
        raise ValueError("선택한 제외 항목과 입력한 식대 정보가 다릅니다.")
    # Store selections in original receipt order, independent of click order.
    chosen = set(ids)
    return {"schema": 1, "plan_hash": digest(plan),
            "selected_ids": [rid for rid in entries if rid in chosen],
            "overrides": {rid: dict(overrides[rid]) for rid in entries if rid in manual_ids}}


def load_selection(folder, plan, config):
    path = Path(folder) / "selection.json"
    selection = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default_selection(plan)
    return validate_selection(plan, selection, config)


def selection_summary(plan, selection):
    chosen = set(selection["selected_ids"])
    changes = {e["receipt"]["id"] for e in plan["entries"] if e["decision"]["action"] == "change"}
    return {"selected": len(chosen), "manual": len(selection["overrides"]), "skipped": len(changes - chosen)}
