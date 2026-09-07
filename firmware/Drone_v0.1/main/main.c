#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "lora_sx1278.h"

// ==== LEDs de estado ====
#define LED1_GPIO         4   // ARMADO
#define LED2_GPIO         5   // DESARMADO

// ==== Señal PWM hacia el ESC (motor brushless) ====
#define ESC_GPIO          6
#define ESC_LEDC_TIMER    LEDC_TIMER_0
#define ESC_LEDC_MODE     LEDC_LOW_SPEED_MODE
#define ESC_LEDC_CHANNEL  LEDC_CHANNEL_0
#define ESC_LEDC_RES      LEDC_TIMER_14_BIT   // resolucion del duty
#define ESC_FREQ_HZ       50                  // PWM tipo servo, 50Hz

#define ESC_MIN_US        1000   // reposo / desarmado
#define ESC_MAX_US        2000   // maximo

#define FAILSAFE_MS       1000   // sin senal por este tiempo -> desarma todo

static const char *TAG = "DRON";

// ==== Estado de control ====
static bool armado = false;
static uint32_t throttle_us = ESC_MIN_US;

// ==== Estadisticas de calidad de enlace (RSSI/SNR/perdidas) ====
// Protocolo de paquete: "<contador>:<comando>"  ej. "42:on" "42:off" "42:1500"
static int32_t ultimo_contador = -1;
static uint32_t paquetes_recibidos = 0;
static uint32_t paquetes_perdidos = 0;

