# ESP-12F WiFi 桥接固件 — 光伏面板除尘控制系统

## 1. 系统概述

本系统通过 ESP-12F WiFi 模块实现 GD32F407 MCU 与前端服务器之间的无线通信，用于远程控制光伏面板执行除尘/复位操作。

### 通信架构

```
前端服务器 <--WiFi/TCP--> ESP-12F <--UART 115200--> GD32F407 MCU
     |                        |                          |
  JSON 命令               桥接透传              解析命令 → 驱动步进电机
  状态展示                                    上传面板状态 / 故障告警
```

| 组件 | 职责 |
|------|------|
| **前端服务器** | 发送面板控制命令，接收面板状态、故障告警、心跳 |
| **ESP-12F** | WiFi 连接 + TCP 透传 + 协议帧封装/解封装 |
| **GD32F407 MCU** | 协议帧编解码、JSON 解析、步进电机驱动、语音播报 |

---

## 2. 硬件连接

| MCU 引脚 | ESP-12F 引脚 | 功能 |
|----------|-------------|------|
| PA11 (UART6 TX) | GPIO3 (RXD) | MCU → ESP-12F 数据发送 |
| PA12 (UART6 RX) | GPIO1 (TXD) | ESP-12F → MCU 数据接收 |
| GND | GND | 共地 |
| 3.3V | VCC / EN | 供电 (3.3V, ≥500mA) |

> ESP-12F 其他引脚：GPIO0 和 GPIO2 上拉 (正常运行模式)，RST 上拉。

---

## 3. 帧协议

### 3.1 帧格式

MCU 与 ESP-12F 之间的串口通信使用二进制帧协议（UART 115200 8N1）。

```
+------+------+-------------+----------+----------+
| 0xAA | type | len (2B,BE) | payload  | checksum |
+------+------+-------------+----------+----------+
  1B     1B        2B          N bytes     1B
```

| 字段 | 长度 | 说明 |
|------|------|------|
| SYNC | 1 字节 | 帧同步头，固定 `0xAA` |
| type | 1 字节 | 消息类型（见下表） |
| len | 2 字节 | payload 长度，Big-Endian，范围 0~512 |
| payload | N 字节 | JSON 字符串（UTF-8） |
| checksum | 1 字节 | 异或校验值 |

### 3.2 校验算法

```
checksum = type ^ len_hi ^ len_lo ^ payload[0] ^ payload[1] ^ ... ^ payload[N-1]
```

接收端逐字节计算 XOR，与收到的 checksum 比对，不匹配则丢弃该帧。

### 3.3 帧解析状态机

```
WAIT_SYNC   → 等待 0xAA
WAIT_TYPE   → 读取 type，累计 XOR
WAIT_LEN_HI → 读取 len 高字节，累计 XOR
WAIT_LEN_LO → 读取 len 低字节，累计 XOR；len>512 则丢弃
WAIT_PAYLOAD→ 逐字节读取 payload，累计 XOR
WAIT_CHECKSUM → 校验 XOR，通过则提取 JSON，回到 WAIT_SYNC
```

---

## 4. 消息类型定义

### 4.1 上行消息 (MCU → 服务器)

MCU 封装协议帧 → ESP-12F 解帧提取 JSON → TCP/UDP 发往服务器。

| type | 名称 | JSON 格式 | 触发时机 |
|------|------|----------|----------|
| `0x11` | 面板状态 | `{"t":"panels","p":[{"id":1,"st":0,"ts":12345},...]}` | 定时 1s 上报 |
| `0x12` | 故障告警 | `{"t":"fault","code":1}` | 面板超时 30s 离线 |
| `0x13` | 自检结果 | `{"t":"selftest_r","ok":4,"ttl":4}` | 自检完成后 |
| `0x14` | 命令确认 | `{"t":"ack","cmd":"set_panel","id":1,"mode":1,"ok":1}` | 电机动作完成后 |

**面板状态 JSON 字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `t` | string | 固定 `"panels"` |
| `p` | array | 面板数组 (4 个) |
| `p[].id` | int | 面板编号 1~4 |
| `p[].st` | int | 当前状态: 0=空闲, 1=除尘中, 2=复位中 |
| `p[].ts` | int | 最后一次状态更新时间戳 (RT-Thread tick) |

### 4.2 下行消息 (服务器 → MCU)

服务器发送 JSON → ESP-12F 封装协议帧 → MCU 解帧解析 JSON。

| type | 名称 | JSON 格式 | 说明 |
|------|------|----------|------|
| `0x20` | 设置面板模式 | `{"t":"set_panel","id":1,"mode":1}` | `id`: 1~4, `mode`: 1=除尘 2=复位 |
| `0x21` | 系统自检 | `{"t":"selftest"}` | 触发全部面板自检 |
| `0x22` | 切换任务 | `{"t":"task","id":2}` | `id`: 1~7, 切换工作模式 |
| `0x23` | 命令索引 | `{"t":"cmd","idx":3}` | 兼容旧 LoRa 协议, `idx`: 1~8 |
| `0x24` | 系统关停 | `{"t":"shutdown"}` | 紧急停止所有面板电机 |

