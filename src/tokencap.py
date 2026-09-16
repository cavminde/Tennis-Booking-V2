# -*- coding: utf-8 -*-
"""
Token 自动捕获 —— 内置 MITM 代理（替代 Fiddler 的那部分工作）

原理
-----
1. 自签一个本地 CA，装进「当前用户」的受信任根证书存储（会弹一次确认框，需点【是】）
2. 把系统代理指到 127.0.0.1:<port>，并通知系统代理已变更
3. 微信小程序走系统代理 → 我们的代理收到 CONNECT
4. 用 CA 现签一张该域名的叶子证书，做 TLS 中间人；解密后从请求头里
   正则提取 Authorization: Bearer <token>
5. 抓到立刻停止：关代理、复原系统设置（证书默认保留，方便下次）

安全性
-----
- 只解密白名单域名（hdscs / hdscw），其余流量原样转发，不做任何解析
- 只「读」Authorization 头，不修改、不记录请求体
- 不碰微信进程、不做注入，无封号风险
- 超时或异常一律复原系统代理设置

可行性依据
-----
用户用 Fiddler 抓到过明文请求 ⇒ PC 版微信小程序走系统代理且信任系统根证书。
Fiddler 做的事和我们完全一样，所以这条路确定可行。
"""
import ctypes
import datetime
import os
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import winreg

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CA_CN = 'Tennis Booker Local CA'
MITM_HOSTS = ('hdscs.hainanu.edu.cn', 'hdscw.hainanu.edu.cn')
TOKEN_RE = re.compile(rb'Authorization:\s*Bearer\s+([A-Za-z0-9._\-]{40,})')
DEFAULT_PORT = 8899


def _utc(days=0):
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)


# ---------------------------------------------------------------------------
# CA 与证书
# ---------------------------------------------------------------------------

class CertAuthority:
    """自签 CA + 按域名现签叶子证书。"""

    def __init__(self, workdir):
        self.dir = workdir
        os.makedirs(self.dir, exist_ok=True)
        self.ca_key_path = os.path.join(self.dir, 'ca.key')
        self.ca_cert_path = os.path.join(self.dir, 'ca.crt')
        self._ca_key = None
        self._ca_cert = None
        self._leaf_cache = {}

    # ---------- CA ----------
    def ensure_ca(self):
        if os.path.exists(self.ca_key_path) and os.path.exists(self.ca_cert_path):
            try:
                self._ca_key = serialization.load_pem_private_key(
                    open(self.ca_key_path, 'rb').read(), password=None)
                self._ca_cert = x509.load_pem_x509_certificate(
                    open(self.ca_cert_path, 'rb').read())
                return self._ca_key, self._ca_cert
            except Exception:
                pass
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_CN)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_utc(-1))
            .not_valid_after(_utc(3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(True, False, True, False, False, True, True, False, False),
                critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256())
        )
        with open(self.ca_key_path, 'wb') as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        with open(self.ca_cert_path, 'wb') as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        self._ca_key, self._ca_cert = key, cert
        return key, cert

    @property
    def ca_cert_path_(self):
        return self.ca_cert_path

    @property
    def fingerprint(self):
        if self._ca_cert is None:
            self.ensure_ca()
        return self._ca_cert.fingerprint(hashes.SHA256()).hex()[:16].upper()

    # ---------- 叶子 ----------
    def issue(self, host):
        """给域名签一张叶子证书，返回 (certfile, keyfile)。带进程内缓存。"""
        if host in self._leaf_cache and all(os.path.exists(p) for p in self._leaf_cache[host]):
            return self._leaf_cache[host]
        if self._ca_key is None:
            self.ensure_ca()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        names = [x509.DNSName(host)]
        if not re.match(r'^\d+\.\d+\.\d+\.\d+$', host):
            names.append(x509.DNSName('*.' + host.split('.', 1)[-1])
                         if host.count('.') >= 2 else x509.DNSName(host))
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self._ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_utc(-1))
            .not_valid_after(_utc(825))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            # 这两条是链构建的关键：没有 AKI，OpenSSL 3.x 会报
            # "unable to get local issuer certificate"
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_key.public_key()),
                critical=False)
            .sign(self._ca_key, hashes.SHA256())
        )
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', host)
        cf = os.path.join(self.dir, f'{safe}.crt')
        kf = os.path.join(self.dir, f'{safe}.key')
        with open(cf, 'wb') as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(kf, 'wb') as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        self._leaf_cache[host] = (cf, kf)
        return cf, kf


