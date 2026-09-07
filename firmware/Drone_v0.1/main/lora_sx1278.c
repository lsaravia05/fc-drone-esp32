#include "lora_sx1278.h"
#include <string.h>
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "LORA";

// ==== Registros SX1278 ====
#define REG_FIFO                 0x00
#define REG_OP_MODE               0x01
#define REG_FRF_MSB               0x06
#define REG_FRF_MID               0x07
#define REG_FRF_LSB               0x08
#define REG_PA_CONFIG              0x09
#define REG_LNA                  0x0c
#define REG_FIFO_ADDR_PTR          0x0d
#define REG_FIFO_TX_BASE_ADDR       0x0e
#define REG_FIFO_RX_BASE_ADDR       0x0f
#define REG_FIFO_RX_CURRENT_ADDR    0x10
#define REG_IRQ_FLAGS              0x12
#define REG_RX_NB_BYTES            0x13
#define REG_PKT_SNR_VALUE          0x19
#define REG_PKT_RSSI_VALUE         0x1a
#define REG_MODEM_CONFIG_1         0x1d
#define REG_PAYLOAD_LENGTH         0x22
#define REG_MODEM_CONFIG_3         0x26
#define REG_VERSION                0x42

#define MODE_LONG_RANGE_MODE       0x80
#define MODE_SLEEP                 0x00
#define MODE_STDBY                 0x01
#define MODE_TX                    0x03
#define MODE_RX_CONTINUOUS         0x05

#define PA_BOOST                   0x80

#define IRQ_TX_DONE_MASK           0x08
#define IRQ_PAYLOAD_CRC_ERROR_MASK 0x20
#define IRQ_RX_DONE_MASK           0x40

#define MAX_PKT_LENGTH             255

static spi_device_handle_t lora_spi;
static int lora_implicit_header = 0;

// ==== SPI de bajo nivel ====

static uint8_t lora_read_reg(uint8_t addr)
{
    uint8_t tx[2] = { (uint8_t)(addr & 0x7f), 0x00 };
    uint8_t rx[2] = { 0, 0 };

    spi_transaction_t t = {
        .length = 16,
        .tx_buffer = tx,
        .rx_buffer = rx,
    };
    spi_device_polling_transmit(lora_spi, &t);
    return rx[1];
}

static void lora_write_reg(uint8_t addr, uint8_t value)
{
    uint8_t tx[2] = { (uint8_t)(addr | 0x80), value };

    spi_transaction_t t = {
        .length = 16,
        .tx_buffer = tx,
    };
    spi_device_polling_transmit(lora_spi, &t);
}