### 4.3 心跳消息

ESP-12F 定时发送心跳维持连接（服务器侧无需回复）。

| type | JSON 格式 | 间隔 |
|------|----------|------|
| — | `{"t":"hb"}` | 30s |

---

## 5. ESP-12F 固件使用

### 5.1 项目结构

```
wifi-gf/
├── platformio.ini          # PlatformIO 工程配置
├── src/
│   ├── config.h            # 用户配置 (WiFi/服务器/串口)
│   ├── protocol.h          # 协议常量 + API 声明
│   ├── protocol.cpp        # 帧打包/解包 + JSON 类型识别
│   └── main.cpp            # 主桥接逻辑 (WiFi + TCP + UART)
├── include/
└── lib/
```

### 5.2 配置修改 (config.h)

烧录前必须修改 `src/config.h` 中的配置：

```cpp
// ------ 必填 ------
#define WIFI_SSID   "你的WiFi名称"
#define WIFI_PASS   "你的WiFi密码"
#define SERVER_IP   "192.168.1.100"   // 前端服务器 IP
#define SERVER_PORT 8888              // 前端服务器端口

// ------ 可选 ------
// 通信模式: 默认 TCP 客户端; 取消下面注释切换为 UDP 广播
// #define COMM_MODE_UDP

// 调试日志: 取消注释开启串口调试输出 (波特率 115200)
// #define DEBUG_ENABLE

// 重连 / 心跳间隔
#define WIFI_RETRY_INTERVAL  5000    // WiFi 重连间隔 ms
#define TCP_RETRY_INTERVAL   3000    // TCP 重连间隔 ms
#define HEARTBEAT_INTERVAL   30000   // 心跳间隔 ms
```

### 5.3 编译与烧录

```bash
# 编译
pio run

# 烧录 (通过 USB-TTL 连接 ESP-12F)
pio run --target upload

# 查看串口输出 (DEBUG_ENABLE 开启后可见日志)
pio device monitor
```

### 5.4 LED 状态指示

ESP-12F 内置 LED (GPIO2) 指示当前工作状态：

| LED 状态 | 含义 |
|----------|------|
| 快闪 (~200ms) | WiFi 未连接 |
| 慢闪 (~800ms) | WiFi OK，TCP 服务器未连接 |
| 常亮 | 全链路正常 (WiFi + TCP 均已连接) |

### 5.5 数据流说明

**上行 (MCU → 服务器)：**

```
Serial.available()
  → Serial.read() 逐字节读取
    → unpackFrame() 状态机解析帧 (校验 XOR)
      → 提取 JSON payload
        → tcpClient.write(payload + '\n')  // TCP 模式
        → udpClient.write(payload)         // UDP 广播模式
```

**下行 (服务器 → MCU)：**

```
tcpClient.available() / udpClient.parsePacket()
  → 读取 JSON 字符串 (以 '\n' 分隔)
    → detectTypeFromJson() 匹配 "t" 字段确定 type
      → packFrame(type, json) 封装协议帧
        → Serial.write(frame) 发给 MCU
```

---

## 6. MCU 侧使用

### 6.1 API 函数

MCU 侧文件路径：`applications/actuate/wifi/wifi_driver.h` / `wifi_driver.c`

| 函数 | 说明 |
|------|------|
| `wifi_init()` | 初始化 UART6 (115200 8N1)，注册接收中断回调 |
| `wifi_thread_entry(void *param)` | WiFi 通信主线程 —— 接收帧 + 定时上报 + 故障检测 |
| `wifi_set_panel_mode(id, mode)` | 控制面板: `id`=1~4, `mode`=1除尘/2复位 |
| `wifi_send_selftest()` | 触发系统自检 |
| `wifi_send_fault_alert()` | 发送故障告警 (面板超时 30s 离线) |

### 6.2 全局变量

| 变量 | 类型 | 说明 |
|------|------|------|
| `wifiPanelData` | `struct wifi_panel` | 4 个面板的状态/目标/时间戳 |
| `wifi_out` | `int` | 故障标志: 0=正常, 非0=故障码 |

### 6.3 兼容宏

为兼容旧代码（`ATK-MV1268D` LoRa 模块），`wifi_driver.h` 定义了以下宏，`task_1.c` 无需修改即可使用：

```c
#define mv1268Data                       wifiPanelData
#define set_Photovoltaic_panel_mode(a,f) wifi_set_panel_mode(a, f)
#define ATK_MV1268D_TxT_Check(a,f)      wifi_send_selftest()
```

### 6.4 线程调用示例

```c
// main.c 或 task 初始化中启动 WiFi 线程
rt_thread_t tid = rt_thread_create(
    "wifi",
    wifi_thread_entry,
    RT_NULL,
    2048,    // 栈大小
    10,      // 优先级
    5);      // 时间片
rt_thread_startup(tid);
```

---

## 7. 服务器对接指南

