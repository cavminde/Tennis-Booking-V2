# -*- coding: utf-8 -*-
"""
海大网球订场 V2 · 本地控制服务
启动： python app_server.py    然后浏览器打开 http://127.0.0.1:8081

接口：
  GET  /                 页面
  GET  /api/config       读取配置（config.json）
  POST /api/config       保存配置
  GET  /api/status       任务状态 + 日志
  POST /api/preview      侦察空场 + 生成候选（不提交）
  POST /api/start_now    立即抢
  POST /api/start_sched  定时抢
  POST /api/stop         中止
  POST /api/bench        接口测速
  POST /api/diagnose     下单有效性检测
  POST /api/salt         重置反冲突随机签
"""
import json
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import booker as b

HOST = '127.0.0.1'
PORT = 8081

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 打包成 exe 后，配置文件放 exe 同目录，方便用户直接改
if getattr(sys, 'frozen', False):
    CONFIG_PATH = os.path.join(os.path.dirname(sys.executable), 'config.json')
else:
    CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

STATE = {'running': False, 'log': [], 'result': None, 'task': None}
_LOCK = threading.Lock()
_STOP = threading.Event()


def append_log(msg):
    with _LOCK:
        STATE['log'].append(str(msg))
        if len(STATE['log']) > 3000:
            STATE['log'] = STATE['log'][-3000:]


def read_config():
    cfg = b.default_config()
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding='utf-8') as f:
                cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def write_config(cfg):
    old = read_config()
    old.update(cfg or {})
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(old, f, ensure_ascii=False, indent=2)
    return old


def params_from(payload):
    cfg = read_config()
    p = dict(cfg)
    p.update(payload or {})
    p['base_dir'] = os.path.dirname(CONFIG_PATH)
    return p


def parse_schedule(raw):
    raw = (raw or '').strip()
    if not raw:
        return None
    try:
        parts = [int(x) for x in raw.split(':')]
        while len(parts) < 3:
            parts.append(0)
        h, m, s = parts[:3]
    except Exception:
        return None
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return None
    return (h, m, s)


def spawn(kind, params):
    def runner():
        STATE['running'] = True
        _STOP.clear()
        t0 = time.time()
        try:
            if kind == 'preview':
                params['submit'] = False
                res = b.run_booking(params, log=append_log, stop=lambda: _STOP.is_set())
            elif kind == 'bench':
                res = b.benchmark(params, log=append_log, stop=lambda: _STOP.is_set())
            elif kind == 'diagnose':
                res = b.diagnose(params, log=append_log)
            else:
                params['submit'] = True
                res = b.run_booking(params, log=append_log, stop=lambda: _STOP.is_set())
            with _LOCK:
                STATE['result'] = res
        except Exception as e:
            append_log(f'[异常] {type(e).__name__}: {e}')
            with _LOCK:
                STATE['result'] = {'success': False, 'error': f'{type(e).__name__}: {e}'}
        finally:
            append_log(f'[完成] 耗时 {time.time() - t0:.1f}s')
            STATE['running'] = False

    threading.Thread(target=runner, daemon=True).start()


def load_page():
    """优先读外部 index.html（改完刷新即可见），找不到再退回内嵌副本。"""
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.join(getattr(sys, '_MEIPASS', BASE_DIR), 'index.html'))
    candidates.append(os.path.join(BASE_DIR, 'index.html'))
    for p in candidates:
        try:
            with open(p, encoding='utf-8') as f:
                return f.read()
        except Exception:
            continue
    try:
        import index_data
        import base64
        return base64.b64decode(index_data.B64).decode('utf-8')
    except Exception:
        return '<h1>index.html 缺失</h1>'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _q(self):
        from urllib.parse import parse_qs
        try:
            return parse_qs(urlparse(self.path).query)
        except Exception:
            return {}

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text):
        body = text.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            self._html(load_page())
        elif path == '/api/status':
            with _LOCK:
                self._json({'running': STATE['running'], 'log': STATE['log'],
                            'result': STATE['result'], 'task': STATE['task']})
        elif path == '/api/config':
            self._json(read_config())
        elif path == '/api/fingerprint':
            q = self._q()
            rs = b.RandomSource(os.path.join(os.path.dirname(CONFIG_PATH),
                                             '.anti_collision_salt'),
                                passphrase=(q.get('passphrase') or [''])[0])
            self._json({'fingerprint': rs.fingerprint, 'salt': rs.salt[:12] + '…'})
        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0) or 0)
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8')) if raw else {}
        except Exception:
            payload = {}

        with _LOCK:
            busy = STATE['running']

        if path == '/api/config':
            self._json(write_config(payload))
            return

        if path == '/api/salt':
            rs = b.RandomSource(os.path.join(os.path.dirname(CONFIG_PATH),
                                             '.anti_collision_salt'),
                                passphrase=payload.get('passphrase') or '')
            rs.reset_salt()
            self._json({'ok': True, 'fingerprint': rs.fingerprint})
            return

        if busy and path != '/api/stop':
            self._json({'ok': False, 'msg': '已有任务在进行，请先停止'})
            return

        if path == '/api/stop':
            _STOP.set()
            self._json({'ok': True})
            return

        with _LOCK:
            STATE['log'] = []
            STATE['result'] = None

        if path == '/api/preview':
            STATE['task'] = '侦察空场'
            spawn('preview', params_from(payload))
        elif path == '/api/start_now':
            STATE['task'] = '立即抢单'
            p = params_from(payload)
            p['schedule'] = None
            spawn('book', p)
        elif path == '/api/start_sched':
            STATE['task'] = '定时抢单'
            p = params_from(payload)
            p['schedule'] = parse_schedule(payload.get('schedule') or
                                           read_config().get('schedule'))
            if not p['schedule']:
                self._json({'ok': False, 'msg': '定时时间格式不对'})
                return
            spawn('book', p)
        elif path == '/api/bench':
            STATE['task'] = '接口测速'
            spawn('bench', params_from(payload))
        elif path == '/api/diagnose':
            STATE['task'] = '有效性检测'
            spawn('diagnose', params_from(payload))
        else:
            self.send_error(404)
            return

        self._json({'ok': True})


class Server(ThreadingHTTPServer):
    # Windows 下 SO_REUSEADDR 允许两个进程同时绑同一端口，
    # 会导致「改了代码却还是旧页面」。显式关掉，保证一个端口只有一个实例。
    allow_reuse_address = False
    daemon_threads = True


def main():
    try:
        server = Server((HOST, PORT), Handler)
    except OSError as e:
        print(f'× 端口 {PORT} 已被占用（{e}）')
        print('  多半是上一次的程序没关干净。排查：')
        print(f'      netstat -ano | findstr :{PORT}')
        print('      taskkill /PID <PID> /F')
        try:
            input('\n按回车键退出...')
        except EOFError:
            pass
        return

    url = f'http://{HOST}:{PORT}'
    print('=' * 58)
    print('  海大网球订场 V2 已启动')
    print(f'  {url}')
    print('  关闭此窗口即停止服务')
    print('=' * 58)
    try:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
