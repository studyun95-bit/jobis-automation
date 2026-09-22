import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from jobis_meals.rules import Receipt
from jobis_meals.selection import default_selection, manual_decision, validate_selection
from jobis_meals.workflow import build_plan


@pytest.fixture
def config():
    return json.loads((Path(__file__).parents[1] / "config.json").read_text())


@pytest.fixture
def receipt():
    return asdict(Receipt("3", "https://service.jobisbiz.co/receipts/form?r_idx=3", "2026-09-21",
                          "서민하", "식당", 27000, "출장비", "지급대기", "출장 점심"))


def test_manual_lunch_uses_people_limit_and_never_raises_amount(receipt, config):
    assert manual_decision(receipt, {"kind": "lunch", "count": 2}, config).target == 20000
    assert manual_decision(receipt, {"kind": "lunch", "count": 3}, config).target == 27000
    receipt["amount"] = 9000
    assert manual_decision(receipt, {"kind": "lunch", "count": 1}, config).target == 9000


def test_manual_night_requires_designated_user_and_one_person(receipt, config):
    with pytest.raises(ValueError, match="야간식대"):
        manual_decision(receipt, {"kind": "night", "count": 1}, config)
    receipt["user"] = "유병규"
    assert manual_decision(receipt, {"kind": "night", "count": 1}, config).target == 12000
    with pytest.raises(ValueError, match="야간식대"):
        manual_decision(receipt, {"kind": "night", "count": 2}, config)


@pytest.mark.parametrize("count", [0, -1, 101, 1.5, True, None, "2"])
def test_manual_count_must_be_a_valid_integer(receipt, config, count):
    with pytest.raises(ValueError):
        manual_decision(receipt, {"kind": "lunch", "count": count}, config)


@pytest.mark.parametrize("patch", [{"status": "지급완료"}, {"status": "지급거절"}, {"amount": 0}, {"amount": -10000}])
def test_manual_selection_preserves_payment_eligibility(receipt, config, patch):
    receipt.update(patch)
    with pytest.raises(ValueError, match="지급상태 또는 금액"):
        manual_decision(receipt, {"kind": "lunch", "count": 1}, config)


@pytest.mark.parametrize("mutation", ["wrong_plan", "unknown", "duplicate", "missing_count", "extra_target", "unused_override", "review"])
def test_selection_cannot_bypass_plan_or_manual_validation(receipt, config, mutation):
    plan = build_plan([Receipt(**receipt)], [{"id": "1", "name": "서민하"}], config, "2026-09")
    selection = {**default_selection(plan), "selected_ids": ["3"],
                 "overrides": {"3": {"kind": "lunch", "count": 2}}}
    assert validate_selection(plan, selection, config) == selection
    bad = copy.deepcopy(selection)
    if mutation == "wrong_plan": bad["plan_hash"] = "old-plan"
    elif mutation == "unknown": bad["selected_ids"] = ["999"]
    elif mutation == "duplicate": bad["selected_ids"] *= 2
    elif mutation == "missing_count": del bad["overrides"]["3"]["count"]
    elif mutation == "extra_target": bad["overrides"]["3"]["target"] = 27000
    elif mutation == "unused_override": bad["selected_ids"] = []
    elif mutation == "review":
        plan["entries"][0]["decision"]["action"] = "review"
        bad["plan_hash"] = default_selection(plan)["plan_hash"]
    with pytest.raises(ValueError):
        validate_selection(plan, bad, config)
