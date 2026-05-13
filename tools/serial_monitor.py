#!/usr/bin/env python3
"""
ESP-12F UART 协议帧串口监听器
==============================

用 USB-TTL 模块监听 ESP-12F 与 MCU 之间的 UART 通信，
实时显示协议帧的十六进制和 JSON 内容。

硬件连接:
  USB-TTL RX  → ESP-12F GPIO1 (TXD, 发往 MCU 的数据)
  USB-TTL RX  → ESP-12F GPIO3 (RXD, MCU 发给 ESP-12F 的数据) [另一路]
  USB-TTL GND → ESP-12F GND

用法:
  pip install pyserial
  python tools/serial_monitor.py COM3            # 监听 COM3
  python tools/serial_monitor.py COM3 --baud 115200
  python tools/serial_monitor.py --list          # 列出可用串口
"""

import sys
import argparse
import time

FRAME_SYNC = 0xAA
FRAME_MAX_PAYLOAD = 512

TYPE_NAMES = {
    0x11: "PANEL_STATUS (MCU->SVR)",
    0x12: "FAULT        (MCU->SVR)",
    0x13: "SELFTEST_R   (MCU->SVR)",
    0x14: "ACK          (MCU->SVR)",
    0x20: "SET_PANEL    (SVR->MCU)",
    0x21: "SELFTEST     (SVR->MCU)",
    0x22: "TASK_SWITCH  (SVR->MCU)",
    0x23: "CMD_INDEX    (SVR->MCU)",
    0x24: "SHUTDOWN     (SVR->MCU)",
}

DIR_MCU_TO_SVR = {0x11, 0x12, 0x13, 0x14}
DIR_SVR_TO_MCU = {0x20, 0x21, 0x22, 0x23, 0x24}


def pack_frame(frame_type: int, payload: str) -> bytes:
    data = payload.encode("utf-8")
    length = len(data)
    checksum = frame_type ^ ((length >> 8) & 0xFF) ^ (length & 0xFF)
    for b in data:
        checksum ^= b
    frame = bytearray([FRAME_SYNC, frame_type, (length >> 8) & 0xFF, length & 0xFF])
    frame.extend(data)
    frame.append(checksum & 0xFF)
    return bytes(frame)


def format_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


class FrameParser:
    def __init__(self):
        self.reset()

    def reset(self):
        self.state = 0
        self.rx_type = 0
        self.rx_checksum = 0
        self.rx_len = 0
        self.rx_payload = bytearray()
        self.rx_payload_idx = 0

    def feed(self, byte: int):
        """喂入一个字节，解析出完整帧时返回 dict，否则返回 None"""
        if self.state == 0:  # WAIT_SYNC
            if byte == FRAME_SYNC:
                self.state = 1
                self.rx_checksum = 0
                self.rx_payload_idx = 0
                self.rx_payload = bytearray()
        elif self.state == 1:  # WAIT_TYPE
            self.rx_type = byte
            self.rx_checksum ^= byte
            self.state = 2
        elif self.state == 2:  # WAIT_LEN_HI
            self.rx_len = byte << 8
            self.rx_checksum ^= byte
            self.state = 3
        elif self.state == 3:  # WAIT_LEN_LO
            self.rx_len |= byte
            self.rx_checksum ^= byte
            if self.rx_len > FRAME_MAX_PAYLOAD:
                self.reset()
            elif self.rx_len == 0:
                self.state = 5
            else:
                self.state = 4
        elif self.state == 4:  # WAIT_PAYLOAD
            self.rx_payload.append(byte)
            self.rx_checksum ^= byte
            self.rx_payload_idx += 1
            if self.rx_payload_idx >= self.rx_len:
                self.state = 5
        elif self.state == 5:  # WAIT_CHECKSUM
            valid = (byte == self.rx_checksum)
            result = {
                "valid": valid,
                "type": self.rx_type,
                "len": self.rx_len,
                "payload": bytes(self.rx_payload),
                "json": bytes(self.rx_payload).decode("utf-8", errors="replace"),
                "checksum_calc": self.rx_checksum,
                "checksum_recv": byte,
            }
            self.reset()
            return result
        return None


