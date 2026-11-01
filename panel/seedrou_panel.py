#!/usr/bin/env python3
"""SeedRou 管理面板入口。

- Python 3 标准库实现，零第三方依赖（Debian 13 minimal 开箱即用）。
- 以专用用户 seedrou 运行；特权操作一律经 sudo 调用 seedrou-priv（见 priv/）。
- --mock：无 sudo 的纯前端预览模式（开发用，固定数据）。
"""
import argparse
import mimetypes
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, "static")
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import api  # noqa: E402
import auth  # noqa: E402
import sysinfo  # noqa: E402

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

MAX_BODY = 128 * 1024 * 1024  # 上传上限 128MB


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SeedRouPanel/0.1"
    timeout = 60  # 读 body 的 socket 超时：谎报 Content-Length 的请求不能无限挂住 drain 循环

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        pass  # 访问日志走 journald 不必要；错误日志保留 stderr

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            # 超限也要把 body 读完丢弃，否则 keep-alive 连接会错位（审计 C-13）
            try:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:  # 读超时：客户端谎报 Content-Length 且不发送
                self.close_connection = True
                raise api.ApiError(400, "BODY_READ", "请求体读取超时", "客户端未按 Content-Length 发送数据")
            self.close_connection = True
            raise api.ApiError(413, "TOO_LARGE", "上传内容过大", "升级包/备份包应在 128MB 以内")
        try:
            return self.rfile.read(n) if n > 0 else b""
        except OSError:
            self.close_connection = True
            raise api.ApiError(400, "BODY_READ", "请求体读取失败", "连接中断或读取超时，请重试")

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command == "HEAD":   # HEAD 不写 body，避免 keep-alive 下客户端解析错位（审计 C-12）
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, code=200, extra=None):
        import json
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), extra=extra)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---------- HTTP ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._api("GET")
        else:
            self._static()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self._api("POST")
        else:
            self._send_json({"ok": False, "code": "NOT_FOUND", "message": "接口不存在"}, 404)

    def _api(self, method):
        try:
            # CSRF：变更请求需自定义头 + 同源
            if method == "POST" and not auth.csrf_ok(self.headers):
                self.close_connection = True  # body 未读即拒绝：必须关闭连接，避免 keep-alive 错位
                self._send_json({"ok": False, "code": "CSRF",
                                 "message": "请求缺少安全校验头",
                                 "hint": "请刷新页面后重试；请勿通过非管理页面脚本调用本接口"}, 403)
                return
            body = self._read_body()
            code, obj, extra = api.handle(method, self.path, body, self.headers,
                                          client_ip=self.client_address[0] if self.client_address else "")
            if isinstance(obj, (bytes, bytearray)):  # 二进制下载（备份包）
                extra = dict(extra or {})
                ctype = extra.pop("Content-Type", "application/octet-stream")
                self._send(code, bytes(obj), ctype, extra)
            else:
                self._send_json(obj, code, extra)
        except api.ApiError as e:
            self._send_json(e.as_dict(), e.status)
        except Exception as e:  # 最后一道防线：绝不输出堆栈给浏览器
            sys.stderr.write(f"[panel] {self.path}: {type(e).__name__}: {e}\n")
            self._send_json({"ok": False, "code": "INTERNAL",
                             "message": "面板内部错误",
                             "hint": "请重试；若持续出现，请在系统上执行 journalctl -u seedrou-panel -n 50 查看日志"}, 500)

    def _static(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/index.html", "/login"):
            if not os.path.exists(os.path.join(STATIC_DIR, "index.html")):
                self._send_json({"ok": False, "code": "NO_STATIC", "message": "前端资源缺失", "hint": "请重新运行 install.sh"}, 500)
                return
            self._file(os.path.join(STATIC_DIR, "index.html"))
            return
        rel = path.lstrip("/")
        if not rel.startswith("static/"):
            self._redirect("/")
            return
        full = os.path.realpath(os.path.join(os.path.dirname(STATIC_DIR), rel))
        if not full.startswith(os.path.realpath(STATIC_DIR)) or not os.path.isfile(full):
            self._send_json({"ok": False, "code": "NOT_FOUND", "message": "资源不存在"}, 404)
            return
        self._file(full)

    def _file(self, full):
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith((".js", ".css", ".html")):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            data = f.read()
        cache = "no-cache" if full.endswith(("index.html", "app.js")) else "max-age=3600"
        self._send(200, data, ctype, {"Cache-Control": cache})


def main():
    ap = argparse.ArgumentParser(description="SeedRou 管理面板")
    ap.add_argument("--port", type=int, default=int(os.environ.get("SEEDROU_PORT", "80")))
    ap.add_argument("--mock", action="store_true", help="开发预览：固定数据，不需要 sudo/root")
    args = ap.parse_args()

    api.init(mock=args.mock)
    auth.init(mock=args.mock)
    sysinfo.init(mock=args.mock)  # 审计 C-16：此前 MOCK 标志从未被置位

    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    httpd.daemon_threads = True
    print(f"SeedRou panel listening on :{args.port}" + (" (mock)" if args.mock else ""), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
