#!/usr/bin/env python3
"""
ESP-12F WiFi 桥接测试服务端
=============================

在电脑上启动 TCP 服务端，等待 ESP-12F 连接上来，然后：
  - 实时显示 ESP-12F 发来的数据（面板状态 / 命令确认 / 心跳等）
  - 可交互输入 JSON 命令发送给 ESP-12F，ESP-12F 会封装协议帧发给 MCU

通信角色:
  电脑(本脚本) ←→ TCP服务端 ← WiFi → ESP-12F(TCP客户端) ← UART → MCU

  本脚本是 TCP SERVER（监听），ESP-12F 是 TCP CLIENT（连接）。

用法:
  pip install pyserial           # 如果还没装
  py tools/test_client.py        # 启动服务端，等待 ESP-12F 连接
  py tools/test_client.py --port 8888         # 指定监听端口
  py tools/test_client.py --panel 1 1         # 等连接后自动发送面板1除尘
  py tools/test_client.py --selftest          # 等连接后自动发送自检命令

对应 ESP-12F 的 config.h 配置:
  #define SERVER_IP   "电脑的IP"    // 本机 IP
  #define SERVER_PORT 8888          // 与本脚本 --port 一致
"""

import socket
import sys
import argparse
import select
import threading

# ============ 默认配置 ============
DEFAULT_BIND = "0.0.0.0"  # 监听所有网卡
DEFAULT_PORT = 8888

# ============ 预设命令 ============
PRESETS = {
    "panel_1_clean":   '{"t":"set_panel","id":1,"mode":1}',
    "panel_1_reset":   '{"t":"set_panel","id":1,"mode":2}',
    "panel_2_clean":   '{"t":"set_panel","id":2,"mode":1}',
    "panel_2_reset":   '{"t":"set_panel","id":2,"mode":2}',
    "panel_3_clean":   '{"t":"set_panel","id":3,"mode":1}',
    "panel_3_reset":   '{"t":"set_panel","id":3,"mode":2}',
    "panel_4_clean":   '{"t":"set_panel","id":4,"mode":1}',
    "panel_4_reset":   '{"t":"set_panel","id":4,"mode":2}',
    "selftest":        '{"t":"selftest"}',
    "task_1":          '{"t":"task","id":1}',
    "task_2":          '{"t":"task","id":2}',
    "task_3":          '{"t":"task","id":3}',
    "cmd_1":           '{"t":"cmd","idx":1}',
    "cmd_2":           '{"t":"cmd","idx":2}',
    "cmd_3":           '{"t":"cmd","idx":3}',
    "shutdown":        '{"t":"shutdown"}',
}

# ============ 帧协议常量 ============
FRAME_SYNC = 0xAA
FRAME_MAX_PAYLOAD = 512

TYPE_NAMES = {
    0x11: "PANEL_STATUS",
    0x12: "FAULT",
    0x13: "SELFTEST_R",
    0x14: "ACK",
    0x20: "SET_PANEL",
    0x21: "SELFTEST",
    0x22: "TASK_SWITCH",
    0x23: "CMD_INDEX",
    0x24: "SHUTDOWN",
}


def pack_frame(frame_type: int, payload: str) -> bytes:
    """将 JSON payload 封装为协议帧 (与 ESP-12F protocol.cpp packFrame 一致)"""
    data = payload.encode("utf-8")
    length = len(data)
    if length > FRAME_MAX_PAYLOAD:
        raise ValueError(f"payload too long: {length} > {FRAME_MAX_PAYLOAD}")

    checksum = frame_type
    checksum ^= (length >> 8) & 0xFF
    checksum ^= length & 0xFF
    for b in data:
        checksum ^= b

    frame = bytearray()
    frame.append(FRAME_SYNC)
    frame.append(frame_type)
    frame.append((length >> 8) & 0xFF)
    frame.append(length & 0xFF)
    frame.extend(data)
    frame.append(checksum & 0xFF)
    return bytes(frame)


