#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <Arduino.h>

// ========== 帧协议常量 (与 MCU wifi_driver.h 一致) ==========
#define FRAME_SYNC          0xAA
#define FRAME_MAX_PAYLOAD   64
#define FRAME_BUF_SIZE      128

// ========== 上行类型: MCU -> 服务器 ==========
#define TYPE_PANEL_STATUS   0x11
#define TYPE_FAULT          0x12
#define TYPE_SELFTEST_R     0x13
#define TYPE_ACK            0x14

// ========== 下行类型: 服务器 -> MCU ==========
#define TYPE_SET_PANEL      0x20
#define TYPE_SELFTEST       0x21
#define TYPE_TASK_SWITCH    0x22
#define TYPE_CMD_INDEX      0x23
#define TYPE_SHUTDOWN       0x24
#define TYPE_ALARM          0x25

// ========== 帧解析结果 ==========
struct FrameResult {
    uint8_t  type;
    uint16_t len;
    uint8_t  payload[FRAME_MAX_PAYLOAD];
    bool     valid;
};

// ========== 帧打包/解析 ==========
uint16_t packFrame(uint8_t type, const uint8_t *payload, uint16_t len,
                   uint8_t *outBuf, uint16_t outBufSize);
bool unpackFrame(uint8_t byte, FrameResult &result);
void resetFrameParser();

// ========== 服务器 JSON → 二进制 payload (下行) ==========
// 根据 JSON "t" 字段推断 type, 并构建二进制 payload
// 返回 type, payload 写入 buf, payloadLen 返回长度
uint8_t jsonToPayload(const char *json, uint8_t *buf, uint8_t *payloadLen);

// ========== 二进制 payload → 服务器 JSON (上行) ==========
// 根据 type 将二进制 payload 转换为 JSON 字符串写入 out
// 返回写入字节数
uint16_t payloadToJson(uint8_t type, const uint8_t *payload, uint16_t len,
                       char *out, uint16_t outSize);

#endif
