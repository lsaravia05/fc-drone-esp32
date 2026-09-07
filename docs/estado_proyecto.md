# Estado del proyecto — FC Drone ESP32 (AEESS)

## Hardware
- MCU: ESP32-S3-N16R8 (placa dual USB-C: puerto "COM"/UART vía CH343 en COM15,
  puerto "USB" nativo OTG sin usar por ahora)
- Motores/ESC: 4x brushless A2208/8T 2600KV, eje de ~3mm (no confirmado con
  calibrador aún). Hélices recomendadas: 6x4.5 (6045) en 3S como opción
  principal, o 5x3 (5030) para más velocidad/menos empuje. Evitar 3-4 palas
  por límite de corriente del motor (18A máx). PWM tipo servo 50 Hz
  (1000/1500/2000 µs)
  - Pines de prueba: M1=GPIO4, M2=GPIO5, M3=GPIO6, M4=GPIO7
  - Diagnóstico en curso: ESC #2 sospechoso de estar defectuoso/mal configurado
    (motor de ESC#2 funcionó bien en otro ESC). Prueba pendiente: intercambiar
    Motor1+ESC2 para confirmar. No mantener ESC#2 en 2000 µs repetidamente.
- LoRa: 1x Ai-Thinker **Ra-02** (emisor, antena IPEX/U.FL) + 1x **Ra-01**
  (receptor/dron, antena espiral soldada). Mismo chip SX1278, banda
  410-525 MHz, 100% compatibles entre sí. Pines SPI: SCK=12, MISO=13,
  MOSI=11, NSS=10, RST=9, DIO0=8. **Enlace probado en hardware real y
  funcionando** (ver Progreso).
- Display: TFT 2.4" SPI táctil (pendiente confirmar controlador exacto,
  probablemente ILI9341 + táctil XPT2046) para el radiocontrol, mostrando
  telemetría (batería, RSSI/SNR, altitud, velocidad, GPS, estado ARM)
  - SPI compartido entre LoRa, TFT y touch, cada uno con su propio CS
- Control: ⚠️ **actualizado 2026-09-07 — decisión de arquitectura vigente:**
  se descarta usar la RadioMaster Pocket. El control se construirá **desde
  cero con un segundo ESP32-S3 dedicado** (lee sticks/botones directamente,
  sin depender de un radio-transmisor comercial ni de un receptor ELRS). El
  plan de "receptor ELRS + CRSF" descrito más abajo queda **descartado**,
  se conserva el texto solo como registro histórico de por qué se llegó a
  la decisión actual.
  - ~~RadioMaster Pocket — confirmado que trae módulo interno ELRS.~~
  - Histórico (ya superado): uso dual que se había planteado —
    - Corto plazo (ya hecho): modo USB Joystick + PC (pygame), para pruebas
      de banco de motores/ESC vía `joystick_control_gui.py`.
    - Plan intermedio (decidido 2026-09-07, **luego descartado el mismo
      día** a favor del ESP32-S3 dedicado): conectar la Pocket al ESP32-S3
      sin pasar por la PC como lector del joystick. USB Host HID no sirve
      (ESP-IDF no soporta gamepads/joysticks por USB Host, solo protocolo
      boot de teclado/mouse — issue espressif/esp-idf #10966, abierto desde
      2023, sin ETA). Se había propuesto un **receptor ELRS** (ej.
      Happymodel EP1/EP2, ~$8-15) emparejado ("bind") con la Pocket, con
      salida UART CRSF (420000 baud) hacia un UART libre del ESP32, que
      parsearía CRSF y reenviaría los canales a la PC por USB serial. Este
      plan ya no se va a implementar.
  - **Plan vigente**: el control se construye desde cero. Ver sección
    "Próximo paso" para los pasos concretos.

## Arquitectura objetivo
Radiocontrol (ESP32-S3 dedicado, construido desde cero + TFT táctil + LoRa)
<—LoRa 433MHz—> Dron (ESP32-S3 + LoRa + mixer + 4 ESC)

Fases: A) enlace LoRa básico → B) paquete de control
(roll/pitch/throttle/yaw, leído directamente por el ESP32-S3 del
controlador) → C) mixer + ESC → D) IMU + PID (control de vuelo)

### Interfaz gráfica del radiocontrol (pantalla TFT)
Se hará con **LVGL** sobre ESP-IDF (no se puede "subir" un script de Python/
Tkinter al ESP32, arquitecturas incompatibles). LVGL corre nativo en el
ESP32-S3 y dibuja directo con los valores de los canales leídos por el
propio controlador (no necesita bridge externo). Vigilar uso de RAM/flash:
usar PSRAM (el N16R8 tiene 8MB) y pasar a proyecto ESP-IDF modular en vez
de archivo único.

