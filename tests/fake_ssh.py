#!/usr/bin/env python3
"""测试用的假 ssh：不连网络，模拟一台装了 BlackHole 的 Mac；收到的音频写进 $VOXRELAY_FAKE_DIR。"""
import json, os, sys, time

args = sys.argv[1:]
while args and args[0].startswith("-"):
    args = args[2:] if args[0] == "-o" else args[1:]
target, command = args[0], args[1]
OUT = os.environ.get("VOXRELAY_FAKE_DIR", ".")
with open(os.path.join(OUT, "fake_ssh.log"), "a", encoding="utf-8") as log:
    log.write(f"{target} :: {command}\n")
if target.endswith("nokey"):
    sys.stderr.write("user@nokey: Permission denied (publickey,password).\n")
    sys.exit(255)
if command.startswith("uname -s"):
    print("Darwin\nHAVE")
elif command.endswith("--list"):
    print(json.dumps([{"name": "MacBook 扬声器", "uid": "spk", "outputs": 2, "inputs": 0, "virtual": False},
                      {"name": "BlackHole 2ch", "uid": "bh", "outputs": 2, "inputs": 2, "virtual": True}], ensure_ascii=False))
elif "--device" in command:
    if "BlackHole" not in command:
        print("ERR 找不到输出设备。现有：BlackHole 2ch")
        sys.exit(1)
    print("READY BlackHole 2ch", flush=True)
    total, last = 0, time.time()
    with open(os.path.join(OUT, "received.pcm"), "wb") as f:
        while True:
            data = os.read(0, 8192)
            if not data:
                break
            f.write(data)
            f.flush()
            total += len(data)
            if time.time() - last >= 1:
                last = time.time()
                print(f"STAT buffer_ms=62 played_s={total / 96000:.1f} underruns=0 dropped_ms=0", flush=True)
else:
    sys.exit("unexpected: " + command)
