from __future__ import annotations

import csv
import hashlib
import html
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.chmod(0o600)
    temp.replace(path)


def journal_writer(path):
    def journal(event, data):
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": datetime.now().astimezone().isoformat(), "event": event, **data}, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(path).chmod(0o600)
    return journal


def csv_safe(value):
    value = str(value if value is not None else "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


LABELS = {"change": "수정 예정", "keep": "기준 충족", "review": "확인 필요", "excluded": "대상 제외"}
COLUMNS = ["영수증ID", "일자", "사용자", "업체명", "기존금액", "예정금액", "현재목적", "예정목적", "인원", "인식한사람", "검사 분류", "판단근거", "메모"]


def preview_rows(plan, config, selection):
    from .selection import manual_decision
    rows = []
    for entry in plan["entries"]:
        r, d = entry["receipt"], entry["decision"]
        if r["id"] in selection["overrides"]:
            d = manual_decision(r, selection["overrides"][r["id"]], config).to_dict()
        rows.append([r["id"], r["date"], r["user"], r["merchant"], r["amount"], d["target"], r["purpose"],
                     "식비" if d["action"] in ("change", "keep") else r["purpose"], d["count"],
                     ", ".join(entry["decision"]["names"]), LABELS[entry["decision"]["action"]], d["reason"], r["memo"]])
    return rows


def render_preview(plan, config=None, selection=None, endpoint=None, revision=None):
    from .selection import can_include, default_selection, meal_types
    selection = selection if selection is not None else default_selection(plan)
    counts = Counter(e["decision"]["action"] for e in plan["entries"])
    cards = "".join(f'<div><b>{counts[k]}</b>{v}</div>' for k, v in LABELS.items())
    body = []
    entries = []
    selected = set(selection["selected_ids"])
    kinds = meal_types(config or {})
    options = ''.join(f'<option value="{kind}">{spec["label"]} · {spec["limit"]:,}원/인</option>'
                      for kind, spec in kinds.items())
    for entry, row in zip(plan["entries"], preview_rows(plan, config, selection)):
        r, d = entry["receipt"], entry["decision"]
        rid = html.escape(r["id"], quote=True)
        allowed = d["action"] == "change" or (d["action"] == "excluded" and config and can_include(r, config))
        checked = " checked" if r["id"] in selected else ""
        disabled = "" if allowed and endpoint else " disabled"
        control = f'<label class="switch"><input type="checkbox" role="switch" class="apply-toggle" aria-label="{rid} 적용"{checked}{disabled}><span></span></label><span class="choice-label"></span>'
        if allowed:
            control += (f'<div class="manual" hidden><label>식대 <select class="kind" aria-label="{rid} 식대 종류">{options}</select></label>'
                        f'<label>인원 <input class="people" type="number" min="1" max="100" step="1" placeholder="직접 입력" aria-label="{rid} 인원"> 명</label></div>')
        fields = {5: "target", 7: "purpose", 8: "count", 11: "reason"}
        cells = ''.join('<td' + (f' data-field="{fields[i]}"' if i in fields else '') + '>'
                        + html.escape(str(v if v is not None else "—")) + '</td>' for i, v in enumerate(row))
        body.append(f'<tr data-id="{rid}" data-status="{d["action"]}"><td class="choice">{control}</td>{cells}</tr>')
        entries.append({"id": r["id"], "amount": r["amount"], "purpose": r["purpose"], "decision": d})
    payload = {"entries": entries, "meal_types": kinds, "selection": selection,
               "endpoint": endpoint, "revision": revision}
    encoded = json.dumps(payload, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    replacements = {"TITLE": html.escape(plan["company"] + " / " + plan["month"]), "CARDS": cards,
                    "HEAD": '<th>적용 선택</th>' + ''.join('<th>' + c + '</th>' for c in COLUMNS),
                    "BODY": ''.join(body), "DATA": encoded}
    template = Path(__file__).with_name("preview.html").read_text(encoding="utf-8")
    return re.sub(r"__(TITLE|CARDS|HEAD|BODY|DATA)__", lambda m: replacements[m[1]], template)


def preview(folder: Path, plan: dict, config=None, selection=None):
    from .selection import default_selection
    selection = selection if selection is not None else default_selection(plan)
    rows = preview_rows(plan, config, selection)
    selected = set(selection["selected_ids"])
    with (folder / "preview.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["적용 선택"] + COLUMNS)
        writer.writerows([["적용" if row[0] in selected else "제외"] + [csv_safe(v) for v in row] for row in rows])
    counts = Counter(e["decision"]["action"] for e in plan["entries"])
    (folder / "preview.html").write_text(render_preview(plan, config, selection), encoding="utf-8")
    return dict(counts)
