"""入口: python -m voxrelay [serve [--port N] | check [用户名@地址] [--device 名称] [--tone]]"""
import argparse, sys


def main():
    P = argparse.ArgumentParser(prog="voxrelay")
    sub = P.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="启动网页服务（默认）")
    s.add_argument("--port", type=int, help="页面端口，远程声卡通道用它加 1")
    c = sub.add_parser("check", help="检查能否把声音送到另一台 Mac")
    c.add_argument("target", nargs="?", help="用户名@内网地址，缺省用 config.json 里的 remote.target")
    c.add_argument("--device", help="对方的输出设备名")
    c.add_argument("--tone", action="store_true", help="送测试音（会被会议里的人听到，开会时别用）")
    a = P.parse_args()
    if a.cmd == "check":
        from . import relay
        try:
            relay.check(a)
        except relay.RemoteError as e:
            sys.exit("失败：" + str(e))
    else:
        from . import server
        server.serve(getattr(a, "port", None))


if __name__ == "__main__":
    main()
