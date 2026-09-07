# Drone_v0.1 (placa "dron" / receptor)

Proyecto ESP-IDF v6.0.1 para el ESP32-S3 del lado del dron. Recibe el
comando de control por LoRa (módulo Ra-01) y mueve el ESC del motor 1.

Ver la descripción funcional completa (qué hace cada parte, canales,
pines, flujo de datos) en [`docs/estado_proyecto.md`](../../docs/estado_proyecto.md)
y en el PDF para el asesor
[`docs/documentacion_tecnica_funcionamiento_actual.pdf`](../../docs/documentacion_tecnica_funcionamiento_actual.pdf).

## Contenido

- `main/main.c` — recibe el paquete LoRa (`"<contador>:<comando>"`), calcula
  RSSI/SNR/% de pérdidas/rating, aplica failsafe (apaga el motor si no llega
  nada en 1000ms) y controla el ESC del motor 1 (GPIO4) vía LEDC (50Hz, 1000-2000us).
- `main/lora_sx1278.c` / `main/lora_sx1278.h` — driver SPI propio para el
  chip SX1278 (sin librerías externas): `lora_begin`, `lora_parse_packet`,
  `lora_packet_rssi`, `lora_packet_snr`, `lora_send_packet`.
- `sdkconfig` — configuración de compilación de este proyecto (generada por `idf.py menuconfig`).

> Nota: no se incluye `build/` (artefactos de compilación, se regeneran con
> `idf.py build`) ni los archivos de proyecto de Espressif-IDE/Eclipse
> (`.project`, `.cproject`, `.settings`), que son específicos de cada máquina.

## Compilar y flashear

Requiere ESP-IDF v6.0.1 instalado (vía Espressif-IDE o `idf.py` en el PATH):

```bash
idf.py set-target esp32s3
idf.py build
idf.py -p <PUERTO_COM> flash monitor
```
