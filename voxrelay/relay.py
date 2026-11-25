"""把页面里的合成声音送到内网另一台 Mac 的声卡：浏览器 →(WebSocket)→ 本机 →(SSH)→ 对方的 vox_play。

对方机器上只需要：开启“远程登录”、本机能免密 SSH、装好虚拟声卡（如 BlackHole）。播放程序会自动拷过去。
自检: ./start.sh check [用户名@地址] [--device 名称] [--tone]
"""
import hashlib, json, math, os, queue, re, shlex, subprocess, sys, threading, time
from collections import deque

from websockets.sync.server import serve

from . import config

SOURCE = os.path.join(config.HOME, "remote", "vox_play.swift")
SSH = os.environ.get("VOXRELAY_SSH", "ssh")  # 测试时可换成假的 ssh
SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=3"]
TARGET_RE = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*")  # 不能以 - 开头，避免被当成 ssh 参数
_build_lock = threading.Lock()


class RemoteError(Exception):
    pass


def explain(err, target):
    """把 ssh 的报错换成能照着做的话。"""
    err = err.strip()
    if "No user exists for uid" in err:
        return "当前服务进程查不到系统用户，SSH 用不了。请在“终端”里运行 ./start.sh 重新启动服务。"
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in err:
        return f"{target} 的主机指纹变了。确认是同一台电脑后，在终端处理 known_hosts 再试。"
    if "Host key verification failed" in err:
        return f"还没信任过这台电脑。请先在终端运行一次 ssh {target} 并确认指纹。"
    if "Permission denied" in err:
        return f"SSH 免密登录没配好。请在终端运行 ssh-copy-id {target}（需要输入对方密码一次）。"
    if re.search(r"timed out|No route to host|Connection refused|Could not resolve|Host is down", err):
        return f"连不上 {target}。请确认对方开机、在同一网络，并开启了“系统设置 → 通用 → 共享 → 远程登录”。"
    return err.splitlines()[-1] if err else "SSH 连接失败"


def ssh_cmd(target, command):
    if not isinstance(target, str) or not TARGET_RE.fullmatch(target):
        raise RemoteError("地址格式应为 用户名@内网地址")
    return [SSH, *SSH_OPTS, target, command]