static void leds_init(void)
{
    gpio_reset_pin(LED1_GPIO);
    gpio_set_direction(LED1_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(LED1_GPIO, 0);

    gpio_reset_pin(LED2_GPIO);
    gpio_set_direction(LED2_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(LED2_GPIO, 0);
}

static void set_leds_armado(bool en_armado)
{
    gpio_set_level(LED1_GPIO, en_armado ? 1 : 0);
    gpio_set_level(LED2_GPIO, en_armado ? 0 : 1);
}

// Failsafe: apaga ambos LEDs (distinto de "desarmado" normal, que deja LED2 prendido)
static void leds_apagar_todo(void)
{
    gpio_set_level(LED1_GPIO, 0);
    gpio_set_level(LED2_GPIO, 0);
}

static uint32_t us_to_duty(uint32_t us)
{
    uint32_t max_duty = (1u << ESC_LEDC_RES) - 1u;
    // periodo a 50Hz = 20000 us
    uint64_t duty = ((uint64_t)us * max_duty) / 20000ull;
    return (uint32_t)duty;
}

static void esc_write_us(uint32_t us)
{
    if (us < ESC_MIN_US) us = ESC_MIN_US;
    if (us > ESC_MAX_US) us = ESC_MAX_US;
    ledc_set_duty(ESC_LEDC_MODE, ESC_LEDC_CHANNEL, us_to_duty(us));
    ledc_update_duty(ESC_LEDC_MODE, ESC_LEDC_CHANNEL);
}

static void esc_init(void)
{
    ledc_timer_config_t timer_conf = {
        .speed_mode = ESC_LEDC_MODE,
        .duty_resolution = ESC_LEDC_RES,
        .timer_num = ESC_LEDC_TIMER,
        .freq_hz = ESC_FREQ_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ledc_timer_config(&timer_conf);

    ledc_channel_config_t ch_conf = {
        .gpio_num = ESC_GPIO,
        .speed_mode = ESC_LEDC_MODE,
        .channel = ESC_LEDC_CHANNEL,
        .timer_sel = ESC_LEDC_TIMER,
        .duty = 0,
        .hpoint = 0,
    };
    ledc_channel_config(&ch_conf);

    // Manda el minimo (1000us) unos segundos para que el ESC arme/reconozca
    // la señal antes de aceptar throttle. NO conectar la helice para esta prueba.
    esc_write_us(ESC_MIN_US);
    ESP_LOGI(TAG, "ESC inicializado en reposo (%d us). Esperando 3s para armado del ESC...", ESC_MIN_US);
    vTaskDelay(pdMS_TO_TICKS(3000));
}

// Registra RSSI/SNR/perdidas del paquete recibido y lo imprime por consola.
static void registrar_calidad(int32_t contador)
{
    int rssi = lora_packet_rssi();
    float snr = lora_packet_snr();

    paquetes_recibidos++;

    if (ultimo_contador != -1) {
        int32_t diferencia = contador - ultimo_contador - 1;
        if (diferencia > 0) {
            paquetes_perdidos += (uint32_t)diferencia;
        }
    }
    ultimo_contador = contador;

    uint32_t total_esperado = paquetes_recibidos + paquetes_perdidos;
    float porcentaje_exito = (total_esperado > 0)
        ? (100.0f * (float)paquetes_recibidos / (float)total_esperado)
        : 100.0f;

    const char *calidad;
    if (rssi > -80) {
        calidad = "EXCELENTE";
    } else if (rssi > -100) {
        calidad = "BUENA";
    } else if (rssi > -115) {
        calidad = "DEBIL";
    } else {
        calidad = "MUY DEBIL / AL LIMITE";
    }

    ESP_LOGI(TAG, "#%ld | RSSI: %d dBm | SNR: %.2f dB | Perdidos: %lu | Total: %lu | Exito: %.1f %% | Senal: %s",
             (long)contador, rssi, snr, (unsigned long)paquetes_perdidos,
             (unsigned long)total_esperado, porcentaje_exito, calidad);
}

// Aplica el comando de texto ("on" / "off" / "<numero 1000-2000>")
static void procesar_comando(char *cmd)
{
    if (strcmp(cmd, "on") == 0) {
        armado = true;
        set_leds_armado(true);
        ESP_LOGI(TAG, "ARMADO");
    } else if (strcmp(cmd, "off") == 0) {
        armado = false;
        throttle_us = ESC_MIN_US;
        esc_write_us(ESC_MIN_US);
        set_leds_armado(false);
        ESP_LOGI(TAG, "DESARMADO (motor forzado a minimo)");
    } else {
        char *endptr;
        long valor = strtol(cmd, &endptr, 10);
        if (endptr != cmd && *endptr == '\0') {
            if (armado) {
                throttle_us = (uint32_t)valor;
                esc_write_us(throttle_us);
                ESP_LOGI(TAG, "Throttle -> %lu us", (unsigned long)throttle_us);
            } else {
                ESP_LOGW(TAG, "Throttle %ld recibido pero NO esta armado, ignorado", valor);
            }
        } else {
            ESP_LOGW(TAG, "Comando invalido: '%s'", cmd);
        }
    }
}

void app_main(void)
{
    leds_init();
    esc_init();

    if (!lora_begin(433000000)) {
        ESP_LOGE(TAG, "Fallo al iniciar LoRa");
        while (1) vTaskDelay(pdMS_TO_TICKS(1000));
    }
    ESP_LOGI(TAG, "Receptor listo, esperando comandos por LoRa...");

    uint8_t buf[64];
    int64_t ultimo_paquete = esp_timer_get_time() / 1000;
    bool en_failsafe = false;

    while (1) {
        int packet_size = lora_parse_packet(buf, sizeof(buf) - 1);
        int64_t ahora = esp_timer_get_time() / 1000;

        if (packet_size > 0) {
            buf[packet_size] = '\0';
            ultimo_paquete = ahora;
            en_failsafe = false;

            // Formato esperado: "<contador>:<comando>"
            char *sep = strchr((char *)buf, ':');
            if (sep != NULL) {
                *sep = '\0';
                int32_t contador = (int32_t)strtol((char *)buf, NULL, 10);
                char *cmd = sep + 1;

                registrar_calidad(contador);
                procesar_comando(cmd);
            } else {
                ESP_LOGW(TAG, "Paquete sin contador (formato viejo): '%s'", (char *)buf);
                procesar_comando((char *)buf);
            }
        }

        if (!en_failsafe && (ahora - ultimo_paquete) > FAILSAFE_MS) {
            ESP_LOGW(TAG, "FAILSAFE: sin senal LoRa, desarmando y apagando motor");
            armado = false;
            throttle_us = ESC_MIN_US;
            esc_write_us(ESC_MIN_US);
            leds_apagar_todo();
            en_failsafe = true;
        }

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