def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def detect_type(json_str: str) -> int:
    if '"set_panel"' in json_str:
        return 0x20
    if '"selftest"' in json_str:
        return 0x21
    if '"task"' in json_str:
        return 0x22
    if '"cmd"' in json_str:
        return 0x23
    if '"shutdown"' in json_str:
        return 0x24
    return 0


def get_local_ip():
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "?.?.?.?"


# ===================================================================
#  TCP 服务端 — 等待 ESP-12F 连接，然后交互
# ===================================================================
class BridgeServer:
    def __init__(self, bind_addr: str, port: int):
        self.bind_addr = bind_addr
        self.port = port
        self.sock = None
        self.client = None
        self.client_addr = None
        self.running = False
        self.send_queue = []  # 待发送命令队列

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.bind_addr, self.port))
        self.sock.listen(1)
        self.sock.settimeout(1.0)  # 允许主循环中断
        self.running = True

        local_ip = get_local_ip()
        print(f"  TCP 服务端已启动")
        print(f"  监听地址: {local_ip}:{self.port}")
        print(f"  等待 ESP-12F 连接 ...")
        print(f"  (ESP-12F config.h 中 SERVER_IP 应设为 {local_ip})")
        print()

    def stop(self):
        self.running = False
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def accept(self):
        """尝试接受一个客户端连接"""
        try:
            client, addr = self.sock.accept()
            self.client = client
            self.client_addr = addr
            client.settimeout(0.1)  # 非阻塞读取
            print(f">>> ESP-12F 已连接: {addr[0]}:{addr[1]}")
            # 发送队列中的待发送命令
            for json_str in self.send_queue:
                self._send_json(json_str)
            self.send_queue.clear()
            return True
        except socket.timeout:
            return False

    def _send_json(self, json_str: str):
        data = (json_str + "\n").encode("utf-8")
        try:
            self.client.sendall(data)
            ft = detect_type(json_str)
            type_name = TYPE_NAMES.get(ft, "?")
            frame = pack_frame(ft, json_str) if ft else b""
            print(f"  -> JSON ({len(data)-1}B): {json_str}")
            if frame:
                print(f"     UART帧 ({len(frame)}B): {format_hex(frame)}  type=0x{ft:02X}({type_name})")
        except Exception as e:
            print(f"  !! 发送失败: {e}")

    def send(self, json_str: str):
        if self.client:
            self._send_json(json_str)
        else:
            self.send_queue.append(json_str)
            print(f"  (ESP-12F 未连接，命令已缓存，连接后自动发送)")

    def recv(self):
        """读取客户端发来的数据并打印"""
        if not self.client:
            return False
        try:
            data = self.client.recv(4096)
            if not data:
                print(f"<<< ESP-12F 已断开: {self.client_addr}")
                try:
                    self.client.close()
                except Exception:
                    pass
                self.client = None
                self.client_addr = None
                print(f"  等待 ESP-12F 重新连接 ...")
                return False
            # 解析并打印收到的数据
            for line in data.decode("utf-8", errors="replace").split("\n"):
                line = line.strip()
                if not line:
                    continue
                # 心跳不重复打印
                if line == '{"t":"hb"}':
                    print(f"  <- 心跳", end="\r")
                    continue
                print(f"  <- JSON: {line}")
                # 展开面板状态
                if '"t":"panels"' in line:
                    try:
                        import json
                        obj = json.loads(line)
                        for p in obj.get("p", []):
                            st_text = {0: "空闲", 1: "除尘中", 2: "复位中"}.get(p.get("st"), "?")
                            print(f"       面板{p['id']}: {st_text}  ts={p.get('ts', '?')}")
                    except Exception:
                        pass
            return True
        except socket.timeout:
            return True  # 正常超时
        except Exception as e:
            print(f"  !! 读取错误: {e}")
            return False