static void lora_reset(void)
{
    gpio_set_level(LORA_PIN_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(LORA_PIN_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(10));
}

static void lora_sleep(void)
{
    lora_write_reg(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_SLEEP);
}

static void lora_idle(void)
{
    lora_write_reg(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_STDBY);
}

static void lora_set_frequency(long frequency)
{
    uint64_t frf = ((uint64_t)frequency << 19) / 32000000;

    lora_write_reg(REG_FRF_MSB, (uint8_t)(frf >> 16));
    lora_write_reg(REG_FRF_MID, (uint8_t)(frf >> 8));
    lora_write_reg(REG_FRF_LSB, (uint8_t)(frf >> 0));
}

static void lora_explicit_header_mode(void)
{
    lora_implicit_header = 0;
    lora_write_reg(REG_MODEM_CONFIG_1, lora_read_reg(REG_MODEM_CONFIG_1) & 0xfe);
}

// ==== Inicializacion ====

bool lora_begin(long frequency_hz)
{
    gpio_reset_pin(LORA_PIN_RST);
    gpio_set_direction(LORA_PIN_RST, GPIO_MODE_OUTPUT);

    spi_bus_config_t buscfg = {
        .miso_io_num = LORA_PIN_MISO,
        .mosi_io_num = LORA_PIN_MOSI,
        .sclk_io_num = LORA_PIN_SCK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = 256,
    };

    esp_err_t ret = spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO);
    if (ret != ESP_OK && ret != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "No se pudo inicializar el bus SPI (%d)", ret);
        return false;
    }

    spi_device_interface_config_t devcfg = {
        .clock_speed_hz = 8 * 1000 * 1000,
        .mode = 0,
        .spics_io_num = LORA_PIN_NSS,
        .queue_size = 1,
    };

    ret = spi_bus_add_device(SPI2_HOST, &devcfg, &lora_spi);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "No se pudo agregar el dispositivo SPI (%d)", ret);
        return false;
    }

    lora_reset();

    uint8_t version = lora_read_reg(REG_VERSION);
    if (version != 0x12) {
        ESP_LOGE(TAG, "Chip SX1278 no detectado (version leida: 0x%02x)", version);
        return false;
    }

    lora_sleep();
    lora_set_frequency(frequency_hz);

    lora_write_reg(REG_FIFO_TX_BASE_ADDR, 0);
    lora_write_reg(REG_FIFO_RX_BASE_ADDR, 0);

    lora_write_reg(REG_LNA, lora_read_reg(REG_LNA) | 0x03);
    lora_write_reg(REG_MODEM_CONFIG_3, 0x04);
    lora_write_reg(REG_PA_CONFIG, PA_BOOST | 0x0f);

    lora_explicit_header_mode();
    lora_idle();

    lora_write_reg(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_RX_CONTINUOUS);

    ESP_LOGI(TAG, "LoRa listo a %ld Hz (chip version 0x%02x)", frequency_hz, version);
    return true;
}

// ==== Recepcion ====

int lora_parse_packet(uint8_t *buf, int max_len)
{
    int packet_length = 0;
    uint8_t irq_flags = lora_read_reg(REG_IRQ_FLAGS);

    lora_write_reg(REG_IRQ_FLAGS, irq_flags);

    if ((irq_flags & IRQ_RX_DONE_MASK) && !(irq_flags & IRQ_PAYLOAD_CRC_ERROR_MASK)) {
        if (lora_implicit_header) {
            packet_length = lora_read_reg(REG_PAYLOAD_LENGTH);
        } else {
            packet_length = lora_read_reg(REG_RX_NB_BYTES);
        }

        if (packet_length > max_len) {
            packet_length = max_len;
        }

        uint8_t current_addr = lora_read_reg(REG_FIFO_RX_CURRENT_ADDR);
        lora_write_reg(REG_FIFO_ADDR_PTR, current_addr);

        for (int i = 0; i < packet_length; i++) {
            buf[i] = lora_read_reg(REG_FIFO);
        }

        lora_write_reg(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_RX_CONTINUOUS);
    }

    return packet_length;
}

int lora_packet_rssi(void)
{
    int rssi = lora_read_reg(REG_PKT_RSSI_VALUE);
    return rssi - 164;
}

float lora_packet_snr(void)
{
    uint8_t value = lora_read_reg(REG_PKT_SNR_VALUE);
    return ((int8_t)value) * 0.25f;
}

// ==== Transmision ====

bool lora_send_packet(const uint8_t *buf, int len)
{
    if (len > MAX_PKT_LENGTH) {
        len = MAX_PKT_LENGTH;
    }

    lora_idle();
    lora_write_reg(REG_FIFO_ADDR_PTR, 0);

    for (int i = 0; i < len; i++) {
        lora_write_reg(REG_FIFO, buf[i]);
    }

    lora_write_reg(REG_PAYLOAD_LENGTH, (uint8_t)len);
    lora_write_reg(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_TX);

    while ((lora_read_reg(REG_IRQ_FLAGS) & IRQ_TX_DONE_MASK) == 0) {
        vTaskDelay(pdMS_TO_TICKS(2));
    }

    lora_write_reg(REG_IRQ_FLAGS, IRQ_TX_DONE_MASK);
    lora_write_reg(REG_OP_MODE, MODE_LONG_RANGE_MODE | MODE_RX_CONTINUOUS);

    return true;
}
