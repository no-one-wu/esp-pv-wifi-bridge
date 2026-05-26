/*
 * ESP-12F WiFi 桥接固件 v2.0 (二进制协议)
 *
 * 通信架构:
 *   MCU (GD32F407) <--UART(115200, 二进制帧)--> ESP-12F <--WiFi/TCP(JSON)--> 前端服务器
 *
 * ESP-12F 负责: WiFi + TCP 连接 + 二进制帧 ↔ JSON 转换
 */
#include <Arduino.h>
#include <ESP8266WiFi.h>
#ifdef COMM_MODE_UDP
#include <WiFiUdp.h>
#endif
#include "config.h"
#include "protocol.h"

#define LED_PIN LED_BUILTIN

enum BridgeState {
    STATE_WIFI_DISCONNECTED, STATE_WIFI_CONNECTING,
    STATE_WIFI_CONNECTED, STATE_TCP_CONNECTED,
};
static BridgeState bridgeState = STATE_WIFI_DISCONNECTED;
static unsigned long lastWiFiRetry=0, lastTcpRetry=0, lastHeartbeat=0, lastLedToggle=0;

#ifdef COMM_MODE_UDP
static WiFiUDP udpClient;
#else
static WiFiClient tcpClient;
#endif

static char    tcpRxBuf[FRAME_MAX_PAYLOAD+1];
static uint16_t tcpRxLen = 0;
static uint8_t  frameBuf[FRAME_BUF_SIZE];
static char     jsonBuf[512];

static void connectWiFi();
static void connectTcp();
static void handleSerialToTcp();
static void handleTcpToSerial();
static void sendHeartbeat();
static void updateLed();

// ===================================================================
void setup() {
    Serial.begin(MCU_BAUD);
    Serial.setRxBufferSize(256);
    Serial.printf("\r\n=== ESP-12F WiFi Bridge v2.0 ===\r\n");
    Serial.printf("WiFi: %s  Server: %s:%d\r\n", WIFI_SSID, SERVER_IP, SERVER_PORT);

    pinMode(LED_PIN, OUTPUT);
    digitalWrite(LED_PIN, HIGH);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    connectWiFi();
}

// ===================================================================
void loop() {
    unsigned long now = millis();

    if (WiFi.status() != WL_CONNECTED) {
        if (bridgeState != STATE_WIFI_DISCONNECTED) {
            Serial.printf("[WiFi] disconnected\r\n");
            bridgeState = STATE_WIFI_DISCONNECTED;
        }
        if (now - lastWiFiRetry >= WIFI_RETRY_INTERVAL) {
            lastWiFiRetry = now; connectWiFi();
        }
    }

    if (WiFi.status() == WL_CONNECTED) {
#ifdef COMM_MODE_UDP
        if (bridgeState != STATE_TCP_CONNECTED) {
            udpClient.begin(UDP_BROADCAST_PORT);
            bridgeState = STATE_TCP_CONNECTED;
        }
#else
        if (!tcpClient.connected()) {
            if (bridgeState == STATE_TCP_CONNECTED) {
                Serial.printf("[TCP] disconnected\r\n");
                bridgeState = STATE_WIFI_CONNECTED;
            }
            if (now - lastTcpRetry >= TCP_RETRY_INTERVAL) {
                lastTcpRetry = now; connectTcp();
            }
        }
#endif
    }

    handleSerialToTcp();
    handleTcpToSerial();

    if (bridgeState == STATE_TCP_CONNECTED && (now - lastHeartbeat >= HEARTBEAT_INTERVAL)) {
        lastHeartbeat = now; sendHeartbeat();
    }
    updateLed();
    yield();
}

// ===================================================================
static void connectWiFi() {
    bridgeState = STATE_WIFI_CONNECTING;
    Serial.printf("[WiFi] connecting to %s...\r\n", WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && (millis()-start) < 15000) {
        digitalWrite(LED_PIN, LOW); delay(200);
        digitalWrite(LED_PIN, HIGH); delay(200);
    }
    if (WiFi.status() == WL_CONNECTED) {
        bridgeState = STATE_WIFI_CONNECTED;
        Serial.printf("[WiFi] OK, IP: %s\r\n", WiFi.localIP().toString().c_str());
        digitalWrite(LED_PIN, LOW);
    } else {
        bridgeState = STATE_WIFI_DISCONNECTED;
        Serial.printf("[WiFi] FAIL\r\n");
    }
}

static void connectTcp() {
    Serial.printf("[TCP] connecting to %s:%d...\r\n", SERVER_IP, SERVER_PORT);
    if (tcpClient.connect(SERVER_IP, SERVER_PORT)) {
        bridgeState = STATE_TCP_CONNECTED;
        tcpClient.setNoDelay(true); tcpClient.setTimeout(100);
        Serial.printf("[TCP] OK\r\n");
    } else {
        Serial.printf("[TCP] FAIL\r\n");
    }
}

