"""
ESP-12F 测试脚本：启动 TCP 服务端 → 等 ESP-12F 连接 → 发 JSON → 打印回复
用法: python tools/send.py
"""
import socket, sys, time, json as _json

PORT = 8888
CMDS = [
    '{"t":"set_panel","id":1,"mode":1}',   # 面板1除尘
    '{"t":"set_panel","id":2,"mode":2}',   # 面板2复位
    '{"t":"selftest"}',                     # 自检
    '{"t":"task","id":1}',                  # 任务1
]

def main():
    # 获取本机 IP
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except:
        ip = "?.?.?.?"
    finally:
        s.close()

    # 启动 TCP 服务端
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PORT))
    server.listen(1)
    server.settimeout(1.0)

    print(f"本机IP: {ip}  监听端口: {PORT}")
    print(f"→ ESP-12F 的 config.h 里 SERVER_IP 填: {ip}")
    print(f"→ 等待 ESP-12F 连接...")
    print()

    client = None
    while client is None:
        try:
            client, addr = server.accept()
            print(f"ESP-12F 已连接: {addr}")
        except socket.timeout:
            pass

    client.settimeout(2.0)

    # 发测试命令
    for i, cmd in enumerate(CMDS):
        print(f"\n[{i+1}] 发送: {cmd}")
        client.sendall((cmd + "\n").encode())

        # 等回复
        try:
            data = client.recv(4096)
            if data:
                for line in data.decode(errors="replace").split("\n"):
                    line = line.strip()
                    if line and line != '{"t":"hb"}':
                        try:
                            obj = _json.loads(line)
                            print(f"    收到: {_json.dumps(obj, ensure_ascii=False)}")
                        except:
                            print(f"    收到: {line}")
            else:
                print(f"    断开")
                break
        except socket.timeout:
            print(f"    (无回复)")

        time.sleep(0.5)

    print(f"\n测试完成。保持连接，按 Ctrl+C 退出...")
    try:
        while True:
            try:
                data = client.recv(4096)
                if data:
                    for line in data.decode(errors="replace").split("\n"):
                        line = line.strip()
                        if line and line != '{"t":"hb"}':
                            print(f"收到: {line}")
                else:
                    print("ESP-12F 断开")
                    break
            except socket.timeout:
                pass
    except KeyboardInterrupt:
        print("退出")
    finally:
        client.close()
        server.close()

if __name__ == "__main__":
    main()