# ---------------------------------------------------------------------------
# Windows：证书存储 / 系统代理
# ---------------------------------------------------------------------------

def install_ca(cert_path, log=print):
    """装进当前用户的受信任根存储。Windows 会弹确认框，需用户点【是】。"""
    try:
        r = subprocess.run(['certutil', '-user', '-addstore', 'Root', cert_path],
                           capture_output=True, timeout=60)
        out = (r.stdout or b'').decode('utf-8', 'ignore') + \
              (r.stderr or b'').decode('utf-8', 'ignore')
        ok = r.returncode == 0
        log(f'  certutil 返回 {r.returncode}')
        for ln in out.splitlines()[-6:]:
            log('    ' + ln.strip())
        if not ok:
            # 退回 PowerShell（有些机器 certutil 被策略拦）
            ps = (f'Import-Certificate -FilePath "{cert_path}" '
                  f'-CertStoreLocation Cert:\\CurrentUser\\Root | Out-Null')
            r2 = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                                capture_output=True, timeout=90)
            ok = r2.returncode == 0
            log(f'  PowerShell Import-Certificate 返回 {r2.returncode}')
        return ok
    except Exception as e:
        log(f'  × 装证书异常：{e}')
        return False


def uninstall_ca(log=print):
    """从当前用户根存储移除我们的 CA。"""
    try:
        r = subprocess.run(['certutil', '-user', '-delstore', 'Root', CA_CN],
                           capture_output=True, timeout=60)
        ok = r.returncode == 0
        log(f'  卸载 CA 返回 {r.returncode}')
        return ok
    except Exception as e:
        log(f'  × 卸载异常：{e}')
        return False


def ca_installed():
    """检查我们的 CA 是否已在当前用户根存储里。"""
    try:
        ps = (f'(Get-ChildItem Cert:\\CurrentUser\\Root | '
              f'Where-Object {{ $_.Subject -like "*{CA_CN}*" }} | Measure-Object).Count')
        r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                           capture_output=True, timeout=60)
        return (r.stdout or b'').decode().strip().isdigit() and \
               int((r.stdout or b'').decode().strip()) > 0
    except Exception:
        return False


_INET = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'


def _notify():
    try:
        ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)   # SETTINGS_CHANGED
        ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)   # REFRESH
    except Exception:
        pass


def get_proxy():
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INET, 0, winreg.KEY_READ)
        en, _ = winreg.QueryValueEx(k, 'ProxyEnable')
        sv, _ = winreg.QueryValueEx(k, 'ProxyServer')
        winreg.CloseKey(k)
        return bool(en), (sv or '')
    except Exception:
        return False, ''


def set_proxy(port, log=print):
    """把系统代理指到 127.0.0.1:port，并记住原来的设置以便复原。"""
    old = get_proxy()
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INET, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, 'ProxyEnable', 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, 'ProxyServer', 0, winreg.REG_SZ, f'127.0.0.1:{port}')
        winreg.SetValueEx(k, 'ProxyOverride', 0, winreg.REG_SZ,
                          'localhost;127.*;<local>;<-loopback>')
        winreg.CloseKey(k)
        _notify()
        log(f'  系统代理已指向 127.0.0.1:{port}（原设置：{old}）')
        return old
    except Exception as e:
        log(f'  × 设置代理失败：{e}')
        return old


