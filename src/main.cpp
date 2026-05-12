/*
 * ESP-12F WiFi 桥接固件
 *
 * 通信架构:
 *   MCU (GD32F407) <--UART(115200)--> ESP-12F <--WiFi/TCP--> 前端服务器
 *
 * 上行 (MCU → 服务器): 接收协议帧 → 提取 JSON → TCP 转发
 * 下行 (服务器 → MCU): 接收 JSON → 封装协议帧 → UART 发给 MCU
 *
 * 协议帧格式: 0xAA | type(1B) | len(2B,BE) | payload(N) | checksum(1B,XOR)
 */
#include <Arduino.h>
#include <ESP8266WiFi.h>
#ifdef COMM_MODE_UDP
#include <WiFiUdp.h>
#endif
#include "config.h"
#include "protocol.h"

// ========== 状态指示 LED (ESP-12F 内置 LED, 低电平点亮) ==========
#define LED_PIN   LED_BUILTIN

// ========== 全局状态 ==========
enum BridgeState {
    STATE_WIFI_DISCONNECTED,
    STATE_WIFI_CONNECTING,
    STATE_WIFI_CONNECTED,
    STATE_TCP_CONNECTED,
};

static BridgeState bridgeState = STATE_WIFI_DISCONNECTED;
static unsigned long lastWiFiRetry   = 0;
static unsigned long lastTcpRetry    = 0;
static unsigned long lastHeartbeat   = 0;
static unsigned long lastLedToggle   = 0;

#ifdef COMM_MODE_UDP
static WiFiUDP udpClient;
#else
static WiFiClient tcpClient;
#endif

// ========== TCP 接收缓冲区 (用于拼合分片 JSON) ==========
static char   tcpRxBuf[FRAME_MAX_PAYLOAD + 1];
static uint16_t tcpRxLen = 0;

// ========== 帧发送缓冲 ==========
static uint8_t  frameBuf[FRAME_BUF_SIZE];

// ========== 前向声明 ==========
static void connectWiFi();
static void connectTcp();
static void handleSerialToTcp();
static void handleTcpToSerial();
static void sendHeartbeat();
static void updateLed();

// ===================================================================
void setup() {
    Serial.begin(MCU_BAUD);
    Serial.setRxBufferSize(512);

#ifdef DEBUG_ENABLE
    Serial.println("\r\n[ESP] ESP-12F WiFi Bridge starting...");
    Serial.printf("[ESP] Mode: %s, Server: %s:%d\r\n",
#ifdef COMM_MODE_UDP
        "UDP_BROADCAST",
#else
        "TCP_CLIENT",
#endif
        SERVER_IP, SERVER_PORT);
#endif

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH); // 灭

    // WiFi 模式设置
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);

    connectWiFi();
}

// ===================================================================
void loop() {
    unsigned long now = millis();

    // ---- WiFi 重连 ----
    if (WiFi.status() != WL_CONNECTED) {
        if (bridgeState != STATE_WIFI_DISCONNECTED) {
            DEBUG("WiFi disconnected, reconnecting...");
            bridgeState = STATE_WIFI_DISCONNECTED;
        }
        if (now - lastWiFiRetry >= WIFI_RETRY_INTERVAL) {
            lastWiFiRetry = now;
            connectWiFi();
        }
    }

    // ---- TCP 连接管理 ----
    if (WiFi.status() == WL_CONNECTED) {
#ifdef COMM_MODE_UDP
        if (bridgeState != STATE_TCP_CONNECTED) {
            udpClient.begin(UDP_BROADCAST_PORT);
            bridgeState = STATE_TCP_CONNECTED;
            DEBUG("UDP broadcast ready on port %d", UDP_BROADCAST_PORT);
        }
#else
        if (!tcpClient.connected()) {
            if (bridgeState == STATE_TCP_CONNECTED) {
                DEBUG("TCP disconnected, reconnecting...");
                bridgeState = STATE_WIFI_CONNECTED;
            }
            if (now - lastTcpRetry >= TCP_RETRY_INTERVAL) {
                lastTcpRetry = now;
                connectTcp();
            }
        }
#endif
    }

    // ---- 数据转发: 串口(MCU) -> TCP(服务器) ----
    handleSerialToTcp();

    // ---- 数据转发: TCP(服务器) -> 串口(MCU) ----
    handleTcpToSerial();

    // ---- 心跳 ----
    if (bridgeState == STATE_TCP_CONNECTED && (now - lastHeartbeat >= HEARTBEAT_INTERVAL)) {
        lastHeartbeat = now;
        sendHeartbeat();
    }

    // ---- LED 状态指示 ----
    updateLed();

    // 让出 CPU
    yield();
}

// ===================================================================
//  WiFi 连接
// ===================================================================
static void connectWiFi() {
    bridgeState = STATE_WIFI_CONNECTING;
    DEBUG("Connecting to WiFi: %s", WIFI_SSID);

    WiFi.begin(WIFI_SSID, WIFI_PASS);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && (millis() - start) < 15000) {
        digitalWrite(LED_PIN, LOW);
        delay(200);
        digitalWrite(LED_PIN, HIGH);
        delay(200);
    }

    if (WiFi.status() == WL_CONNECTED) {
        bridgeState = STATE_WIFI_CONNECTED;
        DEBUG("WiFi connected, IP: %s", WiFi.localIP().toString().c_str());
        digitalWrite(LED_PIN, LOW); // 长亮表示 WiFi 已连接
    } else {
        bridgeState = STATE_WIFI_DISCONNECTED;
        DEBUG("WiFi connect failed");
    }
}

