# emisor_v1 (placa "emisor" / radiocontrol)

Proyecto ESP-IDF v6.0.1 para el ESP32-S3 del lado del emisor. Lee comandos
de texto por la consola serial (UART0) y los retransmite por LoRa (módulo
Ra-02) hacia `Drone_v0.1`.

Ver la descripción funcional completa (qué hace cada parte, canales,
pines, flujo de datos) en [`docs/estado_proyecto.md`](../../docs/estado_proyecto.md)
y en el PDF para el asesor
[`docs/documentacion_tecnica_funcionamiento_actual.pdf`](../../docs/documentacion_tecnica_funcionamiento_actual.pdf).

## Contenido

- `main/main.c` — dos tareas FreeRTOS: `tarea_consola` (lee UART0 línea a
  línea y guarda el comando en `comando_actual`, con mutex) y
  `tarea_transmision` (cada 200ms arma `"<contador>:<comando>"` y lo
  reenvía por LoRa, retransmitiendo el mismo valor mientras no cambie).
- `main/lora_sx1278.c` / `main/lora_sx1278.h` — mismo driver LoRa propio que
  `Drone_v0.1` (aquí se usa `lora_send_packet`).
- `sdkconfig` — configuración de compilación de este proyecto.

> Nota: no se incluye `build/` (artefactos de compilación, se regeneran con
> `idf.py build`) ni los archivos de proyecto de Espressif-IDE/Eclipse
> (`.project`, `.cproject`, `.settings`), que son específicos de cada máquina.

## Herramientas de PC que se conectan a este firmware

Ver [`tools/joystick_control_gui.py`](../../tools/joystick_control_gui.py) —
envía los comandos por el mismo puerto serial que lee `tarea_consola`.

## Compilar y flashear

Requiere ESP-IDF v6.0.1 instalado (vía Espressif-IDE o `idf.py` en el PATH):

```bash
idf.py set-target esp32s3
idf.py build
idf.py -p <PUERTO_COM> flash monitor
```