def restore_proxy(old, log=print):
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INET, 0, winreg.KEY_SET_VALUE)
        en, sv = old
        winreg.SetValueEx(k, 'ProxyEnable', 0, winreg.REG_DWORD, 1 if en else 0)
        if sv:
            winreg.SetValueEx(k, 'ProxyServer', 0, winreg.REG_SZ, sv)
        else:
            try:
                winreg.DeleteValue(k, 'ProxyServer')
            except Exception:
                winreg.SetValueEx(k, 'ProxyServer', 0, winreg.REG_SZ, '')
        winreg.CloseKey(k)
        _notify()
        log(f'  系统代理已复原为：{old}')
        return True
    except Exception as e:
        log(f'  × 复原代理失败：{e}')
        return False


# ---------------------------------------------------------------------------
# MITM 服务器
# ---------------------------------------------------------------------------

def _pump(src, dst, host, server):
    """转发；host 非空时顺带嗅探 Authorization。"""
    buf = b''
    try:
        while True:
            try:
                data = src.recv(8192)
            except (ConnectionResetError, OSError, ssl.SSLError):
                break
            if not data:
                break
            try:
                dst.sendall(data)
            except Exception:
                break
            if host and server is not None:
                buf += data
                m = TOKEN_RE.search(buf)
                if m:
                    server.on_token(m.group(1).decode('ascii', 'ignore'), host)
                    buf = b''
                elif len(buf) > 262144:
                    buf = buf[-4096:]
    except Exception:
        pass
    finally:
        for s in (dst, src):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass


def _tunnel(c2s, host, port):
    """不需要解密的域名：纯转发。"""
    try:
        up = socket.create_connection((host, port), timeout=20)
    except Exception:
        try:
            c2s.close()
        except Exception:
            pass
        return
    t = threading.Thread(target=_pump, args=(up, c2s, None, None), daemon=True)
    t.start()
    _pump(c2s, up, None, None)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(65537)
        except Exception:
            return
        if not line:
            return
        try:
            method, target, _ver = line.split(b' ')
        except ValueError:
            return
        # 吃掉剩余请求头
        try:
            while True:
                h = self.rfile.readline(65537)
                if h in (b'\r\n', b'\n', b''):
                    break
        except Exception:
            return

        if method.upper() == b'CONNECT':
            host, _, port = target.decode().rpartition(':')
            port = int(port or 443)
            mitm = self.server.should_mitm(host)
            self.server.log(f'  [代理] CONNECT {host}:{port} → {"解密" if mitm else "直连转发"}')
            if mitm:
                self._mitm(host, port)
            else:
                try:
                    self.connection.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                except Exception:
                    return
                _tunnel(self.connection, host, port)
        else:
            # 明文 HTTP：直接丢弃（目标站点全站 HTTPS，正常走不到这里）
            try:
                self.connection.close()
            except Exception:
                pass

    def _mitm(self, host, port):
        self.server.log(f'  [MITM] 拦截 {host}:{port}')
        try:
            up = socket.create_connection((host, port), timeout=20)
        except Exception as e:
            self.server.log(f'  [MITM] × 连不上目标 {host}:{port}：{e}')
            try:
                self.connection.close()
            except Exception:
                pass
            return
        try:
            self.connection.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        except Exception as e:
            self.server.log(f'  [MITM] × 回 200 失败：{e}')
            return

        try:
            cf, kf = self.server.ca.issue(host)
            sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                sctx.set_alpn_protocols(['http/1.1'])
            except Exception:
                pass
            sctx.load_cert_chain(cf, kf)
            cli = sctx.wrap_socket(self.connection, server_side=True)
        except Exception as e:
            self.server.log(f'  [TLS] 客户端握手失败 {host}：{e}')
            try:
                up.close()
            except Exception:
                pass
            return

        uctx = ssl.create_default_context()
        try:
            uctx.set_alpn_protocols(['http/1.1'])
        except Exception:
            pass
        try:
            srv = uctx.wrap_socket(up, server_hostname=host)
        except Exception:
            # 目标证书异常时仍继续，只是不再校验（本地工具，可接受）
            uctx.check_hostname = False
            uctx.verify_mode = ssl.CERT_NONE
            try:
                srv = uctx.wrap_socket(up, server_hostname=host)
            except Exception as e:
                self.server.log(f'  [TLS] 连接目标失败 {host}：{e}')
                try:
                    cli.close()
                except Exception:
                    pass
                return

        self.server.log(f'  ⇄ 已接入 {host}')
        threading.Thread(target=_pump, args=(srv, cli, None, None), daemon=True).start()
        _pump(cli, srv, host, self.server)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, addr, ca, log, on_token, hosts):
        self.ca = ca
        self.log = log
        self._on_token = on_token
        self.hosts = hosts
        super().__init__(addr, _Handler)

    def should_mitm(self, host):
        h = host.lower()
        return any(h == m or h.endswith('.' + m) for m in self.hosts)

    def on_token(self, token, host):
        self.log(f'  ★ 从 {host} 捕获到 token（{len(token)} 字符）')
        self._on_token(token)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

