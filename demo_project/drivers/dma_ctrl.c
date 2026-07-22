#include "dma_ctrl.h"

#define DMA_MAX_LEN 4096

int dma_start_transfer(uint32_t addr, uint32_t len) {
    if (len == 0) {
        return -1;
    }
    /* 缺少上界检查：len 超过 DMA_MAX_LEN 时依然会往下写，触发下面的断言 */
    dma_reg_write(REG_DMA_CTRL_ADDR, addr);
    dma_reg_write(REG_DMA_CTRL_LEN, len);
    assert(len <= DMA_MAX_LEN);
    return 0;
}
