# Seguimiento del Proyecto — FC Drone ESP32 (AEESS)

> Este documento es el registro vivo del proyecto: avances, errores, próximos pasos, mejoras posibles y ranking de tiempo/esfuerzo. Se actualiza cada vez que se reporte algo nuevo en este chat. El detalle técnico completo (pines, protocolos, arquitectura) sigue viviendo en `estado_proyecto.md`; este doc es el tablero de seguimiento.

> ⚠️ **Decisión de arquitectura (2026-09-07):** el control del dron **ya no se hará con la RadioMaster Pocket**. Se descarta también el plan intermedio de receptor ELRS + parser CRSF que se había definido antes. En su lugar, el control se construirá **desde cero con un segundo ESP32-S3 dedicado** (lee sticks/botones directamente, sin depender de un radio-transmisor comercial). Detalle en la sección 3 y en el documento entregado al ingeniero asesor (`documentacion_tecnica_funcionamiento_actual.pdf`, describe solo el sistema tal como funciona hoy — LoRa + 1 ESC — sin esta parte todavía implementada).

## 1. Avance general por fase

| Fase | Descripción | Estado |
|---|---|---|
| A | Enlace LoRa básico (emisor↔dron) | ✅ Completado — probado en hardware real, 0% pérdidas, señal BUENA |
| B | Paquete de control (roll/pitch/throttle/yaw) | 🟡 En curso — control de 1 motor por LoRa funcionando; falta construir el controlador dedicado (2do ESP32-S3, desde cero, sin Pocket) |
| C | Mixer + 4 ESC | ⬜ No iniciada — solo 1 ESC probado en banco |
| D | IMU + PID (control de vuelo real) | ⬜ No iniciada |
| — | TFT + LVGL (interfaz radiocontrol) | ⬜ No iniciada |
| — | Telemetría MAVLink hacia PC | ⬜ No iniciada (decisión de arquitectura ya tomada) |

## 2. Registro de errores / bugs

