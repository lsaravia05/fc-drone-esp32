# FC Drone ESP32 — AEESS

Controladora de vuelo (flight controller) basada en ESP32-S3, desarrollada
por AEESS (UNMSM, FIEE). Enlace de control por LoRa 433MHz entre una placa
"emisor" (radiocontrol) y una placa "dron" (receptor + ESC + motores).

## Estado actual (resumen rápido)

- ✅ Enlace LoRa emisor↔dron probado en hardware real (0% de pérdidas, señal BUENA).
- ✅ Control de 1 motor (ESC) por LoRa, con failsafe si se corta el enlace.
- 🟡 En definición: el control se construirá **desde cero con un segundo
  ESP32-S3 dedicado** (se descartó usar la RadioMaster Pocket y el plan
  intermedio de receptor ELRS/CRSF).
- ⬜ Pendiente: mixer de 4 ESC, IMU + PID, interfaz TFT (LVGL), telemetría MAVLink.

El detalle completo, con fechas y causas, está en
[`docs/seguimiento_proyecto.md`](docs/seguimiento_proyecto.md) (avances,
errores, próximos pasos, mejoras posibles, ranking de esfuerzo) y en
[`docs/estado_proyecto.md`](docs/estado_proyecto.md) (estado técnico:
hardware, pines, protocolos, arquitectura).

## Estructura del repositorio

```
firmware/
  Drone_v0.1/                 Proyecto ESP-IDF de la placa "dron" (receptor LoRa + ESC)
  emisor_v1/                  Proyecto ESP-IDF de la placa "emisor" (consola + LoRa)

docs/
  estado_proyecto.md          Estado técnico detallado (hardware, pines, protocolos, arquitectura)
  seguimiento_proyecto.md     Bitácora: avances, errores, próximos pasos, mejoras, ranking de esfuerzo
  documentacion_tecnica_funcionamiento_actual.pdf
                               Documento para el ingeniero asesor: diagrama de bloques +
                               descripción del firmware actual (emisor_v1 / Drone_v0.1),
                               canales usados y flujo de datos
  img/
    diagrama_bloques.png       Diagrama de bloques (render)
    diagrama_bloques.dot       Fuente Graphviz del diagrama (editable)

tools/
  joystick_control_gui.py      Interfaz de banco (lado emisor): lee la RadioMaster Pocket
                                por USB (pygame) y controla el ESC vía serial. Sticks
                                visuales, osciloscopio, armado, calidad de señal, export de log.
  receptor_monitor_gui.py      Interfaz de solo monitoreo (lado receptor/dron): muestra
                                armado/desarmado, velocidad del motor y calidad real del
                                enlace LoRa (RSSI/SNR/rating/pérdidas) parseando la consola
                                serial del receptor. No controla nada.
```

`firmware/Drone_v0.1` y `firmware/emisor_v1` son los dos proyectos ESP-IDF
activos (con el driver LoRa propio `lora_sx1278.h/.c`, compartido entre
ambos). No se incluyen sus carpetas `build/` (se regeneran con `idf.py
build`) ni los archivos de proyecto de Espressif-IDE/Eclipse
(`.project`/`.cproject`/`.settings`), específicos de cada máquina.

## Requisitos para las interfaces de PC (`tools/`)

```bash
pip install pyserial pygame-ce
```

- `joystick_control_gui.py` necesita un joystick USB (RadioMaster Pocket en
  modo USB Joystick) conectado a la PC.
- `receptor_monitor_gui.py` solo necesita el puerto serial del ESP32 receptor.
- Ambas cierran/abren el puerto COM exclusivamente — no pueden compartirlo
  con el monitor serial del IDE al mismo tiempo.

## Próximos pasos

Ver el detalle ordenado y priorizado en
[`docs/seguimiento_proyecto.md`](docs/seguimiento_proyecto.md#3-próximos-pasos-en-orden).
En resumen: construir el controlador dedicado (2º ESP32-S3), probar el
control de motor real por LoRa, extender de 1 a 4 ESC (mixer), y más
adelante LVGL + MAVLink.

## Documentación para el asesor del proyecto

[`docs/documentacion_tecnica_funcionamiento_actual.pdf`](docs/documentacion_tecnica_funcionamiento_actual.pdf)
describe **solo** el sistema tal como funciona hoy (sin las interfaces de
PC ni la arquitectura de control futura): diagrama de bloques, qué hace
cada bloque del firmware `emisor_v1` y `Drone_v0.1`, qué canales se usan
(UART0, SPI, enlace RF LoRa 433MHz, PWM a los ESC) y cómo se transmiten
los datos paso a paso.