// ===================================================================
//  TCP 连接
// ===================================================================
static void connectTcp() {
    DEBUG("Connecting to %s:%d...", SERVER_IP, SERVER_PORT);

    if (tcpClient.connect(SERVER_IP, SERVER_PORT)) {
        bridgeState = STATE_TCP_CONNECTED;
        tcpClient.setNoDelay(true);
        tcpClient.setTimeout(100);
        DEBUG("TCP connected");
    } else {
        DEBUG("TCP connect failed");
    }
}

// ===================================================================
//  上行: 串口(MCU协议帧) → TCP(JSON)
// ===================================================================
static void handleSerialToTcp() {
    while (Serial.available() > 0) {
        uint8_t byte = Serial.read();
        FrameResult result;
        if (unpackFrame(byte, result)) {
            DEBUG("Uplink type=0x%02X len=%d: %s", result.type, result.len, result.payload);

#ifdef COMM_MODE_UDP
            // UDP 广播到子网
            IPAddress broadcastIP = WiFi.localIP();
            broadcastIP[3] = 255;
            udpClient.beginPacket(broadcastIP, UDP_BROADCAST_PORT);
            udpClient.write(result.payload, result.len);
            udpClient.endPacket();
#else
            if (tcpClient.connected()) {
                // 发送 JSON + 换行分隔符
                tcpClient.write(result.payload, result.len);
                tcpClient.write('\n');
                tcpClient.flush();
            }
#endif
        }
    }
}

// ===================================================================
//  下行: TCP(JSON) → 串口(MCU协议帧)
// ===================================================================
static void handleTcpToSerial() {
#ifdef COMM_MODE_UDP
    int pktSize = udpClient.parsePacket();
    if (pktSize > 0 && pktSize <= FRAME_MAX_PAYLOAD) {
        int len = udpClient.read(tcpRxBuf, FRAME_MAX_PAYLOAD);
        if (len > 0) {
            tcpRxBuf[len] = '\0';
            DEBUG("Downlink UDP len=%d: %s", len, tcpRxBuf);

            uint8_t type = detectTypeFromJson(tcpRxBuf);
            if (type != 0) {
                uint16_t frameLen = packFrame(type, (uint8_t *)tcpRxBuf, len,
                                              frameBuf, FRAME_BUF_SIZE);
                if (frameLen > 0) {
                    Serial.write(frameBuf, frameLen);
                    Serial.flush();
                }
            } else {
                DEBUG("Unknown JSON type, dropped");
            }
        }
    }
#else
    if (!tcpClient.connected()) return;

    // 读取 TCP 数据, 拼合到缓冲区
    while (tcpClient.available() > 0) {
        char c = tcpClient.read();
        if (c == '\n' || c == '\r') {
            // 消息分隔符, 处理完整 JSON
            if (tcpRxLen > 0) {
                tcpRxBuf[tcpRxLen] = '\0';
                DEBUG("Downlink TCP len=%d: %s", tcpRxLen, tcpRxBuf);

                uint8_t type = detectTypeFromJson(tcpRxBuf);
                if (type != 0) {
                    uint16_t frameLen = packFrame(type, (uint8_t *)tcpRxBuf, tcpRxLen,
                                                  frameBuf, FRAME_BUF_SIZE);
                    if (frameLen > 0) {
                        Serial.write(frameBuf, frameLen);
                        Serial.flush();
                    }
                } else {
                    DEBUG("Unknown JSON type, dropped");
                }
                tcpRxLen = 0;
            }
        } else if (tcpRxLen < FRAME_MAX_PAYLOAD) {
            tcpRxBuf[tcpRxLen++] = c;
        } else {
            // 缓冲区溢出, 丢弃
            DEBUG("TCP rx overflow, resetting");
            tcpRxLen = 0;
        }
    }
#endif
}

// ===================================================================
//  心跳: 发送 JSON ping 到服务器 (维持连接 + 在线检测)
// ===================================================================
static void sendHeartbeat() {
    const char *hb = "{\"t\":\"hb\"}";

#ifdef COMM_MODE_UDP
    IPAddress broadcastIP = WiFi.localIP();
    broadcastIP[3] = 255;
    udpClient.beginPacket(broadcastIP, UDP_BROADCAST_PORT);
    udpClient.write((const uint8_t *)hb, strlen(hb));
    udpClient.endPacket();
#else
    if (tcpClient.connected()) {
        tcpClient.write((const uint8_t *)hb, strlen(hb));
        tcpClient.write('\n');
        tcpClient.flush();
    }
#endif
}

// ===================================================================
//  LED 状态指示
//  - 快闪 (200ms): WiFi 未连接
//  - 慢闪 (800ms): WiFi 已连接, TCP 未连接
//  - 常亮:        全链路 OK
// ===================================================================
static void updateLed() {
    unsigned long now = millis();
    unsigned long interval;

    switch (bridgeState) {
    case STATE_TCP_CONNECTED:
        digitalWrite(LED_PIN, LOW);  // 常亮
        return;
    case STATE_WIFI_CONNECTED:
        interval = 800;
        break;
    default:
        interval = 200;
        break;
    }

    if (now - lastLedToggle >= interval) {
        lastLedToggle = now;
        digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    }
}