### 7.1 TCP 模式

服务器监听指定端口（默认 8888），与 ESP-12F 建立 TCP 长连接。

**接收数据格式：** 每行一个 JSON 字符串（以 `\n` 结尾）。

**发送数据格式：** 一行一个 JSON 字符串（以 `\n` 结尾），ESP-12F 根据 `"t"` 字段自动识别消息类型并封装协议帧。

| 您发送的 JSON | 触发的 MCU 动作 |
|---------------|----------------|
| `{"t":"set_panel","id":1,"mode":1}` | 面板 1 执行除尘 |
| `{"t":"set_panel","id":2,"mode":2}` | 面板 2 执行复位 |
| `{"t":"selftest"}` | 触发系统自检 |
| `{"t":"task","id":3}` | 切换到任务 3 |
| `{"t":"cmd","idx":5}` | 设置命令索引 (兼容旧协议) |
| `{"t":"shutdown"}` | 紧急关停全部电机 |

**服务器会收到的 JSON：**

| JSON 示例 | 含义 |
|----------|------|
| `{"t":"panels","p":[{"id":1,"st":0,"ts":4294967},...]}` | 面板实时状态 (每秒) |
| `{"t":"fault","code":1}` | 故障告警 |
| `{"t":"selftest_r","ok":4,"ttl":4}` | 自检结果 |
| `{"t":"ack","cmd":"set_panel","id":1,"mode":1,"ok":1}` | 命令执行确认 |
| `{"t":"hb"}` | 心跳 (忽略即可) |

### 7.2 UDP 广播模式

取消 `config.h` 中 `#define COMM_MODE_UDP` 的注释。

- ESP-12F 发送数据到子网广播地址（如 `192.168.1.255`），端口 `UDP_BROADCAST_PORT` (9999)
- 服务器监听 UDP 端口 9999，发送命令时直接发给 ESP-12F 的 IP（ESP-12F 会从 UDP 包中接收）

---

## 8. 典型通信时序

### 8.1 面板除尘控制

```
服务器                    ESP-12F                    MCU
  |                          |                         |
  |--{"t":"set_panel",      |                         |
  |   "id":1,"mode":1}----->|                         |
  |                          |--[0xAA 0x20 0x00 0x1C  |
  |                          |   JSON... XOR]-------->|
  |                          |                         |-- 解析 JSON
  |                          |                         |-- voice_broadcast(3)
  |                          |                         |-- Stepper_motor_open()
  |                          |<--[0xAA 0x14 0x00 0x35  |
  |                          |   ACK JSON... XOR]------|
  |<--{"t":"ack",           |                         |
  |    "cmd":"set_panel",   |                         |
  |    "id":1,"mode":1,     |                         |
  |    "ok":1}--------------|                         |
  |                          |                         |
  |<--{"t":"panels","p":    |                         |
  |    [...]}---------------| (每秒定时上报)           |
```

### 8.2 系统自检

```
服务器                    ESP-12F                    MCU
  |                          |                         |
  |--{"t":"selftest"}------->|                         |
  |                          |--[0xAA 0x21 ...]------>|
  |                          |                         |-- 执行自检
  |                          |<--[0xAA 0x13 ...]-------|
  |<--{"t":"selftest_r",    |                         |
  |    "ok":4,"ttl":4}------|                         |
```

---

## 9. 故障排查

| 现象 | 可能原因 | 解决方法 |
|------|---------|---------|
| LED 快闪不停 | WiFi 连接失败 | 检查 SSID/密码，确认 AP 信号覆盖 |
| LED 慢闪不停 | TCP 服务器连接失败 | 检查服务器 IP/端口，确认防火墙放行 |
| MCU 收不到命令 | 串口接线错误或波特率不匹配 | 确认 TX↔RX 交叉连接，波特率 115200 |
| 服务器收到乱码 | DEBUG_ENABLE 开启导致额外输出 | 关闭 `DEBUG_ENABLE` 宏 |
| 帧校验频繁失败 | UART 干扰或接线不良 | 缩短杜邦线，加共地，检查接触 |
| TCP 频繁断开 | 服务器端主动断开或网络不稳定 | 检查服务器 TCP keepalive 配置 |

---

## 10. 参数汇总

| 参数 | 默认值 | 说明 |
|------|--------|------|
| UART 波特率 | 115200 | MCU ↔ ESP-12F |
| 帧同步头 | 0xAA | 帧起始标记 |
| 最大 payload | 512 字节 | JSON 最大长度 |
| WiFi 重连间隔 | 5s | WiFi 断开后重试间隔 |
| TCP 重连间隔 | 3s | TCP 断开后重试间隔 |
| 心跳间隔 | 30s | 维持连接 + 在线检测 |
| 面板状态上报间隔 | 1s | MCU 定时上报 |
| 面板超时阈值 | 30s | 超时未更新触发故障告警 |
| LED 快闪周期 | 200ms | WiFi 未连接 |
| LED 慢闪周期 | 800ms | TCP 未连接 |