def run_ssh(target, command, data=None, timeout=40):
    try:
        p = subprocess.run(ssh_cmd(target, command), input=data if data is not None else b"", capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RemoteError(f"{target} 响应超时")
    out, err = p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    if p.returncode:
        msg = next((l[4:] for l in out.splitlines() if l.startswith("ERR ")), None)
        raise RemoteError(msg or explain(err, target))
    return out


def build():
    """在本机编译出 arm64 + x86_64 通用版播放程序；源码没变就复用。"""
    with open(SOURCE, "rb") as f:
        name = "vox_play-" + hashlib.sha1(f.read()).hexdigest()[:10]
    root = os.path.join(config.cache_dir(), "build")
    out = os.path.join(root, name)
    with _build_lock:
        if not os.path.exists(out):
            tmp = os.path.join(root, "tmp")
            os.makedirs(tmp, exist_ok=True)
            env = dict(os.environ, TMPDIR=tmp)
            slices = []
            for arch in ("arm64", "x86_64"):
                s = f"{out}.{arch}"
                p = subprocess.run(["swiftc", "-O", "-target", f"{arch}-apple-macos12.0", "-module-cache-path",
                                    os.path.join(root, "modcache"), SOURCE, "-o", s], capture_output=True, text=True, env=env)
                if p.returncode:
                    raise RemoteError("编译播放程序失败：" + p.stderr.strip()[-600:])
                slices.append(s)
            subprocess.run(["lipo", "-create", *slices, "-output", out + ".part"], check=True, env=env)
            os.replace(out + ".part", out)
            for s in slices:
                os.remove(s)
    return out, name


def deploy(target, log=lambda m: None):
    """确保对方机器上有当前版本的播放程序（放在 ~/.voxrelay/），返回它在对方 shell 里的路径。"""
    local, name = build()
    path = f'"$HOME/.voxrelay/{name}"'
    out = run_ssh(target, f"uname -s; test -x {path} && echo HAVE || echo MISSING").split()
    if not out or out[0] != "Darwin":
        raise RemoteError("目前只支持 macOS，对方系统是 " + (out[0] if out else "未知"))
    if out[-1] != "HAVE":
        log(f"正在把播放程序拷到 {target}:~/.voxrelay/{name}")
        part = f'"$HOME/.voxrelay/{name}.part"'
        with open(local, "rb") as f:
            run_ssh(target, f'mkdir -p "$HOME/.voxrelay" && cat > {part} && chmod 755 {part} && mv {part} {path}', f.read(), 60)
    return path


def list_devices(target, log=lambda m: None):
    out = run_ssh(target, deploy(target, log) + " --list")
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RemoteError("读不到对方的声卡列表")


class Session:
    """一条到对方声卡的音频通道。write() 不阻塞；网络卡住时丢旧数据，不让延迟越积越大。"""

    def __init__(self, target, device, rate):
        path = deploy(target)
        if not isinstance(device, str) or not device.strip():
            raise RemoteError("还没选对方的声卡")
        cmd = f"{path} --device {shlex.quote(device)} --rate {int(rate)}"
        self.proc = subprocess.Popen(ssh_cmd(target, cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.target, self.error = target, ""
        self.lines, self._err = queue.Queue(), deque(maxlen=20)
        self._audio = queue.Queue(maxsize=50)  # 每块 20 ms，最多积 1 秒
        for fn in (self._read_out, self._read_err, self._write):
            threading.Thread(target=fn, daemon=True).start()
        try:
            first = self.lines.get(timeout=20)
        except queue.Empty:
            self.close()
            raise RemoteError("对方的播放程序没有响应")
        if first is None or not first.startswith("READY "):
            self.close()
            raise RemoteError(self.reason(first))
        self.device = first[6:]

    def reason(self, line=None):
        if line and line.startswith("ERR "):
            return line[4:]
        if self.error:
            return self.error
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.1)  # 等 stderr 读完
        return explain("\n".join(self._err), self.target)

    def _read_out(self):
        for raw in self.proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            if line.startswith("ERR "):
                self.error = line[4:]
            self.lines.put(line)
        self.lines.put(None)

    def _read_err(self):
        for raw in self.proc.stderr:
            self._err.append(raw.decode(errors="replace").rstrip("\n"))

    def _write(self):
        while True:
            chunk = self._audio.get()
            if chunk is None:
                break
            try:
                self.proc.stdin.write(chunk)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                break

    def write(self, chunk):
        while True:
            try:
                return self._audio.put_nowait(chunk)
            except queue.Full:
                try:
                    self._audio.get_nowait()
                except queue.Empty:
                    pass

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()  # 对方的 vox_play 读到输入结束后自行退出
        self.write(None)


def handle(ws):
    """一个 WebSocket 连接 = 一次操作：列设备，或持续送音频。"""
    session = None

    def send(**m):
        try:
            ws.send(json.dumps(m, ensure_ascii=False))
        except Exception:
            pass

    try:
        m = json.loads(ws.recv(timeout=15))
        if m.get("type") == "devices":
            send(type="devices", devices=list_devices(m.get("target"), lambda s: send(type="log", message=s)))
        elif m.get("type") == "open":
            rate = int(m.get("rate", 0))
            session = Session(m.get("target"), m.get("device"), rate)
            send(type="ready", device=session.device)

            def report():
                while True:
                    line = session.lines.get()
                    if line is None:
                        break
                    if line.startswith("STAT "):
                        send(type="stat", **{k: float(v) for k, v in (p.split("=") for p in line[5:].split())})
                send(type="error", message=session.reason())
                ws.close()

            threading.Thread(target=report, daemon=True).start()
            for data in ws:
                if isinstance(data, bytes):
                    session.write(data)
    except RemoteError as e:
        send(type="error", message=str(e))
    except Exception as e:
        if type(e).__name__ not in ("ConnectionClosedOK", "ConnectionClosedError"):
            print("relay:", type(e).__name__, e, flush=True)
            send(type="error", message=f"{type(e).__name__}: {e}")
    finally:
        if session:
            session.close()


def start(host, port, origins):
    """只接受本页面发来的连接（按 Origin 判断），防止别的网页往会议里塞声音。"""
    server = serve(handle, host, port, origins=origins, compression=None, max_size=1 << 16)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def check(a):
    cfg = config.raw()["remote"]
    target = a.target or cfg["target"]
    wanted = a.device or cfg["device"]
    if not target:
        sys.exit("请指定地址: ./start.sh check 用户名@内网地址")
    print(f"[1/4] SSH 登录 {target}")
    print(run_ssh(target, 'echo "  macOS $(sw_vers -productVersion 2>/dev/null) $(uname -m)"').rstrip())
    print("[2/4] 编译并拷贝播放程序")
    deploy(target, lambda s: print("  " + s))
    print("[3/4] 对方的声卡")
    found = list_devices(target)
    for d in found:
        print(f"  - {d['name']}  输出{d['outputs']} 输入{d['inputs']}{'  [虚拟]' if d['virtual'] else ''}")
    if not any(d["outputs"] and (d["name"] == wanted or wanted.lower() in d["name"].lower()) for d in found):
        sys.exit(f"对方没有名为“{wanted}”的输出设备。装好虚拟声卡（如 BlackHole 2ch）后重试，或用 --device 指定。")
    print(f"[4/4] 向“{wanted}”送 5 秒{'测试音' if a.tone else '静音'}")
    rate = 48000
    s = Session(target, wanted, rate)
    try:
        t0 = time.time()
        for i in range(250):  # 按实时速度送，每块 20 ms
            samples = (int(6000 * math.sin(2 * math.pi * 440 * (i * 960 + k) / rate)) if a.tone and (i // 25) % 2 == 0 else 0 for k in range(960))
            s.write(b"".join(v.to_bytes(2, "little", signed=True) for v in samples))
            time.sleep(max(0, t0 + (i + 1) * 0.02 - time.time()))
        stats = []
        while not s.lines.empty():
            line = s.lines.get()
            if line:
                stats.append(line)
        for line in stats:
            print("  " + line)
        if s.proc.poll() is not None or not any(l.startswith("STAT ") for l in stats):
            sys.exit("没有跑通：" + s.reason(stats[-1] if stats else None))
    finally:
        s.close()
    print("通过。回到页面把“声音送到”选成“内网另一台 Mac”即可。")