### Telemetría — decisión de arquitectura (MAVLink)
Se descartó correr ArduPilot completo en el ESP32 (HAL experimental,
apunta a ESP32 clásico no S3, requiere sensores específicos por I2C, muy
inmaduro). En su lugar:
- El **dron** arma mensajes MAVLink v2 (librería `mavlink/c_library_v2`,
  dialecto `common.xml`): HEARTBEAT (obligatorio, ~1Hz, tipo
  MAV_TYPE_QUADROTOR, autopiloto MAV_AUTOPILOT_GENERIC), SYS_STATUS,
  RC_CHANNELS, y más adelante ATTITUDE/GLOBAL_POSITION_INT con IMU/GPS.
  Empaqueta esos bytes en paquetes LoRa (trocear si excede el payload
  del SX1278) y transmite por SPI al módulo LoRa.
- El **radiocontrol** actúa como puente transparente: recibe el payload
  LoRa por SPI y lo reenvía crudo por UART/USB a la PC, sin parsear nada.
- La **PC** abre el puerto COM del radiocontrol directo en Mission Planner
  o QGroundControl (como radio de telemetría comercial). Limitación: al no
  ser ArduPilot/PX4 real, solo telemetría en vivo, no modos/misiones.
- Limitante real: el LoRa 433MHz tiene poco ancho de banda, hay que ser
  conservador con qué mensajes MAVLink mandar y a qué frecuencia.
- Esto es la fase final, viene después de validar el control básico.

## Entorno de desarrollo
- Se usa **ESP-IDF v6.0.1** (no Arduino) vía Espressif-IDE.
- Dos proyectos activos en `~/workspace/`:
  - **Drone_v0.1**: placa "dron" — ESC (LEDC) + LoRa Ra-01 receptor.
  - **emisor_v1**: placa "emisor" — lee UART, arma paquete y transmite por
    LoRa Ra-02.
  - Ambos comparten el mismo driver `lora_sx1278.h/.c` (propio, sin
    dependencias externas, ver abajo). Nota importante de este IDF: el
    componente `driver` ya NO incluye LEDC/SPI/GPIO — están separados en
    `esp_driver_ledc`, `esp_driver_spi`, `esp_driver_gpio`,
    `esp_driver_uart`. Hay que listarlos explícitamente en `REQUIRES` del
    `main/CMakeLists.txt` de cada proyecto.
  - Ojo: NO editar `README.md` pensando que es el código — el archivo que
    compila es `main/main.c`.
- Flasheo/monitor: cada placa tiene su propio COM (ej. COM15, COM17) — solo
  un programa a la vez puede tener el puerto abierto, y en Espressif-IDE
  cada proyecto nuevo necesita que le asignes su puerto (si no, error
  "Serial port not found").
- Wokwi se sigue usando para simular antes de tocar hardware físico.
- **Driver LoRa propio** (`lora_sx1278.h`/`lora_sx1278.c`): SPI puro sobre
  ESP-IDF, sin librerías externas. Expone `lora_begin(freq_hz)`,
  `lora_parse_packet(buf, max_len)`, `lora_packet_rssi()`,
  `lora_packet_snr()`, `lora_send_packet(buf, len)`. Replica el
  comportamiento de la librería Arduino "LoRa" (Sandeep Mistry): registros
  SX1278 por SPI, modo recepción continua, FIFO, IRQ flags, PA_BOOST a
  17dBm.
- **Protocolo de paquete LoRa**: `"<contador>:<comando>"` (ej. `"42:1500"`,
  `"42:off"`). El contador permite calcular paquetes perdidos comparando
  contra el anterior. El dron responde con RSSI/SNR/% de éxito/rating de
  señal (EXCELENTE/BUENA/DÉBIL/MUY DÉBIL) por consola.
- **`emisor_v1/main/main.c` (confirmado, 2026-09-07)**: dos tareas FreeRTOS.
  `tarea_consola` lee líneas por UART0 (bloqueante, byte a byte hasta \r o
  \n) y las guarda tal cual en la variable compartida `comando_actual`
  (protegida por mutex), sin validar contenido. `tarea_transmision` re-envía
  `comando_actual` por LoRa como `"<contador>:<comando>"` **cada 200ms sin
  parar**, mientras esa siga siendo la variable actual (no manda una sola
  vez y listo — retransmite en loop lo último que haya).

## Progreso

### 1. ESC individual (prueba de banco) — funcionando
Firmware con `esc_init`/`us_to_duty`/`esc_write_us` sobre LEDC (GPIO4, 50Hz):
calibración automática y modo interactivo por consola (`on`/`off`/número
1000-2000us). Controlado desde PC con `esc_control.py` / `joystick_control_gui.py`.

### 2. Enlace LoRa (Fase A) — **probado en hardware real, funcionando**
`emisor_v1` (Ra-02) envía `"contador:comando"` cada vez que llega una línea
por UART. `Drone_v0.1` (Ra-01) lo recibe, calcula RSSI/SNR/pérdidas y
aplica failsafe. Prueba real: 0% de pérdidas, RSSI entre -60 y -73 dBm,
SNR ~8.5-9.5 dB (señal BUENA).