# ===================================================================
#  交互模式
# ===================================================================
def run_interactive(server: BridgeServer):
    print("  输入命令发送给 ESP-12F -> MCU，或输入 help 查看帮助")
    print("  (ESP-12F 发来的数据会自动显示)")
    print()

    while server.running:
        # ---- 接受连接 ----
        if not server.client:
            server.accept()

        # ---- 读取数据 ----
        server.recv()

        # ---- 用户输入 (非阻塞) ----
        if sys.platform == "win32":
            import msvcrt
            if msvcrt.kbhit():
                line = input()
                handle_input(server, line)
        else:
            r, _, _ = select.select([sys.stdin], [], [], 0.5)
            if r:
                line = sys.stdin.readline()
                if line:
                    handle_input(server, line.strip())
                else:
                    break  # EOF

        # 小延迟防止 CPU 空转
        # (socket timeout already provides the delay)


def handle_input(server: BridgeServer, line: str):
    if not line:
        return

    parts = line.split()
    cmd = parts[0].lower()

    # ---- 退出 ----
    if cmd in ("quit", "exit", "q"):
        server.stop()
        print("  退出")
        return

    # ---- 帮助 ----
    if cmd in ("help", "h", "?"):
        print("""
  交互命令 (发送给 ESP-12F -> MCU):
    panel <id> <mode>   设置面板模式 (id=1~4, mode=1除尘/2复位)
    selftest            系统自检
    task <id>           切换任务 (1~7)
    cmd <idx>           命令索引 (1~8)
    shutdown            系统关停
    raw <json>          发送原始 JSON

  快捷命令:
    p1c -> 面板1除尘    p1r -> 面板1复位
    p2c -> 面板2除尘    p2r -> 面板2复位
    p3c -> 面板3除尘    p3r -> 面板3复位
    p4c -> 面板4除尘    p4r -> 面板4复位
    st  -> 自检         sd  -> 关停

    quit / exit / q     退出
    status              查看连接状态
""")
        return

    # ---- 查看状态 ----
    if cmd == "status":
        if server.client:
            print(f"  已连接: {server.client_addr}")
        else:
            print(f"  未连接，等待 ESP-12F ...")
        print(f"  监听端口: {server.port}")
        return

    # ---- 预设快捷命令 ----
    if cmd in PRESETS:
        server.send(PRESETS[cmd])
        return

    # ---- 面板 ----
    if cmd == "panel":
        if len(parts) < 3:
            print("  用法: panel <id 1~4> <mode 1除尘/2复位>")
            return
        json_str = '{"t":"set_panel","id":' + parts[1] + ',"mode":' + parts[2] + '}'
        server.send(json_str)
        return

    # ---- 自检 ----
    if cmd == "selftest":
        server.send(PRESETS["selftest"])
        return

    # ---- 任务 ----
    if cmd == "task":
        if len(parts) < 2:
            print("  用法: task <id 1~7>")
            return
        server.send('{"t":"task","id":' + parts[1] + '}')
        return

    # ---- 命令索引 ----
    if cmd == "cmd":
        if len(parts) < 2:
            print("  用法: cmd <idx 1~8>")
            return
        server.send('{"t":"cmd","idx":' + parts[1] + '}')
        return

    # ---- 关停 ----
    if cmd == "shutdown":
        server.send(PRESETS["shutdown"])
        return

    # ---- 原始 JSON ----
    if cmd == "raw":
        json_str = line[4:]
        if json_str:
            server.send(json_str)
        else:
            print("  用法: raw <json字符串>")
        return

    # ---- 显示帧格式 ----
    if cmd == "frame":
        if len(parts) < 3:
            print("  用法: frame <type_hex> <json>")
            print("  例如: frame 20 {\\\"t\\\":\\\"set_panel\\\",\\\"id\\\":1,\\\"mode\\\":1}")
            return
        try:
            ft = int(parts[1], 16)
        except ValueError:
            print("  type 必须是十六进制数，如 20")
            return
        json_str = " ".join(parts[2:])
        frame = pack_frame(ft, json_str)
        print(f"  协议帧 ({len(frame)}B): {format_hex(frame)}")
        print(f"  SYNC=AA TYPE={ft:02X}({TYPE_NAMES.get(ft, '?')}) "
              f"LEN={len(json_str):04X} CHECKSUM={frame[-1]:02X}")
        return

    # ---- 未知命令 ----
    print(f"  未知命令: {cmd} (输入 help 查看帮助)")
    suggestions = [k for k in PRESETS if k.startswith(cmd)]
    if suggestions:
        print(f"  你是不是想输入: {', '.join(suggestions)}")


