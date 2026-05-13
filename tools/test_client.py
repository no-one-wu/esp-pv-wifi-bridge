#!/usr/bin/env python3
"""
ESP-12F WiFi 桥接测试客户端
=============================

模拟前端服务器，通过 TCP 向 ESP-12F 发送 JSON 命令，用于验证：
  - ESP-12F 是否正确接收 TCP 数据
  - ESP-12F 是否正确封装协议帧发往 MCU
  - 协议帧格式是否正确（用 USB-TTL 监听 ESP-12F TX 引脚 GPIO1 可观察）

用法:
  py tools/test_client.py                        # 交互模式
  py tools/test_client.py --host 192.168.1.100   # 指定 ESP-12F IP
  py tools/test_client.py --port 8888            # 指定端口
  py tools/test_client.py --panel 1 1            # 直接发送面板除尘命令
  py tools/test_client.py --selftest             # 直接发送自检命令
  py tools/test_client.py --shutdown             # 直接发送关停命令

硬件监听方法:
  用 USB-TTL 模块连接 ESP-12F 的 GPIO1 (TXD) 和 GND，
  打开串口助手 (115200 8N1)，即可看到 ESP-12F 发给 MCU 的协议帧。
"""

import socket
import sys
import argparse
import time

# ============ 默认配置 ============
DEFAULT_HOST = "192.168.1.100"
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

# 消息类型
TYPE_SET_PANEL    = 0x20
TYPE_SELFTEST     = 0x21
TYPE_TASK_SWITCH  = 0x22
TYPE_CMD_INDEX    = 0x23
TYPE_SHUTDOWN     = 0x24

TYPE_PANEL_STATUS = 0x11
TYPE_FAULT        = 0x12
TYPE_SELFTEST_R   = 0x13
TYPE_ACK          = 0x14

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
        raise ValueError(f"payload 过长: {length} > {FRAME_MAX_PAYLOAD}")

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


def unpack_frame(byte_iter) -> dict:
    """从字节流解析一个协议帧 (模拟 MCU wifi_driver.c 的接收逻辑)"""
    state = 0  # 0=WAIT_SYNC, 1=WAIT_TYPE, 2=WAIT_LEN_HI, 3=WAIT_LO, 4=PAYLOAD, 5=CHECKSUM
    frame_type = 0
    length = 0
    payload = bytearray()
    checksum_calc = 0
    payload_idx = 0

    for b in byte_iter:
        if state == 0:
            if b == FRAME_SYNC:
                state = 1
                checksum_calc = 0
                payload_idx = 0
        elif state == 1:
            frame_type = b
            checksum_calc ^= b
            state = 2
        elif state == 2:
            length = b << 8
            checksum_calc ^= b
            state = 3
        elif state == 3:
            length |= b
            checksum_calc ^= b
            if length > FRAME_MAX_PAYLOAD:
                state = 0
            elif length == 0:
                state = 5
            else:
                state = 4
        elif state == 4:
            payload.append(b)
            checksum_calc ^= b
            payload_idx += 1
            if payload_idx >= length:
                state = 5
        elif state == 5:
            if b == checksum_calc:
                return {
                    "valid": True,
                    "type": frame_type,
                    "type_name": TYPE_NAMES.get(frame_type, "UNKNOWN"),
                    "len": length,
                    "payload": bytes(payload),
                    "json": bytes(payload).decode("utf-8", errors="replace"),
                }
            else:
                return {
                    "valid": False,
                    "calc": checksum_calc,
                    "recv": b,
                }
            # state = 0  # 继续解析下一帧
    return None


def format_frame_hex(frame: bytes) -> str:
    """格式化帧为十六进制字符串"""
    parts = []
    for b in frame:
        parts.append(f"{b:02X}")
    return " ".join(parts)


