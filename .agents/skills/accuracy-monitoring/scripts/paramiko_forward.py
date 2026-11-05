#!/usr/bin/env python3
# paramiko_forward.py —— SSH 本地端口转发（paramiko 实现，免 ssh client/sshpass——铁律 9）
# 由 webui_control.sh tunnel 子命令 daemonize 调用：
#   本地 <local-bind>:<local-port> --SSH(paramiko)--> 目标节点回环 127.0.0.1:<target-port>
# 凭据：--pass-file 路径引用（转发器自行读取，密码本体不入 argv/日志——铁律 10）或 --key-path 私钥。
# host key：AutoAddPolicy 自动接受（等价 StrictHostKeyChecking=accept-new）；keepalive 30s。
# --forward 参数仅承载转发特征串 "<local-port>:127.0.0.1:<target-port>"，供 tunnel --stop 进程扫描兜底。
import argparse
import os
import select
import socketserver
import sys


def read_password(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def make_handler(transport, target):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                chan = transport.open_channel(
                    "direct-tcpip", target, self.request.getpeername())
            except Exception as exc:
                print(f"[forward] open_channel failed: {exc}", file=sys.stderr, flush=True)
                return
            if chan is None:
                print("[forward] open_channel returned None", file=sys.stderr, flush=True)
                return
            try:
                while True:
                    r, _, _ = select.select([self.request, chan], [], [])
                    if self.request in r:
                        data = self.request.recv(65536)
                        if not data:
                            break
                        chan.sendall(data)
                    if chan in r:
                        data = chan.recv(65536)
                        if not data:
                            break
                        self.request.sendall(data)
            except Exception:
                pass
            finally:
                for c in (chan, self.request):
                    try:
                        c.close()
                    except Exception:
                        pass
    return Handler


class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="paramiko local port forwarder")
    ap.add_argument("--host", required=True, help="SSH 服务器（被测节点）")
    ap.add_argument("--port", type=int, default=22, help="SSH 端口")
    ap.add_argument("--user", default="root", help="SSH 用户名")
    ap.add_argument("--pass-file", help="密码文件路径（引用，转发器自行读取）")
    ap.add_argument("--key-path", help="SSH 私钥路径")
    ap.add_argument("--local-bind", default="0.0.0.0", help="本地监听地址")
    ap.add_argument("--local-port", type=int, required=True, help="本地监听端口")
    ap.add_argument("--target-host", default="127.0.0.1", help="远端转发目标地址")
    ap.add_argument("--target-port", type=int, required=True, help="远端转发目标端口")
    ap.add_argument("--forward", default="", help="转发特征串（--stop 扫描用，无功能作用）")
    args = ap.parse_args()

    try:
        import paramiko
    except ImportError:
        print("错误：paramiko 未安装（由用户安装后重跑：python -m pip install paramiko）",
              file=sys.stderr)
        sys.exit(6)

    if not args.pass_file and not args.key_path:
        print("错误：需要 --pass-file 或 --key-path", file=sys.stderr)
        sys.exit(2)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = dict(
        hostname=args.host,
        port=args.port,
        username=args.user,
        allow_agent=False,
        look_for_keys=False,
        timeout=15,
    )
    try:
        if args.pass_file:
            connect_kwargs["password"] = read_password(args.pass_file)
        else:
            connect_kwargs["key_filename"] = os.path.expanduser(args.key_path)
        client.connect(**connect_kwargs)
    except Exception as exc:
        print(f"错误：SSH 连接/认证失败: {exc}", file=sys.stderr)
        sys.exit(1)

    transport = client.get_transport()
    if transport is None or not transport.is_active():
        print("错误：SSH 传输未建立", file=sys.stderr)
        sys.exit(1)
    transport.set_keepalive(30)

    print(f"[forward] up {args.local_bind}:{args.local_port} -> "
          f"{args.host}:{args.port} -> {args.target_host}:{args.target_port}",
          file=sys.stderr, flush=True)
    try:
        ThreadingTCPServer(
            (args.local_bind, args.local_port),
            make_handler(transport, (args.target_host, args.target_port)),
        ).serve_forever()
    except OSError as exc:
        print(f"错误：本地端口监听失败 {args.local_bind}:{args.local_port}: {exc}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