### 3. Control del motor por LoRa — integrado en el mismo firmware de arriba
`Drone_v0.1` ya combina LoRa + ESC en un solo `main.c`: recibe el comando
por LoRa y mueve el motor con `esc_write_us`, con failsafe (apaga el motor
si no llega nada en 1000ms). Pendiente: probar con motor real conectado
(sin hélice) recibiendo comandos desde `emisor_v1`.

### 4. Bug resuelto (2026-09-07): armado intermitente desde `joystick_control_gui.py`
Síntoma: al armar desde la interfaz (pantalla o botón físico), a veces el
motor no se movía y el receptor logueaba "Throttle ... recibido pero NO
esta armado, ignorado" — pero tipeando "on" a mano en la terminal del IDE
del emisor, siempre funcionaba bien.

Causa raíz (confirmada leyendo `emisor_v1/main.c`): `tarea_transmision`
retransmite `comando_actual` por LoRa cada 200ms mientras esa siga siendo
la variable compartida. Tipeando a mano, "on" queda ahí varios segundos
(lo que tarda el humano en escribir el siguiente número), así que se
manda por LoRa muchas veces. El script, en cambio, mandaba "on" y menos
de 200ms después ya lo pisaba con el primer número de throttle — a veces
"on" no alcanzaba a transmitirse ni una sola vez antes de cambiar, y si
ese único intento se perdía por LoRa (hay pérdida de paquetes real, no
0%), el receptor nunca se enteraba de que había que armar.

Fix aplicado en `joystick_control_gui.py`: al armar, se manda "on" y se
retiene esa variable sin mandar números de throttle durante `ARM_HOLD_S =
1.0` segundo (5 ciclos de 200ms), dándole a `tarea_transmision` varias
oportunidades de retransmitir "on" pese a pérdida de paquetes. También se
agregó envío en `\r\n` (antes solo `\n`) y flush explícito tras cada
`_send_line`, aunque el cuello de botella real era el timing, no el
formato de línea (el parser de `main.c` acepta \r o \n indistintamente).

## Herramientas de control (PC, para pruebas de banco — temporales)
- **esc_control.py**: app Tkinter+pyserial, botones ON/OFF, slider/numérico,
  consola de log. Probada y funcionando.
- **control_joystick_esc.py**: script de consola, lee la Pocket (pygame-ce,
  modo USB Joystick) y envía throttle por serial. Modo `--calibrar`.
- **joystick_control_gui.py**: interfaz gráfica estilo radiocontrol (lado
  emisor) — dos sticks visuales, barras de canales, osciloscopio, armado
  (pantalla + botón físico configurable con detector visual de botones),
  consola en vivo, watchdog, calidad de señal, export de log. Guardada
  como doc del proyecto (`claude/joystick_control_gui.py`). Hoy lee el
  joystick directo por USB (pygame); pendiente decidir su rol una vez que
  exista el controlador dedicado (ver "Próximo paso").
- **receptor_monitor_gui.py**: interfaz de solo monitoreo (lado receptor,
  Drone_v0.1) — muestra armado/desarmado, velocidad del motor (barra +
  osciloscopio) y calidad real del enlace LoRa (RSSI/SNR/rating/perdidos)
  parseando la consola serial del receptor. No controla nada. Guardada
  como doc del proyecto (`claude/receptor_monitor_gui.py`).
- Todas requieren cerrar el monitor serial del IDE antes de conectarse (un
  solo programa a la vez por puerto COM).

## Documentos entregables generados
- `documentacion_tecnica_funcionamiento_actual.pdf` (2026-09-07): diagrama
  de bloques + descripción línea por línea de `emisor_v1`/`Drone_v0.1`,
  canales usados y flujo de datos. Preparado para el ingeniero asesor del
  proyecto. Describe **solo** el sistema tal como funciona hoy (LoRa + 1
  ESC controlado por consola), sin las interfaces de PC y sin la
  arquitectura de control futura (todavía no implementada).

## Próximo paso
- **Plan vigente (control desde cero, decidido 2026-09-07)**: construir un
  controlador dedicado con un **segundo ESP32-S3**, sin RadioMaster Pocket
  ni receptor ELRS/CRSF. Pasos concretos:
  1. Definir el hardware del controlador: sticks/potenciómetros y botones
     cableados directo al ESP32-S3 (entradas ADC/GPIO).
  2. Escribir firmware del controlador: lectura de sticks/botones + lógica
     de armado, y empaquetado del comando de control.
  3. Fusionar esa lectura con la lógica de transmisión LoRa (extender
     `emisor_v1` o crear un proyecto nuevo) para que el mismo ESP32-S3 lea
     los controles y transmita al dron en un solo firmware.
  4. Definir si `joystick_control_gui.py` se mantiene como herramienta de
     monitoreo en PC o si el controlador dedicado la reemplaza.
- Probar el control del motor por LoRa con motor real conectado (Fase 3),
  ahora que el bug de armado intermitente está resuelto.
- Confirmar diagnóstico del ESC #2 y diámetro real del eje del motor.
- Extender de 1 ESC a los 4 motores (mixer).
- Interfaz LVGL en el TFT y capa MAVLink para telemetría hacia PC (fases
  posteriores).
