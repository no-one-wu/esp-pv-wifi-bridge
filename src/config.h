#ifndef CONFIG_H
#define CONFIG_H

// ============ WiFi 配置 ============
#define WIFI_SSID   "1"//iQOO Neo9 Pro
#define WIFI_PASS   "12345678"//wucaijin1

// ============ TCP 服务器配置 ============
// 前端服务器地址 (支持 IP 或域名), 设为 255.255.255.255 则使用 UDP 广播
#define SERVER_IP   "192.168.0.200"
#define SERVER_PORT 8888

// 通信模式: 注释掉使用 TCP 客户端, 取消注释使用 UDP 广播
// #define COMM_MODE_UDP

// UDP 广播端口 (COMM_MODE_UDP 时生效)
#define UDP_BROADCAST_PORT 9999

// ============ 串口配置 (连接 MCU UART6) ============
#define MCU_BAUD    115200

// ============ 系统配置 ============
#define WIFI_RETRY_INTERVAL  5000   // WiFi 重连间隔 ms
#define TCP_RETRY_INTERVAL   3000   // TCP 重连间隔 ms
#define HEARTBEAT_INTERVAL   30000  // 心跳间隔 ms

// ============ 调试开关 ============
#define DEBUG_ENABLE

#ifdef DEBUG_ENABLE
  #define DEBUG(fmt, ...) Serial.printf("[DBG] " fmt "\r\n", ##__VA_ARGS__)
#else
  #define DEBUG(fmt, ...)
#endif

#endif
