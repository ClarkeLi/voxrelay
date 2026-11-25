"""远程声卡通道测试：用假 ssh 模拟对方机器，不联网，也不需要声卡、模型和编译器。
运行: python tests/test_relay.py （需要 websockets>=11）
"""
import array, json, os, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix="voxrelay-test-")
os.environ["VOXRELAY_SSH"] = os.path.join(HERE, "fake_ssh.py")
os.environ["VOXRELAY_FAKE_DIR"] = TMP
sys.path.insert(0, os.path.dirname(HERE))
from websockets.sync.client import connect  # noqa: E402
from voxrelay import relay  # noqa: E402

OK = "http://127.0.0.1:9881"
relay.build = lambda: (__file__, "vox_play-test")  # 假 ssh 不会真的拷贝，免去编译
server = relay.start("127.0.0.1", 0, [OK])
URL = f"ws://127.0.0.1:{server.socket.getsockname()[1]}"


def ask(message, origin=OK):
    with connect(URL, origin=origin, open_timeout=5) as ws:
        ws.send(json.dumps(message))
        return json.loads(ws.recv(timeout=15))


def rejected(origin):
    try:
        ask({"type": "devices", "target": "user@mock"}, origin)
    except Exception as e:
        return "403" in str(e)
    return False


def opening(target, device="BlackHole 2ch"):
    return ask({"type": "open", "target": target, "device": device, "rate": 48000})


assert rejected("http://evil.example"), "其他网页的连接应被拒绝"
assert rejected(None), "没有 Origin 的连接应被拒绝"
for bad in ("-oProxyCommand=true", "a b", "user@host;ls", ""):
    r = ask({"type": "devices", "target": bad})
    assert r["type"] == "error" and "地址格式" in r["message"], (bad, r)
r = ask({"type": "devices", "target": "user@mock"})
assert [d["name"] for d in r["devices"]][-1] == "BlackHole 2ch", r
assert "ssh-copy-id" in opening("user@nokey")["message"]
assert "找不到输出设备" in opening("user@mock", "x'; echo hi #")["message"]
with connect(URL, origin=OK) as ws:
    ws.send(json.dumps({"type": "open", "target": "user@mock", "device": "BlackHole 2ch", "rate": 48000}))
    assert json.loads(ws.recv(timeout=15))["type"] == "ready"
    sent = b""
    for i in range(80):
        chunk = array.array("h", [(i * 960 + k) % 30000 - 15000 for k in range(960)]).tobytes()
        ws.send(chunk)
        sent += chunk
        time.sleep(0.02)
    assert json.loads(ws.recv(timeout=5))["type"] == "stat"
    time.sleep(0.5)
with open(os.path.join(TMP, "received.pcm"), "rb") as f:
    assert f.read() == sent, "送达的数据应与发出的逐字节一致"
server.shutdown()
print("通过：来源限制、地址校验、列设备、错误提示、音频传输")
