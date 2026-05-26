"""
ESP-12F 测试脚本：启动 TCP 服务端 → 等 ESP-12F 连接 → 发 JSON → 打印回复

用法:
  py tools/send.py          自动跑完全部测试命令后进入交互模式
  py tools/send.py -i       跳过自动测试，直接进入交互模式
  py tools/send.py -p 8888  指定端口

交互模式命令:
  panel <id> <mode>  id=1~4, mode=1除尘/2复位
  alarm              报警灯 PA.0 高电平2秒
  selftest           系统自检
  task <id>          切换任务 id=1~7
  cmd <idx>          命令索引 idx=1~8
  shutdown           系统关停
  raw <json>         发送任意 JSON
  h                  帮助
  q                  退出
"""
import socket, sys, time, json as _json, threading

PORT = 8888

# 全覆盖自动测试
AUTO_CMDS = [
    '{"t":"set_panel","id":1,"mode":1}',   # 面板1除尘
    '{"t":"set_panel","id":2,"mode":2}',   # 面板2复位
    '{"t":"selftest"}',                     # 系统自检
    '{"t":"alarm"}',                        # 报警灯
    '{"t":"task","id":1}',                  # 任务切换
    '{"t":"cmd","idx":3}',                  # 命令索引
    '{"t":"shutdown"}',                     # 系统关停
    '{"t":"set_panel","id":1,"mode":1}',   # 恢复面板1除尘
]

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]
    except:
        ip = "?.?.?.?"
    finally:
        s.close()
    return ip

def send_cmd(client, cmd):
    print(f"\n>>> {cmd}")
    client.sendall((cmd + "\n").encode())
    time.sleep(0.3)
    try:
        data = client.recv(4096)
        if data:
            for line in data.decode(errors="replace").split("\n"):
                line = line.strip()
                if line and line != '{"t":"hb"}':
                    try:
                        obj = _json.loads(line)
                        print(f"<<< {_json.dumps(obj, ensure_ascii=False)}")
                    except:
                        print(f"<<< {line}")
        else:
            print("<<< (断开)")
            return False
    except socket.timeout:
        print("<<< (无回复)")
    return True

def interactive(client):
    print("\n交互模式 (h=帮助 q=退出)")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line: continue
        parts = line.split()
        c = parts[0].lower()

        if c in ('q', 'quit', 'exit'): break
        if c in ('h', 'help', '?'):
            print("panel <id> <mode> | alarm | selftest | task <id> | cmd <idx> | shutdown | raw <json> | q")
            continue
        if c == 'panel' and len(parts) >= 3:
            cmd = '{"t":"set_panel","id":' + parts[1] + ',"mode":' + parts[2] + '}'
        elif c == 'alarm':
            cmd = '{"t":"alarm"}'
        elif c == 'selftest':
            cmd = '{"t":"selftest"}'
        elif c == 'task' and len(parts) >= 2:
            cmd = '{"t":"task","id":' + parts[1] + '}'
        elif c == 'cmd' and len(parts) >= 2:
            cmd = '{"t":"cmd","idx":' + parts[1] + '}'
        elif c == 'shutdown':
            cmd = '{"t":"shutdown"}'
        elif c == 'raw' and len(parts) >= 2:
            cmd = line[4:]
        else:
            print(f"未知命令: {c}")
            continue

        if not send_cmd(client, cmd):
            print("连接断开")
            break

def main():
    ip = get_ip()
    interactive_only = "-i" in sys.argv
    port = PORT
    for i, a in enumerate(sys.argv):
        if a == "-p" and i+1 < len(sys.argv):
            port = int(sys.argv[i+1])

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", port))
    server.listen(1)
    server.settimeout(1.0)

    print(f"本机IP: {ip}  端口: {port}")
    print(f"→ ESP-12F config.h: SERVER_IP={ip}  SERVER_PORT={port}")
    print(f"→ 等待 ESP-12F 连接...")
    if not interactive_only:
        print(f"→ 连接后将自动发送 {len(AUTO_CMDS)} 条测试命令")

    client = None
    while client is None:
        try:
            client, addr = server.accept()
            print(f"\nESP-12F 已连接: {addr}")
        except socket.timeout:
            pass

    client.settimeout(1.0)

    if not interactive_only:
        print("=" * 50)
        for i, cmd in enumerate(AUTO_CMDS):
            print(f"[{i+1}/{len(AUTO_CMDS)}]", end="")
            if not send_cmd(client, cmd):
                break
        print("=" * 50)

    interactive(client)
    client.close()
    server.close()
    print("退出")

if __name__ == "__main__":
    main()