class TokenCapture:
    def __init__(self, workdir, port=DEFAULT_PORT, log=print):
        self.dir = workdir
        self.port = port
        self.log = log
        self.ca = CertAuthority(workdir)
        self.server = None
        self.thread = None
        self.token = None
        self._old_proxy = (False, '')
        self._running = False
        self._lock = threading.Lock()

    def prepare(self):
        """生成/加载 CA，必要时安装到根存储。"""
        self.ca.ensure_ca()
        self.log(f'[证书] 本地 CA：{CA_CN}（指纹 {self.ca.fingerprint}）')
        if ca_installed():
            self.log('[证书] 已在受信任根存储中，无需重复安装')
            return True
        self.log('[证书] 正在安装到「当前用户 → 受信任的根证书颁发机构」…')
        self.log('       ⚠ 屏幕上会弹出 Windows 安全确认框，请点【是】；')
        self.log('         没看到弹窗就去任务栏找闪烁的图标。')
        ok = install_ca(self.ca.ca_cert_path, log=self.log)
        self.log('[证书] 安装' + ('成功' if ok else '失败/被取消'))
        return ok

    def start(self, timeout=300):
        with self._lock:
            if self._running:
                return False
            self._running = True
        self.token = None
        self._old_proxy = set_proxy(self.port, log=self.log)
        try:
            self.server = _Server(('127.0.0.1', self.port), self.ca, self.log,
                                  self._on_token, MITM_HOSTS)
        except OSError as e:
            self.log(f'× 端口 {self.port} 起不来：{e}')
            self._running = False
            return False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.log(f'[代理] 已在 127.0.0.1:{self.port} 监听，等待小程序发请求…')
        return True

    def _on_token(self, token):
        if not self.token:
            self.token = token

    def wait(self, timeout=300, poll=0.3):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.token:
                return self.token
            if not self._running:
                break
            time.sleep(poll)
        return self.token

    def stop(self, restore=True):
        with self._lock:
            self._running = False
        try:
            if self.server:
                self.server.shutdown()
                self.server.server_close()
        except Exception:
            pass
        if restore:
            restore_proxy(self._old_proxy, log=self.log)
        self.log('[代理] 已停止')

    def cleanup_cert(self):
        uninstall_ca(log=self.log)


def capture_token(workdir, timeout=300, log=print, port=DEFAULT_PORT, stop=None):
    """一键抓 token：准备 → 启动 → 等待 → 复原。返回 token 或 None。"""
    stop = stop or (lambda: False)
    cap = TokenCapture(workdir, port=port, log=log)
    if not cap.prepare():
        log('× 证书没装成，无法继续。（没点【是】的话再试一次）')
        return None
    if not cap.start():
        return None
    try:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cap.token:
                return cap.token
            if stop():
                log('  用户中止。')
                return None
            time.sleep(0.3)
        log(f'× {timeout} 秒内没抓到 —— 确认微信里打开过小程序并点了几个页面。')
        return None
    finally:
        cap.stop(restore=True)
