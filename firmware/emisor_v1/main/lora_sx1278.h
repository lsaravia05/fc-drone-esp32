#ifndef LORA_SX1278_H
#define LORA_SX1278_H

#include <stdint.h>
#include <stdbool.h>

// ==== Pines SPI hacia el modulo SX1278 (Ra-01 / Ra-02) ====
#define LORA_PIN_SCK   12
#define LORA_PIN_MISO  13
#define LORA_PIN_MOSI  11
#define LORA_PIN_NSS   10
#define LORA_PIN_RST   9
#define LORA_PIN_DIO0  8

// Inicializa SPI + chip LoRa a la frecuencia indicada (Hz). true si detecto el chip.
bool lora_begin(long frequency_hz);

// Revisa si llego un paquete; si hay uno lo copia a buf (sin '\0') y retorna su tamano.
// Retorna 0 si no hay paquete nuevo.
int lora_parse_packet(uint8_t *buf, int max_len);

// RSSI (dBm) y SNR (dB) del ultimo paquete recibido.
int lora_packet_rssi(void);
float lora_packet_snr(void);

// Envia un paquete (bloqueante hasta que termina la transmision).
bool lora_send_packet(const uint8_t *buf, int len);

#endif