def run_monitor(port: str, baud: int, send_cmd: str = None):
    try:
        import serial
    except ImportError:
        print("需要 pyserial 库: pip install pyserial")
        sys.exit(1)

    # 打开串口
    try:
        ser = serial.Serial(port, baud, timeout=0.1)
    except Exception as e:
        print(f"无法打开串口 {port}: {e}")
        sys.exit(1)

    print(f"串口 {port} 已打开 ({baud} 8N1)")
    print(f"监听 ESP-12F UART 协议帧 ...")
    print(f"{'='*70}")

    parser = FrameParser()
    frame_count = 0

    # 如果有注入命令，先发
    if send_cmd:
        frame = pack_frame(0x20, send_cmd)
        ser.write(frame)
        print(f">>> 已发送注入帧: {format_hex(frame)}")
        print()

    try:
        while True:
            raw = ser.read(ser.in_waiting or 1)
            if not raw:
                continue

            for b in raw:
                result = parser.feed(b)
                if result:
                    frame_count += 1
                    direction = "MCU→服务器" if result["type"] in DIR_MCU_TO_SVR else "服务器→MCU"
                    type_name = TYPE_NAMES.get(result["type"], f"UNKNOWN(0x{result['type']:02X})")

                    if result["valid"]:
                        # 重建完整帧用于显示
                        frame = pack_frame(result["type"], result["json"])
                        print(f"\n{'─'*70}")
                        print(f"[#{frame_count}] {type_name}  ({direction})")
                        print(f"     HEX: {format_hex(frame)}")
                        print(f"     JSON: {result['json']}")

                        # 面板状态展开
                        if result["type"] == 0x11 and '"panels"' in result["json"]:
                            try:
                                import json
                                obj = json.loads(result["json"])
                                for p in obj.get("p", []):
                                    st = {0: "空闲", 1: "除尘中", 2: "复位中"}.get(p.get("st"), "?")
                                    print(f"           面板{p['id']}: {st}  ts={p.get('ts','?')}")
                            except Exception:
                                pass

                        # 故障告警高亮
                        if result["type"] == 0x12:
                            print(f"     *** 故障告警 ***")
                    else:
                        print(f"\n{'─'*70}")
                        print(f"[#{frame_count}] !! 校验失败 !! type=0x{result['type']:02X}")
                        print(f"     期望 XOR=0x{result['checksum_calc']:02X}, 收到=0x{result['checksum_recv']:02X}")

    except KeyboardInterrupt:
        print(f"\n\n退出。共收到 {frame_count} 帧。")
    finally:
        ser.close()


def send_only(port: str, baud: int, json_str: str):
    """仅发送一帧到 ESP-12F (模拟 MCU 发送)"""
    try:
        import serial
    except ImportError:
        print("需要 pyserial 库: pip install pyserial")
        sys.exit(1)

    from test_client import detect_type

    frame_type = 0x20  # default SET_PANEL
    if '"selftest"' in json_str:
        frame_type = 0x21
    elif '"task"' in json_str:
        frame_type = 0x22
    elif '"cmd"' in json_str:
        frame_type = 0x23
    elif '"shutdown"' in json_str:
        frame_type = 0x24

    frame = pack_frame(frame_type, json_str)
    print(f"发送帧 ({len(frame)}B): {format_hex(frame)}")
    print(f"  type=0x{frame_type:02X}({TYPE_NAMES.get(frame_type, '?')}) JSON={json_str}")

    try:
        ser = serial.Serial(port, baud, timeout=1)
        ser.write(frame)
        ser.close()
        print("  已发送")
    except Exception as e:
        print(f"  发送失败: {e}")


def list_ports():
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        if not ports:
            print("未检测到串口")
            return
        print("可用串口:")
        for p in ports:
            print(f"  {p.device}  -  {p.description}")
    except ImportError:
        print("需要 pyserial 库: pip install pyserial")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ESP-12F UART 协议帧串口监听器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python tools/serial_monitor.py COM3                监听 COM3 上的协议帧
  python tools/serial_monitor.py COM5 --baud 9600    指定波特率
  python tools/serial_monitor.py --list              列出可用串口

硬件连接:
  USB-TTL RX ── ESP-12F GPIO1 (TXD, 发往 MCU 方向)
  USB-TTL RX ── ESP-12F GPIO3 (RXD, MCU 发来方向) [可选, 再开一个窗口监听]
  USB-TTL GND ── ESP-12F GND
        """,
    )
    parser.add_argument("port", nargs="?", help="串口名，如 COM3")
    parser.add_argument("--baud", type=int, default=115200, help="波特率 (默认: 115200)")
    parser.add_argument("--list", action="store_true", help="列出可用串口")
    parser.add_argument("--send", type=str, metavar="JSON", help="发送一帧 JSON 到 ESP-12F 后退出")

    args = parser.parse_args()

    if args.list:
        list_ports()
        sys.exit(0)

    if not args.port:
        parser.print_help()
        print("\n错误: 请指定串口，如 COM3。用 --list 查看可用串口。")
        sys.exit(1)

    if args.send:
        send_only(args.port, args.baud, args.send)
    else:
        run_monitor(args.port, args.baud)
