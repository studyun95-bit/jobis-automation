import json
from dataclasses import replace
from pathlib import Path

import pytest

from jobis_meals.rules import NameCounter, Receipt, decide


@pytest.fixture
def config():
    return json.loads((Path(__file__).parents[1] / "config.json").read_text())


@pytest.fixture
def employees():
    names = ["이혜영", "김기태", "임희정", "서민하", "정보람", "조은주", "고선우", "유병규", "강두원", "허선아", "전은정", "백지훈"]
    return [{"id": str(i), "name": n} for i, n in enumerate(names, 1)]


@pytest.fixture
def counter(config, employees):
    return NameCounter(employees, config["aliases"])


@pytest.mark.parametrize("memo,count", [
    ("점심", 1), ("", 1), ("09/21 점심 식대", 1),
    ("점심 김기태, 임희정", 2), ("점심 김기태임희정", 2),
    ("점심 김기태 / 기태 / 김기태님", 1), ("점심 기태 희정 민하", 3),
    ("점심 포함 4인", 4), ("본인 포함 4인 점심", 4),
    ("허선아 외2인(전은정,백지훈) 점심", 3), ("허선아 외 2인 점심", 3),
    ("김기태 포함 4인 점심", 4), ("총 3명 점심", 3),
    ("점심 2026.09.21 김기태 임희정", 2), ("점심 본인 김기태", 2),
    ("2026년9월21일 점심 김기태", 1), ("저녁 식대", 1),
])
def test_count(counter, memo, count):
    assert counter.count(memo, "이혜영").count == count


@pytest.mark.parametrize("memo", [
    "점심 미등록이름", "점심 김기태 임희정 포함 4인", "점심 김기태 임희정 3명",
    "점심 4인 3명", "점심 외 2인", "점심 0명", "점심 101명",
    "점심 김기태 또는 임희정", "점심 홍길동", "점심 김기태 3명", "점심 고객 2명",
])
def test_ambiguity_is_review(counter, memo):
    assert counter.count(memo, "이혜영").count is None


def test_duplicate_full_names_are_ambiguous(config, employees):
    ambiguous = NameCounter(employees + [{"id": "999", "name": "김기태"}], config["aliases"])
    assert ambiguous.count("점심 기태").count is None
    assert ambiguous.count("점심 김기태").count is None


def receipt(memo="점심", user="서민하", amount=11500, purpose="", **kwargs):
    return Receipt("1", "https://service.jobisbiz.co/receipts/form?r_idx=1", "2026-09-21", user,
                   "테스트 식당", amount, purpose, kwargs.pop("status", "지급대기"), memo, **kwargs)


@pytest.mark.parametrize("memo,user,amount,purpose,action,target,count", [
    ("점심", "서민하", 11500, "", "change", 10000, 1),
    ("점심", "서민하", 9000, "", "change", 9000, 1),
    ("점심", "서민하", 10000, "식비", "keep", 10000, 1),
    ("점심", "서민하", 9000, "식비", "keep", 9000, 1),
    ("김기태 임희정 점심", "서민하", 25000, "", "change", 20000, 2),
    ("김기태 임희정 점심", "서민하", 17000, "", "change", 17000, 2),
    ("야간 식대", "유병규", 13500, "", "change", 12000, 1),
    ("야근", "강두원", 9500, "", "change", 9500, 1),
    ("저녁 식대", "강두원", 12500, "", "change", 12000, 1),
    ("야근", "서민하", 15000, "", "excluded", None, None),
    ("09/21 이혜영 김기태 임희정", "이혜영", 33000, "", "change", 30000, 3),
    ("09/21 이혜영", "이혜영", 12000, "", "change", 10000, 1),
    ("09/21 김기태 임희정", "서민하", 30000, "", "excluded", None, None),
    ("야간 식대 강두원 유병규", "강두원", 25000, "", "review", None, 2),
    ("점심 야근", "강두원", 15000, "", "review", None, None),
    ("점심 김기태 홍길동", "서민하", 20000, "", "review", None, None),
])
def test_decisions(config, counter, memo, user, amount, purpose, action, target, count):
    d = decide(receipt(memo, user, amount, purpose), config, counter)
    assert (d.action, d.target, d.count) == (action, target, count)


@pytest.mark.parametrize("memo", ["모인런치", "모인 런치 점심", "출장 점심", "휴일근무 점심식대", "휴일 점심", "택시", "점심 선물", "회식", "접대 점심", "명함", "이혜영 화환"])
def test_exclusions(config, counter, memo):
    assert decide(receipt(memo, "이혜영"), config, counter).action == "excluded"


@pytest.mark.parametrize("amount,status", [(0, "지급대기"), (-10000, "지급대기"), (12000, "지급완료"), (12000, "지급거절")])
def test_nonpending_and_refunds(config, counter, amount, status):
    assert decide(receipt(amount=amount, status=status), config, counter).action == "excluded"


@pytest.mark.parametrize("memo", ["", "9/21", "김기태", "이혜영 자료", "이혜영 3명"])
def test_name_only_scope(config, counter, memo):
    assert decide(receipt(memo, "이혜영"), config, counter).action == "review"


def test_payer_is_not_added(config, counter):
    assert decide(receipt("김기태 임희정 점심", "서민하"), config, counter).count == 2