| Fecha | Bug | Causa raíz | Estado |
|---|---|---|---|
| 2026-09-07 | Armado intermitente desde `joystick_control_gui.py` (a veces el motor no arrancaba) | `tarea_transmision` en `emisor_v1` retransmite el comando actual cada 200ms; el script pisaba "on" con el throttle antes de que se alcanzara a transmitir, y si ese único intento se perdía por LoRa, el dron nunca se enteraba | ✅ Resuelto — fix `ARM_HOLD_S = 1.0s` en el script |
| — | ESC #2 sospechoso de estar defectuoso o mal configurado (motor que en otro ESC sí funcionó bien) | No confirmada aún | 🟡 Diagnóstico en curso — prueba pendiente: intercambiar Motor1+ESC2. Evitar sostener ESC#2 en 2000µs repetidamente |
| — | USB Host HID no soporta joysticks/gamepads en ESP-IDF | Limitación de espressif/esp-idf (issue #10966, sin ETA) | ✅ Resuelto vía cambio de enfoque — descartado además el plan intermedio de receptor ELRS + CRSF (2026-09-07): el control se construye desde cero con un 2do ESP32-S3 dedicado, sin RadioMaster Pocket |

## 3. Próximos pasos (en orden)

> Plan vigente desde el 2026-09-07. Reemplaza por completo el plan anterior de "receptor ELRS + CRSF + RadioMaster Pocket" (ver tachado más abajo, se mantiene solo como registro histórico).

1. Definir el hardware del controlador dedicado: sticks/potenciómetros y botones que leerá el 2do ESP32-S3 (entradas ADC/GPIO directas, sin radio-transmisor comercial de por medio).
2. Escribir firmware del controlador: lectura de sticks/botones + armado, y empaquetado del comando (mismo formato u homólogo a `"<contador>:<comando>"` o extendido a roll/pitch/throttle/yaw).
3. Fusionar ese firmware con la lógica de `emisor_v1` (o crear un proyecto nuevo) para que el mismo ESP32-S3 lea los controles Y transmita por LoRa al dron.
4. Definir si se mantiene compatibilidad con `joystick_control_gui.py` (monitor en PC) o si el controlador dedicado reemplaza también esa herramienta de banco.
5. Probar control de motor real por LoRa (Fase 3, ya con el bug de armado resuelto).
6. Confirmar diagnóstico del ESC #2 y diámetro real del eje del motor.
7. Extender de 1 ESC a los 4 motores (mixer).
8. Interfaz LVGL en el TFT.
9. Capa MAVLink para telemetría hacia PC.

<details>
<summary>Plan anterior descartado (RadioMaster Pocket + ELRS/CRSF) — solo histórico</summary>

1. ~~Comprar receptor ELRS 2.4GHz (ej. Happymodel EP1/EP2, ~$8-15).~~
2. ~~Emparejar ("bind") el receptor con la RadioMaster Pocket.~~
3. ~~Cablear el receptor a un UART libre del ESP32 (no UART0/consola).~~
4. ~~Escribir parser CRSF en el ESP32 (frame 0x16, 16 canales 11-bit, CRC8).~~
5. ~~Adaptar `joystick_control_gui.py` para leer canales desde el ESP32 por serial en vez de pygame.~~
6. ~~Integrar con `emisor_v1` para que el mismo ESP32 lea la Pocket (CRSF) y transmita por LoRa al dron.~~

</details>

## 4. Posibles mejoras / cambios positivos

- **Modularizar los proyectos ESP-IDF**: pasar de `main.c` único a estructura modular (separar lora, esc, crsf, mavlink en sus propios componentes) antes de que crezca más — más fácil de mantener cuando se integren CRSF + LoRa + LVGL en un solo firmware.
- **Watchdog / failsafe general**: ya existe failsafe de motor por timeout LoRa (1000ms); conviene aplicar el mismo criterio a la futura conexión CRSF (qué pasa si se pierde el enlace con la Pocket).
- **Log histórico de pruebas de enlace LoRa**: guardar RSSI/SNR/% pérdidas de cada prueba en un archivo (no solo consola) para comparar degradación con distancia/obstáculos más adelante.
- **Confirmar controlador del TFT antes de comprar/cablear** (ILI9341 + XPT2046 es la suposición actual, no confirmada) — evita retrabajo en el driver SPI.
- **Documentar pinout único**: hay pines de LoRa, ESC y (pronto) UART CRSF + TFT compartiendo SPI — un diagrama/tabla único de pines por placa evita conflictos al integrar todo.
- **Definir ya el formato del "paquete de control" de la Fase B** (roll/pitch/throttle/yaw + armado) para que el nuevo controlador dedicado lo arme una sola vez y no haya que rediseñarlo después.
- **Aprovechar que el controlador se construye desde cero**: definir de una vez cuántos canales/botones necesita el sistema completo (armado, modos de vuelo futuros, etc.) para no tener que rehacer el hardware del controlador cuando se agregue IMU/PID más adelante.

## 5. Ranking de tiempo / esfuerzo

No tengo fechas de inicio registradas por tarea, así que esto es un ranking por **esfuerzo técnico estimado**, no por tiempo calendario real. Si me pasas fechas de cuándo arrancaste cada bloque, lo convierto en un cronograma real con duraciones.

| Tarea | Esfuerzo estimado | Motivo |
|---|---|---|
| Controlador dedicado (2do ESP32-S3) + LoRa + LVGL en un solo firmware modular | 🔴 Alto | Junta 3 subsistemas (lectura de controles, radio, UI) que hoy están separados; además el controlador se construye desde cero (hardware + firmware), requiere reestructurar a proyecto ESP-IDF modular |
| IMU + PID (Fase D) | 🔴 Alto | Es control de vuelo real; requiere tuning iterativo, no es solo firmware |
| Diseño de hardware del controlador (sticks/botones + ESP32-S3) | 🟠 Medio-alto | Al no usarse la Pocket ni un receptor ELRS, hay que definir y cablear los sticks/botones desde cero |
| Interfaz LVGL en TFT | 🟠 Medio-alto | Nueva librería (LVGL) + gestión de RAM/PSRAM, pero acotada a una pantalla |
| Mixer + 4 ESC | 🟠 Medio | Ya hay 1 ESC funcionando; extender a 4 es principalmente repetir + sumar lógica de mezcla |
| Capa MAVLink (telemetría) | 🟠 Medio | Requiere ser selectivo con qué mandar por el ancho de banda limitado de LoRa 433MHz |
| Diagnóstico ESC #2 | 🟢 Bajo | Ya hay hipótesis clara y prueba concreta pendiente (intercambiar motor) |
| Enlace LoRa básico (Fase A) | ✅ Ya hecho | — |
| Control de 1 motor por LoRa + fix de armado | ✅ Ya hecho | — |

## 6. Documentos entregables generados

- `documentacion_tecnica_funcionamiento_actual.pdf` (2026-09-07): diagrama de bloques + descripción de `emisor_v1`/`Drone_v0.1`, canales usados y flujo de datos. Preparado para el ingeniero asesor del proyecto. Describe solo el sistema actual (LoRa + 1 ESC), no incluye interfaces de PC ni la arquitectura futura de control.

---
*Última actualización: 2026-09-07*
