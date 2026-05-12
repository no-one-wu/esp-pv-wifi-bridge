#ifndef PROTOCOL_H
#define PROTOCOL_H

#include <Arduino.h>

// ========== 帧协议常量 (与 MCU wifi_driver.h 一致) ==========
#define FRAME_SYNC          0xAA
#define FRAME_MAX_PAYLOAD   512
#define FRAME_BUF_SIZE      600

// ========== 消息类型: MCU -> 服务器 (上行) ==========
#define TYPE_PANEL_STATUS   0x11
#define TYPE_FAULT          0x12
#define TYPE_SELFTEST_R     0x13
#define TYPE_ACK            0x14

// ========== 消息类型: 服务器 -> MCU (下行) ==========
#define TYPE_SET_PANEL      0x20
#define TYPE_SELFTEST       0x21
#define TYPE_TASK_SWITCH    0x22
#define TYPE_CMD_INDEX      0x23
#define TYPE_SHUTDOWN       0x24

// ========== 帧解析结果 ==========
struct FrameResult {
    uint8_t  type;
    uint16_t len;
    uint8_t  payload[FRAME_MAX_PAYLOAD];
    bool     valid;
};

// ========== API ==========

// 将 JSON 封装为协议帧 (用于下行: TCP -> MCU)
// 返回帧数据长度
uint16_t packFrame(uint8_t type, const uint8_t *payload, uint16_t len,
                   uint8_t *outBuf, uint16_t outBufSize);

// 根据 JSON 中 "t" 字段推断消息类型 (用于下行: 服务器 JSON -> type)
uint8_t detectTypeFromJson(const char *json);

// 状态机 — 喂入单个字节, 返回 true 表示解析出一个完整帧
bool unpackFrame(uint8_t byte, FrameResult &result);

// 重置帧接收状态机
void resetFrameParser();

#endif
