#include "protocol.h"
#include "config.h"
#include <stdio.h>

// ========== 帧接收状态机 ==========
enum RxState { WAIT_SYNC=0, WAIT_TYPE, WAIT_LEN_HI, WAIT_LEN_LO, WAIT_PAYLOAD, WAIT_CHECKSUM };
static RxState  rxState      = WAIT_SYNC;
static uint8_t  rxType       = 0;
static uint8_t  rxChecksum   = 0;
static uint16_t rxLen        = 0;
static uint16_t rxPayloadIdx = 0;
static uint8_t  rxPayload[FRAME_MAX_PAYLOAD];

void resetFrameParser() {
    rxState=WAIT_SYNC; rxType=0; rxChecksum=0; rxLen=0; rxPayloadIdx=0;
}

bool unpackFrame(uint8_t byte, FrameResult &result) {
    result.valid = false;
    switch (rxState) {
    case WAIT_SYNC:
        if (byte == FRAME_SYNC) { rxState=WAIT_TYPE; rxChecksum=0; rxPayloadIdx=0; }
        break;
    case WAIT_TYPE:
        rxType=byte; rxChecksum^=byte; rxState=WAIT_LEN_HI; break;
    case WAIT_LEN_HI:
        rxLen=((uint16_t)byte)<<8; rxChecksum^=byte; rxState=WAIT_LEN_LO; break;
    case WAIT_LEN_LO:
        rxLen|=byte; rxChecksum^=byte;
        if (rxLen>FRAME_MAX_PAYLOAD) rxState=WAIT_SYNC;
        else if (rxLen==0) rxState=WAIT_CHECKSUM;
        else rxState=WAIT_PAYLOAD;
        break;
    case WAIT_PAYLOAD:
        rxPayload[rxPayloadIdx++]=byte; rxChecksum^=byte;
        if (rxPayloadIdx>=rxLen) rxState=WAIT_CHECKSUM;
        break;
    case WAIT_CHECKSUM:
        if (byte==rxChecksum) {
            result.type=rxType; result.len=rxLen;
            memcpy(result.payload, rxPayload, rxLen); result.valid=true;
        }
        rxState=WAIT_SYNC; return result.valid;
    }
    return false;
}

uint16_t packFrame(uint8_t type, const uint8_t *payload, uint16_t len,
                   uint8_t *outBuf, uint16_t outBufSize) {
    if (len>FRAME_MAX_PAYLOAD) return 0;
    uint16_t total = 1+1+2+len+1;
    if (total>outBufSize) return 0;
    uint16_t pos=0; uint8_t checksum=0;
    outBuf[pos++]=FRAME_SYNC;
    outBuf[pos]=type; checksum^=type; pos++;
    outBuf[pos]=(uint8_t)(len>>8); checksum^=outBuf[pos]; pos++;
    outBuf[pos]=(uint8_t)(len&0xFF); checksum^=outBuf[pos]; pos++;
    for (uint16_t i=0; i<len; i++) { outBuf[pos]=payload[i]; checksum^=payload[i]; pos++; }
    outBuf[pos++]=checksum;
    return pos;
}

// ===================================================================
//  服务器 JSON → 二进制 payload (下行: TCP → MCU)
// ===================================================================
uint8_t jsonToPayload(const char *json, uint8_t *buf, uint8_t *payloadLen) {
    if (!json) return 0;

    // alarm
    if (strstr(json, "\"alarm\"")) { *payloadLen=0; return TYPE_ALARM; }

    // shutdown
    if (strstr(json, "\"shutdown\"")) { *payloadLen=0; return TYPE_SHUTDOWN; }

    // selftest
    if (strstr(json, "\"selftest\"")) { *payloadLen=0; return TYPE_SELFTEST; }

    // set_panel: {"t":"set_panel","id":X,"mode":Y}
    if (strstr(json, "\"set_panel\"")) {
        const char *p=json;
        uint8_t id=1, mode=1;
        p=strstr(json, "\"id\":"); if (p) id=atoi(p+5);
        p=strstr(json, "\"mode\":"); if (p) mode=atoi(p+7);
        buf[0]=id; buf[1]=mode;
        *payloadLen=2; return TYPE_SET_PANEL;
    }

    // task: {"t":"task","id":X}
    if (strstr(json, "\"task\"")) {
        const char *p=strstr(json, "\"id\":");
        uint8_t id=1; if (p) id=atoi(p+5);
        buf[0]=id; *payloadLen=1; return TYPE_TASK_SWITCH;
    }

    // cmd: {"t":"cmd","idx":X}
    if (strstr(json, "\"cmd\"")) {
        const char *p=strstr(json, "\"idx\":");
        uint8_t idx=1; if (p) idx=atoi(p+6);
        buf[0]=idx; *payloadLen=1; return TYPE_CMD_INDEX;
    }

    return 0;
}

// ===================================================================
//  二进制 payload → 服务器 JSON (上行: MCU → TCP)
// ===================================================================
uint16_t payloadToJson(uint8_t type, const uint8_t *payload, uint16_t len,
                       char *out, uint16_t outSize) {
    switch (type) {
    case TYPE_PANEL_STATUS:  // 4×(state+ts4B_LE) = 20B
        if (len>=20) {
            return snprintf(out, outSize,
                "{\"t\":\"panels\",\"p\":["
                "{\"id\":1,\"st\":%d,\"ts\":%u},"
                "{\"id\":2,\"st\":%d,\"ts\":%u},"
                "{\"id\":3,\"st\":%d,\"ts\":%u},"
                "{\"id\":4,\"st\":%d,\"ts\":%u}]}",
                payload[0],  (uint32_t)payload[1]|((uint32_t)payload[2]<<8)|((uint32_t)payload[3]<<16)|((uint32_t)payload[4]<<24),
                payload[5],  (uint32_t)payload[6]|((uint32_t)payload[7]<<8)|((uint32_t)payload[8]<<16)|((uint32_t)payload[9]<<24),
                payload[10], (uint32_t)payload[11]|((uint32_t)payload[12]<<8)|((uint32_t)payload[13]<<16)|((uint32_t)payload[14]<<24),
                payload[15], (uint32_t)payload[16]|((uint32_t)payload[17]<<8)|((uint32_t)payload[18]<<16)|((uint32_t)payload[19]<<24));
        }
        break;

    case TYPE_FAULT:  // code(1B)
        if (len>=1) return snprintf(out, outSize, "{\"t\":\"fault\",\"code\":%d}", payload[0]);
        break;

    case TYPE_SELFTEST_R:  // ok(1B)+total(1B)
        if (len>=2) return snprintf(out, outSize, "{\"t\":\"selftest_r\",\"ok\":%d,\"ttl\":%d}", payload[0], payload[1]);
        break;

    case TYPE_ACK: {  // cmd(1B)+id(1B)+mode(1B)+ok(1B)
        if (len>=4) {
            const char *cmdName = "?";
            switch (payload[0]) {
                case TYPE_SET_PANEL: cmdName="set_panel"; break;
                case TYPE_SELFTEST:  cmdName="selftest"; break;
                case TYPE_ALARM:     cmdName="alarm"; break;
            }
            return snprintf(out, outSize,
                "{\"t\":\"ack\",\"cmd\":\"%s\",\"id\":%d,\"mode\":%d,\"ok\":%d}",
                cmdName, payload[1], payload[2], payload[3]);
        }
        break;
    }
    }
    return 0;
}
