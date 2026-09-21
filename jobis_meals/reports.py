from __future__ import annotations

import csv
import hashlib
import html
import json
import os
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


def preview(folder: Path, plan: dict):
    labels = {"change": "수정 예정", "keep": "기준 충족", "review": "확인 필요", "excluded": "대상 제외"}
    columns = ["영수증ID", "일자", "사용자", "업체명", "기존금액", "예정금액", "현재목적", "예정목적", "인원", "인식한사람", "상태", "판단근거", "메모"]
    rows = []
    for entry in plan["entries"]:
        r, d = entry["receipt"], entry["decision"]
        rows.append([r["id"], r["date"], r["user"], r["merchant"], r["amount"], d["target"], r["purpose"],
                     "식비" if d["action"] in ("change", "keep") else r["purpose"], d["count"],
                     ", ".join(d["names"]), labels[d["action"]], d["reason"], r["memo"]])
    with (folder / "preview.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows([[csv_safe(v) for v in row] for row in rows])
    counts = Counter(e["decision"]["action"] for e in plan["entries"])
    cards = "".join(f'<div><b>{counts[k]}</b>{v}</div>' for k, v in labels.items())
    body = "".join('<tr data-status="' + e["decision"]["action"] + '">' +
                   "".join("<td>" + html.escape(str(v if v is not None else "—")) + "</td>" for v in row) + "</tr>"
                   for e, row in zip(plan["entries"], rows))
    doc = """<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>자비스 식대 변경 미리보기</title><style>
body{font:14px -apple-system,BlinkMacSystemFont,sans-serif;color:#172b40;background:#f3f6f9;margin:32px}h1{font-size:28px}.cards{display:flex;gap:16px;margin:24px 0}.cards div{background:white;border-radius:12px;padding:18px 24px;min-width:110px}.cards b{display:block;font-size:28px;margin-bottom:5px}input,select{padding:10px;border:1px solid #ccd5df;border-radius:6px;margin:0 8px 16px 0}.table{overflow:auto;background:white;border-radius:8px}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:11px;border-bottom:1px solid #e5eaf0;text-align:left}th{background:#e9f0f6;position:sticky;top:0}tr[data-status=review]{background:#fff5dc}tr[data-status=change]{background:#edfaf4}td:last-child{white-space:normal;min-width:260px}p{color:#526276}</style>
<h1>자비스 식대 변경 미리보기</h1><p>__TITLE__ · 저장 전 검사 결과입니다. 확인 필요 항목은 적용에서 제외됩니다.</p>
<div class="cards">__CARDS__</div><input id="q" placeholder="이름·메모·영수증ID 검색"><select id="s"><option value="">전체 상태</option><option value="change">수정 예정</option><option value="review">확인 필요</option><option value="keep">기준 충족</option><option value="excluded">대상 제외</option></select>
<div class="table"><table><thead><tr>__HEAD__</tr></thead><tbody>__BODY__</tbody></table></div>
<script>function filter(){const q=document.getElementById('q').value.toLowerCase(),s=document.getElementById('s').value;document.querySelectorAll('tbody tr').forEach(r=>r.hidden=!(r.textContent.toLowerCase().includes(q)&&(!s||r.dataset.status===s)))}document.getElementById('q').oninput=filter;document.getElementById('s').onchange=filter;</script></html>"""
    doc = doc.replace("__TITLE__", html.escape(plan["company"] + " / " + plan["month"]))
    doc = doc.replace("__CARDS__", cards).replace("__HEAD__", "".join("<th>" + c + "</th>" for c in columns)).replace("__BODY__", body)
    (folder / "preview.html").write_text(doc, encoding="utf-8")
    return dict(counts)
