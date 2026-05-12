#include "protocol.h"
#include "config.h"

// ========== 帧接收状态机 ==========
enum RxState {
    WAIT_SYNC = 0,
    WAIT_TYPE,
    WAIT_LEN_HI,
    WAIT_LEN_LO,
    WAIT_PAYLOAD,
    WAIT_CHECKSUM,
};

static RxState      rxState       = WAIT_SYNC;
static uint8_t      rxType        = 0;
static uint8_t      rxChecksum    = 0;
static uint16_t     rxLen         = 0;
static uint16_t     rxPayloadIdx  = 0;
static uint8_t      rxPayload[FRAME_MAX_PAYLOAD];

// ===================================================================
//  重置帧解析状态机
// ===================================================================
void resetFrameParser() {
    rxState      = WAIT_SYNC;
    rxType       = 0;
    rxChecksum   = 0;
    rxLen        = 0;
    rxPayloadIdx = 0;
}

// ===================================================================
//  逐字节喂入, 返回 true 表示解析出一个完整帧
// ===================================================================
bool unpackFrame(uint8_t byte, FrameResult &result) {
    result.valid = false;

    switch (rxState) {
    case WAIT_SYNC:
        if (byte == FRAME_SYNC) {
            rxState       = WAIT_TYPE;
            rxChecksum    = 0;
            rxPayloadIdx  = 0;
        }
        break;

    case WAIT_TYPE:
        rxType     = byte;
        rxChecksum ^= byte;
        rxState    = WAIT_LEN_HI;
        break;

    case WAIT_LEN_HI:
        rxLen       = ((uint16_t)byte) << 8;
        rxChecksum ^= byte;
        rxState     = WAIT_LEN_LO;
        break;

    case WAIT_LEN_LO:
        rxLen      |= byte;
        rxChecksum ^= byte;
        if (rxLen > FRAME_MAX_PAYLOAD) {
            rxState = WAIT_SYNC;
        } else if (rxLen == 0) {
            rxState = WAIT_CHECKSUM;
        } else {
            rxState = WAIT_PAYLOAD;
        }
        break;

    case WAIT_PAYLOAD:
        rxPayload[rxPayloadIdx++] = byte;
        rxChecksum ^= byte;
        if (rxPayloadIdx >= rxLen)
            rxState = WAIT_CHECKSUM;
        break;

    case WAIT_CHECKSUM:
        if (byte == rxChecksum) {
            result.type = rxType;
            result.len  = rxLen;
            memcpy(result.payload, rxPayload, rxLen);
            result.payload[rxLen] = '\0';
            result.valid = true;
        } else {
            DEBUG("checksum err: calc=0x%02X recv=0x%02X", rxChecksum, byte);
        }
        rxState = WAIT_SYNC;
        return result.valid;
    }
    return false;
}

// ===================================================================
//  封装协议帧: type + payload → 完整帧字节
// ===================================================================
uint16_t packFrame(uint8_t type, const uint8_t *payload, uint16_t len,
                   uint8_t *outBuf, uint16_t outBufSize) {
    if (len > FRAME_MAX_PAYLOAD) return 0;
    uint16_t total = 1 + 1 + 2 + len + 1;
    if (total > outBufSize) return 0;

    uint16_t pos = 0;
    uint8_t  checksum = 0;

    outBuf[pos++] = FRAME_SYNC;

    outBuf[pos] = type;
    checksum   ^= type;
    pos++;

    outBuf[pos] = (uint8_t)(len >> 8);
    checksum   ^= outBuf[pos];
    pos++;
    outBuf[pos] = (uint8_t)(len & 0xFF);
    checksum   ^= outBuf[pos];
    pos++;

    for (uint16_t i = 0; i < len; i++) {
        outBuf[pos] = payload[i];
        checksum   ^= payload[i];
        pos++;
    }

    outBuf[pos++] = checksum;
    return pos;
}

// ===================================================================
//  根据 JSON 中 "t" 字段推断消息类型 (服务器 -> MCU)
// ===================================================================
uint8_t detectTypeFromJson(const char *json) {
    if (!json) return 0;

    // 简单字符串匹配, 避免引入完整 JSON 库
    if (strstr(json, "\"set_panel\""))  return TYPE_SET_PANEL;
    if (strstr(json, "\"selftest\""))   return TYPE_SELFTEST;
    if (strstr(json, "\"task\""))       return TYPE_TASK_SWITCH;
    if (strstr(json, "\"cmd\""))        return TYPE_CMD_INDEX;
    if (strstr(json, "\"shutdown\""))   return TYPE_SHUTDOWN;

    return 0; // 未知类型
}
