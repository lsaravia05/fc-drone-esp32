#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "lora_sx1278.h"

#define UART_PORT       UART_NUM_0
#define UART_BUF_SIZE   256
#define ENVIO_PERIODO_MS 200   // frecuencia del enlace continuo (5 Hz)

static const char *TAG = "CONTROL_REMOTO";

// ==== Comando actual, compartido entre la tarea que lee la consola
//      y la tarea que transmite por LoRa cada ENVIO_PERIODO_MS ====
static char comando_actual[32] = "off";
static SemaphoreHandle_t comando_mutex;

// Lee una linea de texto desde la consola serial (bloquea hasta Enter)
static int leer_linea(char *buf, int max_len)
{
    int idx = 0;
    while (1) {
        uint8_t byte;
        int len = uart_read_bytes(UART_PORT, &byte, 1, pdMS_TO_TICKS(100));
        if (len <= 0) continue;
        if (byte == '\r' || byte == '\n') {
            if (idx == 0) continue;
            buf[idx] = '\0';
            return idx;
        }
        if (idx < max_len - 1) buf[idx++] = (char)byte;
    }
}

// ==== Tarea: lee comandos de la consola y actualiza comando_actual ====
static void tarea_consola(void *arg)
{
    char linea[32];
    printf("Control remoto listo. Comandos: on / off / <numero 1000-2000>\n");
    while (1) {
        printf("> ");
        fflush(stdout);
        int len = leer_linea(linea, sizeof(linea));
        if (len <= 0) continue;

        xSemaphoreTake(comando_mutex, portMAX_DELAY);
        strncpy(comando_actual, linea, sizeof(comando_actual) - 1);
        comando_actual[sizeof(comando_actual) - 1] = '\0';
        xSemaphoreGive(comando_mutex);

        ESP_LOGI(TAG, "Comando actualizado: %s", linea);
    }
}

// ==== Tarea: retransmite el comando actual por LoRa cada ENVIO_PERIODO_MS ====
// Esto simula un enlace de control continuo (como un RC real), en vez de
// mandar un solo paquete por Enter. Asi el receptor siempre tiene senal
// fresca y no dispara el failsafe mientras el comando siga siendo valido.
static void tarea_transmision(void *arg)
{
    char paquete[40];
    char copia_comando[32];
    uint32_t contador = 0;

    while (1) {
        xSemaphoreTake(comando_mutex, portMAX_DELAY);
        strncpy(copia_comando, comando_actual, sizeof(copia_comando));
        xSemaphoreGive(comando_mutex);

        int plen = snprintf(paquete, sizeof(paquete), "%lu:%s", (unsigned long)contador, copia_comando);
        lora_send_packet((const uint8_t *)paquete, plen);
        contador++;

        vTaskDelay(pdMS_TO_TICKS(ENVIO_PERIODO_MS));
    }
}

void app_main(void)
{
    uart_driver_install(UART_PORT, UART_BUF_SIZE * 2, 0, 0, NULL, 0);
    comando_mutex = xSemaphoreCreateMutex();

    if (!lora_begin(433000000)) {
        ESP_LOGE(TAG, "Fallo al iniciar LoRa");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }

    xTaskCreate(tarea_transmision, "tx_lora", 4096, NULL, 5, NULL);
    xTaskCreate(tarea_consola, "consola", 4096, NULL, 4, NULL);
}