# ===================================================================
#  非交互模式: 发送单条命令
# ===================================================================
def run_one_shot(server: BridgeServer, json_str: str):
    import time
    # 等 ESP-12F 连接
    print("  等待 ESP-12F 连接 ...")
    wait_start = time.time()
    while not server.client and (time.time() - wait_start) < 30:
        server.accept()
        time.sleep(0.1)
    if not server.client:
        print("  !! 超时: ESP-12F 未在 30s 内连接")
        return

    server.send(json_str)
    # 再等一会儿看回复
    deadline = time.time() + 3
    while time.time() < deadline:
        server.recv()
        time.sleep(0.1)

    server.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ESP-12F WiFi 桥接测试服务端 (TCP Server)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  py tools/test_client.py                        启动服务端，交互模式
  py tools/test_client.py --port 8888            指定端口
  py tools/test_client.py --panel 1 1            等待连接后自动发送面板1除尘
  py tools/test_client.py --selftest             等待连接后自动发送自检

ESP-12F 的 config.h 应配置:
  #define SERVER_IP   "本机局域网IP"    // 脚本启动时会显示
  #define SERVER_PORT 8888
        """,
    )
    parser.add_argument("--bind", default=DEFAULT_BIND, help=f"监听地址 (默认: {DEFAULT_BIND}=所有网卡)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口 (默认: {DEFAULT_PORT})")
    parser.add_argument("--panel", nargs=2, metavar=("ID", "MODE"), help="面板命令: ID(1~4) MODE(1除尘/2复位)")
    parser.add_argument("--selftest", action="store_true", help="发送自检命令")
    parser.add_argument("--task", type=int, metavar="ID", help="切换任务: ID(1~7)")
    parser.add_argument("--cmd", type=int, metavar="IDX", help="命令索引: IDX(1~8)")
    parser.add_argument("--shutdown", action="store_true", help="发送关停命令")
    parser.add_argument("--raw", type=str, metavar="JSON", help="发送原始 JSON 字符串")
    parser.add_argument("--frame", nargs=2, metavar=("TYPE_HEX", "JSON"), help="仅显示协议帧格式 (不启动服务)")

    args = parser.parse_args()

    # ---- --frame 只看帧格式,不启动服务 ----
    if args.frame:
        try:
            ft = int(args.frame[0], 16)
        except ValueError:
            print("错误: type 必须是十六进制数")
            sys.exit(1)
        json_str = args.frame[1]
        frame = pack_frame(ft, json_str)
        print(f"协议帧 ({len(frame)}B): {format_hex(frame)}")
        print(f"  SYNC=AA TYPE={ft:02X}({TYPE_NAMES.get(ft, '?')}) "
              f"LEN={len(json_str):04X} CHECKSUM={frame[-1]:02X}")
        sys.exit(0)

    # ---- 构建要发送的命令 ----
    one_shot_json = None
    if args.panel:
        panel_id, mode = args.panel
        one_shot_json = '{"t":"set_panel","id":' + panel_id + ',"mode":' + mode + '}'
    elif args.selftest:
        one_shot_json = PRESETS["selftest"]
    elif args.task:
        one_shot_json = '{"t":"task","id":' + str(args.task) + '}'
    elif args.cmd:
        one_shot_json = '{"t":"cmd","idx":' + str(args.cmd) + '}'
    elif args.shutdown:
        one_shot_json = PRESETS["shutdown"]
    elif args.raw:
        one_shot_json = args.raw

    # ---- 启动服务 ----
    server = BridgeServer(args.bind, args.port)
    server.start()

    try:
        if one_shot_json:
            # 非交互模式
            server.send_queue.append(one_shot_json)
            run_one_shot(server, one_shot_json)
        else:
            # 交互模式
            run_interactive(server)
    except KeyboardInterrupt:
        print("\n  退出")
    finally:
        server.stop()