// ===================================================================
//  上行: 串口(MCU二进制帧) → 二进制payload转JSON → TCP
// ===================================================================
static void handleSerialToTcp() {
    while (Serial.available() > 0) {
        FrameResult result;
        if (unpackFrame(Serial.read(), result)) {
            // 二进制 → JSON
            uint16_t n = payloadToJson(result.type, result.payload, result.len,
                                       jsonBuf, sizeof(jsonBuf));
            if (n > 0 && n < sizeof(jsonBuf)) {
                Serial.printf("[UART->TCP] type=0x%02X %s\r\n", result.type, jsonBuf);
#ifdef COMM_MODE_UDP
                IPAddress bcast = WiFi.localIP(); bcast[3]=255;
                udpClient.beginPacket(bcast, UDP_BROADCAST_PORT);
                udpClient.write((uint8_t*)jsonBuf, n); udpClient.endPacket();
#else
                if (tcpClient.connected()) {
                    tcpClient.write((uint8_t*)jsonBuf, n);
                    tcpClient.write('\n'); tcpClient.flush();
                }
#endif
            }
        }
    }
}

// ===================================================================
//  下行: TCP(JSON) → JSON转二进制payload → 封装帧 → 串口发MCU
// ===================================================================
static void handleTcpToSerial() {
#ifdef COMM_MODE_UDP
    int pktSize = udpClient.parsePacket();
    if (pktSize > 0 && pktSize <= (int)sizeof(tcpRxBuf)-1) {
        int len = udpClient.read(tcpRxBuf, sizeof(tcpRxBuf)-1);
        if (len > 0) {
            tcpRxBuf[len] = '\0';
            uint8_t binPayload[FRAME_MAX_PAYLOAD];
            uint8_t binLen = 0;
            uint8_t type = jsonToPayload(tcpRxBuf, binPayload, &binLen);
            if (type != 0) {
                uint16_t fl = packFrame(type, binPayload, binLen, frameBuf, FRAME_BUF_SIZE);
                if (fl > 0) { Serial.write(frameBuf, fl); Serial.flush(); }
                Serial.printf("[TCP->UART] type=0x%02X json=%s\r\n", type, tcpRxBuf);
            } else {
                Serial.printf("[TCP->UART] UNKNOWN: %s\r\n", tcpRxBuf);
            }
        }
    }
#else
    if (!tcpClient.connected()) return;
    while (tcpClient.available() > 0) {
        char c = tcpClient.read();
        if (c == '\n' || c == '\r') {
            if (tcpRxLen > 0) {
                tcpRxBuf[tcpRxLen] = '\0';
                uint8_t binPayload[FRAME_MAX_PAYLOAD];
                uint8_t binLen = 0;
                uint8_t type = jsonToPayload(tcpRxBuf, binPayload, &binLen);
                if (type != 0) {
                    uint16_t fl = packFrame(type, binPayload, binLen, frameBuf, FRAME_BUF_SIZE);
                    if (fl > 0) { Serial.write(frameBuf, fl); Serial.flush(); }
                    Serial.printf("[TCP->UART] type=0x%02X json=%s\r\n", type, tcpRxBuf);
                } else {
                    Serial.printf("[TCP->UART] UNKNOWN: %s\r\n", tcpRxBuf);
                }
                tcpRxLen = 0;
            }
        } else if (tcpRxLen < sizeof(tcpRxBuf)-1) {
            tcpRxBuf[tcpRxLen++] = c;
        } else {
            tcpRxLen = 0;
        }
    }
#endif
}

// ===================================================================
static void sendHeartbeat() {
    const char *hb = "{\"t\":\"hb\"}";
#ifdef COMM_MODE_UDP
    IPAddress bcast = WiFi.localIP(); bcast[3]=255;
    udpClient.beginPacket(bcast, UDP_BROADCAST_PORT);
    udpClient.write((const uint8_t*)hb, strlen(hb)); udpClient.endPacket();
#else
    if (tcpClient.connected()) {
        tcpClient.write((const uint8_t*)hb, strlen(hb));
        tcpClient.write('\n'); tcpClient.flush();
    }
#endif
}

// ===================================================================
static void updateLed() {
    unsigned long now = millis(), interval;
    switch (bridgeState) {
    case STATE_TCP_CONNECTED: digitalWrite(LED_PIN, LOW); return;
    case STATE_WIFI_CONNECTED: interval=800; break;
    default: interval=200; break;
    }
    if (now-lastLedToggle >= interval) {
        lastLedToggle=now; digitalWrite(LED_PIN, !digitalRead(LED_PIN));
    }
}
