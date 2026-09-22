"""Local preview editing only; this server cannot save receipts to Jobis."""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .reports import digest, preview, render_preview, write_json
from .selection import load_selection, selection_lock, selection_summary, validate_selection


class PreviewServer:
    def __init__(self, plan_path, config):
        self.path = Path(plan_path).resolve()
        self.plan = json.loads(self.path.read_text(encoding="utf-8"))
        self.config = config
        self.lock = threading.Lock()
        self.prefix = "/" + secrets.token_urlsafe(32) + "/"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data, content_type="application/json; charset=utf-8"):
                body = data.encode("utf-8") if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)

            def valid_host(self):
                return self.headers.get("Host") == owner.host

            def do_GET(self):
                if not self.valid_host() or self.path != owner.prefix:
                    return self.respond(404, {"error": "미리보기를 다시 여세요."})
                try:
                    with owner.lock, selection_lock(owner.path.parent):
                        owner.check_plan()
                        selection = load_selection(owner.path.parent, owner.plan, owner.config)
                        doc = render_preview(owner.plan, owner.config, selection,
                                             owner.prefix + "selection", digest(selection))
                    self.respond(200, doc, "text/html; charset=utf-8")
                except (ValueError, OSError) as exc:
                    self.respond(400, {"error": str(exc)})

            def do_POST(self):
                if (not self.valid_host() or self.path != owner.prefix + "selection"
                        or self.headers.get("Origin") != owner.origin
                        or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
                    return self.respond(403, {"error": "미리보기에서 선택을 저장하세요."})
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 1_000_000:
                        raise ValueError("선택 정보 크기가 잘못되었습니다.")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("선택 정보가 잘못되었습니다.")
                    with owner.lock, selection_lock(owner.path.parent):
                        owner.check_plan()
                        current = load_selection(owner.path.parent, owner.plan, owner.config)
                        if payload.get("revision") != digest(current):
                            return self.respond(409, {"error": "다른 창에서 선택이 바뀌었습니다. 새로고침 후 다시 선택하세요."})
                        selection = validate_selection(owner.plan, payload.get("selection"), owner.config)
                        preview(owner.path.parent, owner.plan, owner.config, selection)
                        write_json(owner.path.parent / "selection.json", selection)
                    self.respond(200, {"selection": selection, "revision": digest(selection),
                                       "summary": selection_summary(owner.plan, selection)})
                except (ValueError, OSError) as exc:
                    self.respond(400, {"error": str(exc)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.origin = "http://" + self.host
        self.url = self.origin + self.prefix
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def check_plan(self):
        if digest(json.loads(self.path.read_text(encoding="utf-8"))) != digest(self.plan):
            raise ValueError("검사 파일이 바뀌었습니다. 프로그램에서 미리보기를 다시 여세요.")

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
