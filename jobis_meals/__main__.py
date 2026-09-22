from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import __version__
from .browser import BASE_URL, JobisUI, month_range
from .reports import digest, preview
from .preview_server import PreviewServer
from .selection import load_selection, selection_summary, validate_selection
from .workflow import apply_plan, scan_to_folder, verify_plan


ROOT = Path(__file__).resolve().parent.parent
PREVIEW_SERVERS = {}


def read_config(path):
    with Path(path).open(encoding="utf-8") as f:
        config = json.load(f)
    for key in ("lunch_limit", "night_limit"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(key + "는 양의 정수여야 합니다.")
    for key in ("night_users", "name_only_lunch_users", "eligible_statuses", "excluded_patterns", "non_meal_patterns"):
        if not isinstance(config.get(key), list) or not all(isinstance(s, str) and s for s in config[key]):
            raise ValueError(key + " 설정을 확인하세요.")
    for pattern in config["excluded_patterns"] + config["non_meal_patterns"]:
        re.compile(pattern)
    if not isinstance(config.get("company"), str) or not config["company"].strip():
        raise ValueError("회사명을 지정하세요.")
    if not isinstance(config.get("aliases"), dict):
        raise ValueError("aliases는 약칭:성명 형태여야 합니다.")
    for alias, full in config["aliases"].items():
        if not alias.strip() or not isinstance(full, str) or not full.strip():
            raise ValueError("약칭과 성명을 비워둘 수 없습니다.")
    return config


@contextmanager
def exclusive_session(local):
    # macOS/Linux advisory lock. OS releases it even if the process is killed.
    import fcntl
    local.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (local / "run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("다른 자동화가 실행 중입니다. 먼저 종료하세요.")
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def browser_session(config, browser):
    local = ROOT / ".local"
    with exclusive_session(local), sync_playwright() as playwright:
        kwargs = {"headless": False, "accept_downloads": False}
        if browser == "chrome":
            kwargs["channel"] = "chrome"
        context = playwright.chromium.launch_persistent_context(str(local / "browser-profile"), **kwargs)
        try:
            yield JobisUI(context, config["company"])
        finally:
            context.close()


def latest_plan():
    plans = list((ROOT / "runs").glob("*/plan.json"))
    if not plans:
        raise ValueError("검사 결과가 없습니다. 먼저 scan을 실행하세요.")
    return max(plans, key=lambda path: path.stat().st_mtime_ns)


def load_plan(path):
    path = Path(path).expanduser().resolve()
    with path.open(encoding="utf-8") as f:
        return path, json.load(f)


def show_counts(counts):
    labels = {"change": "수정 예정", "keep": "기준 충족", "review": "확인 필요", "excluded": "대상 제외"}
    print(" / ".join(f"{name} {counts.get(key, 0)}건" for key, name in labels.items()))


def launch_preview(target):
    if sys.platform == "darwin":
        # LaunchServices opens the saved file in a regular browser, after the
        # automation context has closed. Pass the path as one argument.
        for app in ("Google Chrome", "Safari"):
            try:
                result = subprocess.run(["/usr/bin/open", "-a", app, str(target)],
                                        capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    print(f"{app}에서 미리보기를 확인하세요.", flush=True)
                    return True
            except (OSError, subprocess.TimeoutExpired):
                continue
    else:
        try:
            if webbrowser.open(str(target), new=2):
                return True
        except (OSError, webbrowser.Error):
            pass
    return False


def open_preview_file(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("미리보기 파일이 없습니다: " + str(path))
    if launch_preview(str(path) if sys.platform == "darwin" else path.as_uri()):
        return True
    print("검사 결과는 저장됐지만 미리보기 자동 열기에 실패했습니다.")
    print("아래 파일을 Chrome 또는 Safari로 열거나 메뉴 5번으로 다시 여세요:")
    print(path)
    return False


def start_preview(path, config):
    path = Path(path).resolve()
    server = PREVIEW_SERVERS.get(path)
    if server and (digest(server.config) != digest(config) or digest(server.plan) != digest(load_plan(path)[1])):
        server.close()
        del PREVIEW_SERVERS[path]
        server = None
    if server is None:
        server = PreviewServer(path, config).start()
        PREVIEW_SERVERS[path] = server
    if not launch_preview(server.url):
        print("미리보기 자동 열기에 실패했습니다. 아래 주소를 Chrome에서 여세요:")
        print(server.url)
    print("미리보기에서 적용 토글을 바꾼 뒤 '선택 저장'을 누르세요. 자비스 저장은 메뉴 3번에서 진행합니다.")
    return 0


def close_previews():
    for server in PREVIEW_SERVERS.values():
        server.close()
    PREVIEW_SERVERS.clear()


def reopen_preview(plan_path=None, config=None):
    path, plan = load_plan(plan_path or latest_plan())
    config = config if config is not None else read_config(ROOT / "config.json")
    selection = load_selection(path.parent, plan, config)
    saved = path.parent / "preview.html"
    preview(path.parent, plan, config, selection)
    print(f"저장된 검사 결과: {plan['company']} / {plan['month']} / 생성 {plan['created_at']}")
    print("미리보기:", saved)
    return start_preview(path, config)


def execute(command, config, browser, month=None, plan_path=None, only=None, open_preview=False, selection=None):
    if command == "preview":
        return reopen_preview(plan_path, config)
    saved_preview = None
    if command == "scan":
        month_range(month)
    if command in ("apply", "verify"):
        path, plan = load_plan(plan_path)
    if command == "apply":
        selection = (load_selection(path.parent, plan, config) if selection is None
                     else validate_selection(plan, selection, config))
        if only is not None and (not only or set(only) - set(selection["selected_ids"])):
            raise ValueError("--only에는 미리보기에서 선택한 영수증 ID만 지정하세요.")
        if not selection["selected_ids"]:
            print("선택한 항목이 0건입니다. 자비스에 저장할 항목이 없습니다.")
            return 0
    with browser_session(config, browser) as ui:
        if command == "login":
            ui.page.goto(BASE_URL + "/receipts/main", wait_until="domcontentloaded")
            print(f"열린 자동화 전용 브라우저에서 자비스에 로그인하고 '{config['company']}'을 선택하세요.")
            input("영수증 목록이 보이면 이 터미널에서 Enter: ")
            ui.check_company()
            ui.page.locator("#receipt_table").wait_for(state="visible")
            ui.roster()
            print("로그인 확인 완료. 다음 실행부터 이 전용 브라우저의 로그인을 사용합니다.")
        elif command == "scan":
            print(f"{month} 영수증을 검사하고 있습니다. 모든 페이지 조회와 재확인이 끝나면 결과를 표시합니다.", flush=True)
            folder = ROOT / "runs" / (month + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            _, counts = scan_to_folder(ui, config, month, folder)
            print("검사 완료. 미리보기 파일을 저장했습니다.", flush=True)
            show_counts(counts)
            print("미리보기:", folder / "preview.html")
            print("계획 파일:", folder / "plan.json")
            if open_preview:
                saved_preview = folder / "plan.json"
        elif command == "apply":
            result = apply_plan(ui, config, plan, path.parent, only, selection)
            changed = sum(item["status"] in ("updated", "updated_with_vat_change") for item in result["items"])
            print(f"완료: 저장 후 검증 {changed}건 / 이미 적용됨 {len(result['items']) - changed}건")
            vat_changed = sum(item["status"] == "updated_with_vat_change" for item in result["items"])
            if vat_changed:
                print(f"자비스가 저장 과정에서 부가세를 바꾼 항목 {vat_changed}건: audit.jsonl에 변경 전후를 기록했습니다.")
            print("전체 누락 확인은 verify 명령 또는 메뉴 4번을 실행하세요.")
        elif command == "verify":
            result = verify_plan(ui, config, plan, path.parent)
            show_counts(result["counts"])
            if result["skipped_ids"]:
                print(f"사용자가 선택 해제한 수정 예정 {len(result['skipped_ids'])}건은 적용 대상에서 제외했습니다.")
            if result["success"]:
                print("검증 완료: 저장된 선택 기준으로 미반영 항목이 없습니다.")
            else:
                print(f"확인 필요: 계획과 불일치 {len(result['problems'])}건 / 수정 필요 {len(result['remaining_changes'])}건")
                print(json.dumps(result, ensure_ascii=False, indent=2))
            if result["counts"].get("review", 0):
                print("인원 판정 보류 항목은 수동 검토가 필요합니다. 미리보기의 '확인 필요'를 확인하세요.")
            return 0 if result["success"] else 2
    if saved_preview is not None:
        return reopen_preview(saved_preview, config)
    return 0


def menu(config, browser):
    while True:
        print(f"\n자비스 식대 자동화 v{__version__}\n1 로그인\n2 검사·미리보기 (저장 안 함)\n3 미리보기대로 적용\n4 빠진 항목 확인\n5 최근 미리보기 다시 열기\n0 종료")
        choice = input("선택: ").strip()
        try:
            if choice == "0":
                return 0
            if choice == "1":
                execute("login", config, browser)
            elif choice == "5":
                reopen_preview(config=config)
            elif choice == "2":
                default = datetime.now().strftime("%Y-%m")
                month = input(f"조회 월 [{default}]: ").strip() or default
                execute("scan", config, browser, month=month, open_preview=True)
            elif choice in ("3", "4"):
                default = latest_plan()
                entry = input(f"계획 파일 [Enter: {default}]: ").strip()
                path, plan = load_plan(entry or default)
                print(f"{plan['company']} / {plan['month']} / 생성 {plan['created_at']}")
                if choice == "3":
                    selection = load_selection(path.parent, plan, config)
                    counts = selection_summary(plan, selection)
                    print(f"저장된 선택 {counts['selected']}건 (대상 제외에서 추가 {counts['manual']}건 / 선택 해제 {counts['skipped']}건)")
                    if not counts["selected"]:
                        print("선택한 항목이 없습니다. 메뉴 5번에서 토글을 켜고 선택 저장을 누르세요.")
                        continue
                    if input("미리보기를 확인했으면 '적용' 입력: ").strip() != "적용":
                        print("취소했습니다.")
                        continue
                execute("apply" if choice == "3" else "verify", config, browser, plan_path=path,
                        selection=selection if choice == "3" else None)
            else:
                print("0~5 중 선택하세요.")
        except Exception as exc:
            print("중단:", exc)
            print("로그인 만료/화면 변경/메모 불일치 여부를 확인한 뒤 새로 검사하세요.")


def main():
    parser = argparse.ArgumentParser(description="자비스 식대: 검사 → 미리보기 → 적용 → 검증")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--browser", choices=("chromium", "chrome"), default="chromium")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("menu", help="대화형 메뉴")
    sub.add_parser("login", help="자동화 전용 브라우저에 로그인")
    scan = sub.add_parser("scan", help="읽기 전용 검사 및 HTML/CSV 미리보기")
    scan.add_argument("--month", required=True, help="YYYY-MM")
    scan.add_argument("--open", action="store_true", help="미리보기를 브라우저로 열기")
    saved = sub.add_parser("preview", help="저장된 미리보기 다시 열기 (로그인/재검사 없음)")
    saved.add_argument("--plan", type=Path, help="생략하면 가장 최근에 생성된 검사 결과")
    for name in ("apply", "verify"):
        p = sub.add_parser(name, help="계획 적용" if name == "apply" else "전체 누락/변경 검사")
        p.add_argument("--plan", required=True, type=Path)
        if name == "apply":
            p.add_argument("--only", help="미리보기에서 선택한 영수증 중 일부만 적용 (쉼표 구분)")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = read_config(args.config)
        if args.command in (None, "menu"):
            return menu(config, args.browser)
        only = set(args.only.split(",")) if getattr(args, "only", None) else None
        result = execute(args.command, config, args.browser, month=getattr(args, "month", None),
                         plan_path=getattr(args, "plan", None), only=only, open_preview=getattr(args, "open", False))
        if PREVIEW_SERVERS:
            input("미리보기에서 선택 저장을 마쳤으면 Enter로 미리보기를 종료하세요: ")
        return result
    except KeyboardInterrupt:
        print("\n중단했습니다. 저장 도중이었다면 같은 계획으로 재실행하거나 verify로 확인하세요.")
        return 130
    except Exception as exc:
        print("중단:", exc, file=sys.stderr)
        return 1
    finally:
        close_previews()


if __name__ == "__main__":
    raise SystemExit(main())