def send_tcp(host: str, port: int, json_str: str, timeout: float = 5.0):
    """通过 TCP 发送 JSON 到 ESP-12F，并接收回复"""
    print(f"\n  连接 {host}:{port} ...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        sock.connect((host, port))
        print(f"  已连接")

        # 发送 JSON + 换行分隔符
        data = (json_str + "\n").encode("utf-8")
        sock.sendall(data)
        print(f"  -> 已发送 ({len(data)-1} 字节): {json_str}")

        # 同时展示封装的协议帧
        frame_type = detect_type(json_str)
        if frame_type:
            frame = pack_frame(frame_type, json_str)
            print(f"  -> 对应协议帧 ({len(frame)} 字节): {format_frame_hex(frame)}")
            print(f"     SYNC=AA TYPE={frame_type:02X} LEN={len(json_str):04X}")

        # 等待可能的回复
        print(f"  等待回复 ...")
        try:
            response = sock.recv(4096)
            if response:
                print(f"  <- 收到回复 ({len(response)} 字节):")
                # 尝试解析回复内容
                text = response.decode("utf-8", errors="replace")
                for line in text.split("\n"):
                    line = line.strip()
                    if line:
                        print(f"     JSON: {line}")

                # 尝试按协议帧解析
                print(f"     十六进制: {format_frame_hex(response)}")
                result = unpack_frame(response)
                if result:
                    print(f"     帧解析: type=0x{result['type']:02X}({result['type_name']}) "
                          f"len={result['len']} json={result['json']}")
            else:
                print(f"  <- 无回复 (服务器关闭连接)")
        except socket.timeout:
            print(f"  <- 无回复 (超时 {timeout}s)")

        sock.close()
        return True

    except socket.timeout:
        print(f"  ✗ 连接超时 ({timeout}s)")
        return False
    except ConnectionRefusedError:
        print(f"  ✗ 连接被拒绝 (服务器未启动或端口不对)")
        return False
    except Exception as e:
        print(f"  ✗ 错误: {e}")
        return False


def detect_type(json_str: str) -> int:
    """根据 JSON 的 t 字段推断协议帧 type"""
    if '"set_panel"' in json_str:
        return TYPE_SET_PANEL
    if '"selftest"' in json_str:
        return TYPE_SELFTEST
    if '"task"' in json_str:
        return TYPE_TASK_SWITCH
    if '"cmd"' in json_str:
        return TYPE_CMD_INDEX
    if '"shutdown"' in json_str:
        return TYPE_SHUTDOWN
    return 0


def show_help():
    """显示帮助"""
    print("""
===== ESP-12F 测试客户端 =====

交互命令:
  panel <id> <mode>   设置面板模式 (id=1~4, mode=1除尘/2复位)
  selftest            系统自检
  task <id>           切换任务 (1~7)
  cmd <idx>           命令索引 (1~8)
  shutdown            系统关停
  raw <json>          发送原始 JSON
  frame <type> <json> 发送原始协议帧 (type 为十六进制如 20)
  frameraw <hex>      发送原始十六进制数据

  connect <host> <port>  设置连接地址
  quit / exit / q     退出

预设命令快捷输入:
  p1c -> 面板1除尘    p1r -> 面板1复位
  p2c -> 面板2除尘    p2r -> 面板2复位
  p3c -> 面板3除尘    p3r -> 面板3复位
  p4c -> 面板4除尘    p4r -> 面板4复位
  st  -> 自检         sd  -> 关停

当前连接: {host}:{port}
""".format(host=current_host, port=current_port))


def interactive():
    """交互模式"""
    global current_host, current_port

    show_help()

    while True:
        try:
            line = input("TEST> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  退出")
            break

        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        # ---- 退出 ----
        if cmd in ("quit", "exit", "q"):
            break

        # ---- 帮助 ----
        if cmd in ("help", "h", "?"):
            show_help()
            continue

        # ---- 切换连接地址 ----
        if cmd == "connect":
            if len(parts) >= 3:
                current_host = parts[1]
                current_port = int(parts[2])
                print(f"  已设置: {current_host}:{current_port}")
            else:
                print(f"  用法: connect <ip> <port>")
            continue

        # ---- 预设快捷命令 ----
        if cmd in PRESETS:
            json_str = PRESETS[cmd]
            send_tcp(current_host, current_port, json_str)
            continue

        # ---- 面板命令 ----
        if cmd == "panel":
            if len(parts) < 3:
                print("  用法: panel <id 1~4> <mode 1除尘/2复位>")
                continue
            panel_id = parts[1]
            mode = parts[2]
            json_str = '{"t":"set_panel","id":' + panel_id + ',"mode":' + mode + '}'
            send_tcp(current_host, current_port, json_str)
            continue

        # ---- 自检 ----
        if cmd == "selftest":
            send_tcp(current_host, current_port, PRESETS["selftest"])
            continue

        # ---- 任务切换 ----
        if cmd == "task":
            if len(parts) < 2:
                print("  用法: task <id 1~7>")
                continue
            json_str = '{"t":"task","id":' + parts[1] + '}'
            send_tcp(current_host, current_port, json_str)
            continue

        # ---- 命令索引 ----
        if cmd == "cmd":
            if len(parts) < 2:
                print("  用法: cmd <idx 1~8>")
                continue
            json_str = '{"t":"cmd","idx":' + parts[1] + '}'
            send_tcp(current_host, current_port, json_str)
            continue

        # ---- 关停 ----
        if cmd == "shutdown":
            send_tcp(current_host, current_port, PRESETS["shutdown"])
            continue

        # ---- 原始 JSON ----
        if cmd == "raw":
            json_str = line[4:]  # 取 "raw " 之后的内容
            if json_str:
                send_tcp(current_host, current_port, json_str)
            else:
                print("  用法: raw <json字符串>")
            continue

        # ---- 原始协议帧 ----
        if cmd == "frame":
            if len(parts) < 3:
                print("  用法: frame <type_hex> <json>")
                print("  例如: frame 20 {\"t\":\"set_panel\",\"id\":1,\"mode\":1}")
                continue
            try:
                frame_type = int(parts[1], 16)
            except ValueError:
                print(f"  type 必须是十六进制数，如 20")
                continue
            json_str = " ".join(parts[2:])
            if not json_str:
                print("  payload 不能为空")
                continue
            frame = pack_frame(frame_type, json_str)
            print(f"\n  协议帧 ({len(frame)} 字节): {format_frame_hex(frame)}")
            print(f"  SYNC=AA TYPE={frame_type:02X}({TYPE_NAMES.get(frame_type, 'UNKNOWN')}) "
                  f"LEN={len(json_str):04X} CHECKSUM={frame[-1]:02X}")
            # 也可以通过 TCP 发送原始帧
            print(f"  注意: 在 TCP 模式下通常发送 JSON 而非协议帧，协议帧用于 UART。")
            print(f"  如需发送原始 TCP 数据，请用 frameraw 命令。")
            continue

        # ---- 发送原始十六进制数据 ----
        if cmd == "frameraw":
            hex_str = line[8:].replace(" ", "")
            if not hex_str:
                print("  用法: frameraw <hex字符串>")
                print("  例如: frameraw AA20001D7B22743A227365745..."
                "F70616E656C222C226964223A312C226D6F6465223A317DXX")
                continue
            try:
                raw = bytes.fromhex(hex_str)
            except ValueError as e:
                print(f"  十六进制解析错误: {e}")
                continue
            print(f"\n  连接 {current_host}:{current_port} ...")
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            try:
                sock.connect((current_host, current_port))
                sock.sendall(raw)
                print(f"  -> 已发送 {len(raw)} 字节: {format_frame_hex(raw)}")
                # 解析帧
                result = unpack_frame(raw)
                if result:
                    if result["valid"]:
                        print(f"     帧解析: type=0x{result['type']:02X}({result['type_name']}) "
                              f"len={result['len']} json={result['json']}")
                    else:
                        print(f"     帧校验失败: calc=0x{result['calc']:02X} recv=0x{result['recv']:02X}")
                sock.close()
            except Exception as e:
                print(f"  ✗ 错误: {e}")
            continue

        # ---- 未知命令 ----
        print(f"  未知命令: {cmd} (输入 help 查看帮助)")
        # 尝试模糊匹配快捷命令
        suggestions = [k for k in PRESETS if k.startswith(cmd)]
        if suggestions:
            print(f"  你是不是想输入: {', '.join(suggestions)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ESP-12F WiFi 桥接测试客户端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  py tools/test_client.py                        交互模式
  py tools/test_client.py --panel 1 1            面板1除尘
  py tools/test_client.py --panel 2 2            面板2复位
  py tools/test_client.py --selftest             系统自检
  py tools/test_client.py --task 3               切换任务3
  py tools/test_client.py --cmd 5                命令索引5
  py tools/test_client.py --shutdown             系统关停
  py tools/test_client.py --raw '{"t":"hb"}'     发送自定义JSON
  py tools/test_client.py --frame 20 '{"t":"set_panel","id":1,"mode":1}'  显示帧格式
        """,
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"ESP-12F IP 地址 (默认: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP 端口 (默认: {DEFAULT_PORT})")
    parser.add_argument("--panel", nargs=2, metavar=("ID", "MODE"), help="面板命令: ID(1~4) MODE(1除尘/2复位)")
    parser.add_argument("--selftest", action="store_true", help="发送自检命令")
    parser.add_argument("--task", type=int, metavar="ID", help="切换任务: ID(1~7)")
    parser.add_argument("--cmd", type=int, metavar="IDX", help="命令索引: IDX(1~8)")
    parser.add_argument("--shutdown", action="store_true", help="发送关停命令")
    parser.add_argument("--raw", type=str, metavar="JSON", help="发送原始 JSON 字符串")
    parser.add_argument("--frame", nargs=2, metavar=("TYPE_HEX", "JSON"), help="仅显示协议帧格式 (不发送)")

    args = parser.parse_args()

    current_host = args.host
    current_port = args.port

    # ---- 非交互模式: 发送单条命令后退出 ----
    if args.panel:
        panel_id, mode = args.panel
        json_str = '{"t":"set_panel","id":' + panel_id + ',"mode":' + mode + '}'
        send_tcp(current_host, current_port, json_str)
    elif args.selftest:
        send_tcp(current_host, current_port, PRESETS["selftest"])
    elif args.task:
        json_str = '{"t":"task","id":' + str(args.task) + '}'
        send_tcp(current_host, current_port, json_str)
    elif args.cmd:
        json_str = '{"t":"cmd","idx":' + str(args.cmd) + '}'
        send_tcp(current_host, current_port, json_str)
    elif args.shutdown:
        send_tcp(current_host, current_port, PRESETS["shutdown"])
    elif args.raw:
        send_tcp(current_host, current_port, args.raw)
    elif args.frame:
        try:
            frame_type = int(args.frame[0], 16)
        except ValueError:
            print(f"错误: type 必须是十六进制数")
            sys.exit(1)
        json_str = args.frame[1]
        frame = pack_frame(frame_type, json_str)
        print(f"协议帧 ({len(frame)} 字节): {format_frame_hex(frame)}")
        print(f"  SYNC=AA TYPE={frame_type:02X}({TYPE_NAMES.get(frame_type, 'UNKNOWN')}) "
              f"LEN={len(json_str):04X} CHECKSUM={frame[-1]:02X}")
    else:
        # 交互模式
        interactive()
