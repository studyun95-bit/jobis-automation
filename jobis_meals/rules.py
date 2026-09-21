from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Optional


def clean(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


@dataclass(frozen=True)
class Receipt:
    id: str
    url: str
    date: str
    user: str
    merchant: str
    amount: int
    purpose: str
    status: str
    memo: str
    evidence: str = ""
    duplicate: str = ""
    tax: str = ""


@dataclass(frozen=True)
class People:
    count: Optional[int]
    names: tuple[str, ...]
    reason: str
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    action: str  # change / keep / review / excluded
    kind: str
    target: Optional[int]
    count: Optional[int]
    names: tuple[str, ...]
    reason: str

    def to_dict(self):
        result = asdict(self)
        result["names"] = list(self.names)
        return result


# 숫자는 날짜와 인원 표현을 해석한 뒤 제거한다. '인' 등을 전역 삭제하지 않는다.
COUNT = re.compile(r"(?P<prefix>본인\s*포함|포함|총|외)?\s*(?P<n>\d+)\s*(?:인|명)(?![가-힣])")
DATE = re.compile(r"\d{2,4}\s*[-/.–]\s*\d{1,2}(?:\s*[-/.–]\s*\d{1,2})?|\d+\s*[년월일]|\b\d{6,8}\b")
MEAL_WORDS = re.compile(r"(?:점\s*심|덤심|야간\s*근무|야간|야근|저녁)(?:\s*(?:근무|식사|식대|식비))*|식대|식사|식비")


class NameCounter:
    """직원 ID로 중복 제거. 유일하게 분해 가능한 이름만 자동 확정한다."""

    def __init__(self, employees: list[dict], aliases: dict[str, str]):
        self.by_token: dict[str, set[str]] = {}
        self.labels: dict[str, str] = {}
        for employee in employees:
            key, name = str(employee["id"]), clean(employee["name"])
            if not name or "(삭제)" in name:
                continue
            self.labels[key] = name
            self.by_token.setdefault(name.replace(" ", ""), set()).add(key)
        for alias, full_name in aliases.items():
            ids = self.by_token.get(clean(full_name).replace(" ", ""), set())
            if ids:
                self.by_token.setdefault(clean(alias), set()).update(ids)

    def segment(self, token: str) -> Optional[set[tuple[str, ...]]]:
        class TooManyWays(Exception):
            pass

        @lru_cache(None)
        def walk(position):
            if position == len(token):
                return {()}
            found = set()
            for name, ids in self.by_token.items():
                if token.startswith(name, position):
                    end = position + len(name)
                    # '님'은 이름 뒤에 붙은 경우만 소비한다.
                    if token[end:end + 1] == "님":
                        end += 1
                    for tail in walk(end):
                        for person_id in ids:
                            found.add((person_id,) + tail)
                            if len(found) > 8:
                                raise TooManyWays
            return found
        try:
            return walk(0)
        except (TooManyWays, RecursionError):
            return None

    def count(self, memo: str, submitter: str = "") -> People:
        text = clean(memo)
        counts = [(clean(m.group("prefix") or ""), int(m.group("n"))) for m in COUNT.finditer(text)]
        text = COUNT.sub(" ", text)
        text = DATE.sub(" ", text)
        text = MEAL_WORDS.sub(" ", text)
        if "본인" in text:
            text = text.replace("본인", submitter)
        text = re.sub(r"\b(?:포함|총|및|와|과)\b", " ", text)
        # 알 수 없는 한글/영문은 이름 없음을 뜻하지 않는다.
        tokens = re.findall(r"[가-힣A-Za-z]+", text)
        ids: set[str] = set()
        unknown = []
        for token in tokens:
            paths = self.segment(token)
            if paths is None:
                unknown.append(token)
                continue
            identities = {tuple(sorted(set(path))) for path in paths}
            if len(identities) != 1:
                unknown.append(token)
            else:
                ids.update(next(iter(identities)))
        names = tuple(sorted(self.labels[key] for key in ids))
        if unknown:
            return People(None, names, "이름 또는 약칭을 확정할 수 없음: " + ", ".join(unknown), tuple(unknown))
        totals = set()
        anchored = False
        for prefix, n in counts:
            if n < 1 or n > 100:
                return People(None, names, "인원 숫자 범위 확인 필요")
            if prefix == "외":
                if not ids:
                    return People(None, names, "'외 N인'의 기준 사람이 없음")
                n += 1
                anchored = True
            if "포함" in prefix:
                anchored = True
            totals.add(n)
        if len(totals) > 1:
            return People(None, names, "메모의 인원 숫자들이 서로 다름")
        if totals:
            total = next(iter(totals))
            if ids and len(ids) != total and not (anchored and len(ids) == 1):
                return People(None, names, f"명시 {total}명 / 인식한 이름 {len(ids)}명 불일치")
            return People(total, names, f"메모의 명시 인원 {total}명")
        total = len(ids) or 1
        return People(total, names, f"서로 다른 이름 {total}명" if ids else "이름 없음 → 1명")


def decide(receipt: Receipt, config: dict, counter: NameCounter) -> Decision:
    r, memo = receipt, clean(receipt.memo)

    def excluded(reason):
        return Decision("excluded", "other", None, None, (), reason)

    if r.status not in config["eligible_statuses"]:
        return excluded("처리 대상 지급상태가 아님: " + r.status)
    if r.amount <= 0:
        return excluded("0원 또는 취소/환불 금액")
    if any(re.search(p, memo, re.I) for p in config["excluded_patterns"]):
        return excluded("모인런치·출장·휴일근무 별도 식대")
    if any(re.search(p, memo, re.I) for p in config["non_meal_patterns"]):
        return excluded("점심 외 지출")
    night = bool(re.search(r"야간|야근|저녁", memo))
    lunch = bool(re.search(r"점\s*심|덤심", memo))
    if night and lunch:
        return Decision("review", "unknown", None, None, (), "점심과 야간 표현이 함께 있음")
    if night:
        if r.user not in config["night_users"]:
            return excluded("지정된 두 명 이외의 야간/저녁 식대")
        kind = "night"
    elif lunch:
        kind = "lunch"
    elif r.user in config["name_only_lunch_users"]:
        # 날짜와 이름만 있는 승인된 사용자 메모만 허용한다.
        people = counter.count(memo, r.user)
        if not people.count or not people.names or r.user not in people.names or COUNT.search(memo):
            return Decision("review", "unknown", None, None, people.names, "날짜·이름만 있는 점심인지 확인 필요")
        kind = "lunch"
    else:
        return excluded("점심 또는 지정 야간식대로 확인되지 않음")
    people = counter.count(memo, r.user)
    if people.count is None:
        return Decision("review", kind, None, None, people.names, people.reason)
    if kind == "night" and people.count != 1:
        return Decision("review", kind, None, people.count, people.names, "지정 야간식대에 여러 명: 별도 확인 필요")
    limit = config["night_limit"] if kind == "night" else config["lunch_limit"] * people.count
    target = min(r.amount, limit)
    action = "keep" if r.amount == target and r.purpose == "식비" else "change"
    return Decision(action, kind, target, people.count, people.names, people.reason + f" / 한도 {limit:,}원")
