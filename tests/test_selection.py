import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from jobis_meals.rules import Receipt
from jobis_meals.selection import default_selection, manual_decision, meal_types, selection_summary, validate_selection
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


@pytest.mark.parametrize("kind,label,limit", [
    ("lunch", "점심", 10000), ("overtime", "야근", 10000),
    ("night", "야간", 12000), ("moin_lunch", "모인런치", 15000),
])
def test_explicit_meal_choices_use_per_person_limits(receipt, config, kind, label, limit):
    receipt["amount"] = 47000
    decision = manual_decision(receipt, {"kind": kind, "count": 2}, config)
    assert decision.target == limit * 2
    assert label + " 2명" in decision.reason
    receipt["amount"] = 7000
    assert manual_decision(receipt, {"kind": kind, "count": 1}, config).target == 7000


def test_new_meal_defaults_keep_existing_config_hash_compatible(config):
    from jobis_meals.reports import digest
    before = digest(config)
    assert meal_types(config)["overtime"]["limit"] == 10000
    assert meal_types(config)["moin_lunch"]["limit"] == 15000
    assert digest(config) == before
    assert "overtime_limit" not in config and "moin_lunch_limit" not in config


@pytest.mark.parametrize("key", ["overtime_limit", "moin_lunch_limit"])
@pytest.mark.parametrize("value", [0, -1, True, "15000"])
def test_invalid_new_limits_are_rejected(config, key, value):
    config[key] = value
    with pytest.raises(ValueError, match=key):
        meal_types(config)


def test_change_receipt_can_override_meal_kind_and_count(receipt, config):
    receipt["memo"] = "점심"
    plan = build_plan([Receipt(**receipt)], [{"id": "1", "name": "서민하"}], config, "2026-09")
    selection = {**default_selection(plan), "overrides": {"3": {"kind": "moin_lunch", "count": 1}}}
    assert validate_selection(plan, selection, config) == selection
    assert selection_summary(plan, selection) == {"selected": 1, "manual": 0, "adjusted": 1, "skipped": 0}


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
