"""
receptor_monitor_gui.py
------------------------
Interfaz de MONITOREO para la placa RECEPTORA (Drone_v0.1, ESP32-S3) del
sistema de vuelo. Se conecta por USB serial a la consola del ESP32 del dron
y muestra en vivo:
  - Armado / desarmado del motor (solo lectura: el control real llega por
    LoRa desde el emisor + RadioMaster Pocket, esta interfaz no controla
    nada, solo refleja lo que el receptor reporta/hace).
  - Velocidad del motor (pulso ESC en us) con barra + historial tipo
    osciloscopio.
  - Calidad del enlace LoRa: RSSI, SNR, % de paquetes perdidos y el rating
    (EXCELENTE / BUENA / DEBIL / MUY DEBIL) que ya imprime el firmware.
  - Consola general con export a CSV/TXT.

Mismos colores que joystick_control_gui.py (verde oscuro) para mantener
consistencia visual entre las dos interfaces del proyecto.

Nota sobre el parser: el receptor imprime por consola el paquete LoRa
recibido ("<contador>:<comando>", ej. "42:1500" / "42:off") y datos de
RSSI/SNR/perdidos/rating (ver estado_proyecto.md). Este script usa
expresiones regulares flexibles (RE_RSSI / RE_SNR / RE_LOSS / RE_RATING /
RE_PAQUETE / RE_ARMADO_KW / RE_DESARMADO_KW / RE_FAILSAFE mas abajo) para
extraer esos datos de cualquier linea que los mencione. Si el formato
exacto que imprime main.c cambia, ajustar esas regex.

Requisitos:
    pip install pyserial

Uso:
    python receptor_monitor_gui.py
"""

import csv
import re
import threading
import time
import tkinter as tk
from collections import deque
from tkinter import ttk, messagebox, filedialog

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise SystemExit("Falta pyserial. Instalalo con:\n\n    pip install pyserial\n")

# ==== Configuracion ====
BAUDRATES = ["115200", "230400", "57600", "9600"]
BAUDRATE_DEFAULT = "115200"

ESC_MIN_US = 1000
ESC_MAX_US = 2000

CHART_HISTORY = 150
WATCHDOG_TIMEOUT_S = 2.0

# ==== Tema: mismo verde oscuro que joystick_control_gui.py ====
COLOR_BG = "#001b0f"
COLOR_PANEL = "#0e4429"
COLOR_BORDER = "#2f9e63"
COLOR_TEXT = "#e8fff0"
COLOR_MUTED = "#8fc7a8"
COLOR_CONSOLE_BG = "#000f08"
COLOR_DANGER = "#ff4d4d"

# ==== Parser flexible del texto que imprime el receptor ====
RE_RSSI = re.compile(r"RSSI[^\-\d]*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
RE_SNR = re.compile(r"SNR[^\-\d]*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
RE_LOSS = re.compile(r"(?:perdid\w*|loss)[^\d]*(\d+(?:\.\d+)?)\s*%", re.IGNORECASE)
RE_RATING = re.compile(r"\b(EXCELENTE|MUY\s+D[EÉ]BIL|D[EÉ]BIL|BUENA)\b", re.IGNORECASE)
RE_PAQUETE = re.compile(r"\b(\d+)\s*:\s*(off|\d{3,4})\b", re.IGNORECASE)
RE_MOTOR_US = re.compile(r"(\d{3,4})\s*us\b", re.IGNORECASE)
RE_ARMADO_KW = re.compile(r"\bARMADO\b", re.IGNORECASE)
RE_DESARMADO_KW = re.compile(r"\bDESARMADO\b", re.IGNORECASE)
RE_FAILSAFE = re.compile(r"failsafe", re.IGNORECASE)

RATING_COLOR = {
    "EXCELENTE": COLOR_BORDER,
    "BUENA": COLOR_BORDER,
    "DEBIL": COLOR_DANGER,
    "DÉBIL": COLOR_DANGER,
    "MUY DEBIL": COLOR_DANGER,
    "MUY DÉBIL": COLOR_DANGER,
}


class ArmadoIndicator:
    """Luz grande tipo semaforo: verde solido = ARMADO, rojo = DESARMADO,
    gris = todavia sin datos del receptor."""

    def __init__(self, parent):
        self.frame = ttk.LabelFrame(parent, text="Estado del motor (receptor)")

        body = ttk.Frame(self.frame)
        body.pack(expand=True, pady=14)

        self.canvas = tk.Canvas(body, width=100, height=100, bg=COLOR_CONSOLE_BG,
                                 highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.canvas.pack()
        self.light = self.canvas.create_oval(10, 10, 90, 90, fill=COLOR_MUTED, outline="")

        self.texto_var = tk.StringVar(value="SIN DATOS")
        self.label = ttk.Label(body, textvariable=self.texto_var, font=("Segoe UI", 15, "bold"),
                                foreground=COLOR_MUTED)
        self.label.pack(pady=(10, 0))

    def actualizar(self, armado, con_datos):
        if not con_datos:
            color, texto, fg = COLOR_MUTED, "SIN DATOS", COLOR_MUTED
        elif armado:
            color, texto, fg = COLOR_BORDER, "ARMADO", COLOR_TEXT
        else:
            color, texto, fg = COLOR_DANGER, "DESARMADO", COLOR_DANGER
        self.canvas.itemconfig(self.light, fill=color)
        self.texto_var.set(texto)
        self.label.configure(foreground=fg)

    def grid(self, **kw):
        self.frame.grid(**kw)


class VelocidadGauge:
    """Barra horizontal con la velocidad actual del motor (pulso ESC en us)."""

    def __init__(self, parent):
        self.frame = ttk.LabelFrame(parent, text=f"Velocidad del motor ({ESC_MIN_US}-{ESC_MAX_US} us)")
        self._us = None

        self.canvas = tk.Canvas(self.frame, height=24, bg=COLOR_CONSOLE_BG, highlightthickness=1,
                                 highlightbackground=COLOR_BORDER)
        self.canvas.pack(fill="x", padx=10, pady=(14, 6))
        self.bar_id = self.canvas.create_rectangle(0, 0, 0, 24, fill=COLOR_BORDER, outline="")
        self.label_id = self.canvas.create_text(6, 12, text="--", fill=COLOR_TEXT,
                                                  font=("Consolas", 9, "bold"), anchor="w")
        self.canvas.bind("<Configure>", lambda e: self._redibujar())

        self.detalle_var = tk.StringVar(value="Sin datos")
        ttk.Label(self.frame, textvariable=self.detalle_var, foreground=COLOR_MUTED).pack(
            anchor="w", padx=10, pady=(0, 12)
        )

    def actualizar(self, us):
        self._us = us
        if us is None:
            self.detalle_var.set("Sin datos")
        else:
            pct = 100.0 * (us - ESC_MIN_US) / (ESC_MAX_US - ESC_MIN_US)
            pct = max(0.0, min(100.0, pct))
            self.detalle_var.set(f"{us} us  ({pct:.0f}%)")
        self._redibujar()

    def _redibujar(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 2 or h < 2:
            return
        if self._us is None:
            ancho, texto = 0, "--"
        else:
            pct = 100.0 * (self._us - ESC_MIN_US) / (ESC_MAX_US - ESC_MIN_US)
            pct = max(0.0, min(100.0, pct))
            ancho = max(2, int(w * pct / 100))
            texto = f"{self._us} us"
        self.canvas.coords(self.bar_id, 0, 0, ancho, h)
        self.canvas.coords(self.label_id, 6, h // 2)
        self.canvas.itemconfig(self.label_id, text=texto)

    def grid(self, **kw):
        self.frame.grid(**kw)


class SignalMeter:
    """Barra de calidad de senal + detalle (RSSI/SNR/perdidos) con datos
    reales que reporta el receptor por el enlace LoRa."""

    def __init__(self, parent):
        self.frame = ttk.LabelFrame(parent, text="Calidad de senal (enlace LoRa)")
        self._pct = None
        self._color = COLOR_BORDER

        self.canvas = tk.Canvas(self.frame, height=22, bg=COLOR_CONSOLE_BG, highlightthickness=1,
                                 highlightbackground=COLOR_BORDER)
        self.canvas.pack(fill="x", padx=10, pady=(14, 4))
        self.bar_id = self.canvas.create_rectangle(0, 0, 0, 22, fill=COLOR_BORDER, outline="")
        self.label_id = self.canvas.create_text(6, 11, text="—", fill=COLOR_TEXT,
                                                  font=("Consolas", 9, "bold"), anchor="w")
        self.canvas.bind("<Configure>", lambda e: self._redibujar())

        self.detalle_var = tk.StringVar(value="Sin datos")
        ttk.Label(self.frame, textvariable=self.detalle_var, foreground=COLOR_MUTED,
                  justify="left").pack(anchor="w", padx=10, pady=(0, 12))

    def actualizar(self, pct, detalle, peligro=False):
        self._pct = pct
        self._color = COLOR_DANGER if peligro else COLOR_BORDER
        self.detalle_var.set(detalle)
        self._redibujar()

    def _redibujar(self):
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 2 or h < 2:
            return
        ancho = 0 if not self._pct else max(2, int(w * min(100, self._pct) / 100))
        self.canvas.coords(self.bar_id, 0, 0, ancho, h)
        self.canvas.itemconfig(self.bar_id, fill=self._color)
        self.canvas.coords(self.label_id, 6, h // 2)
        texto = "—" if self._pct is None else f"{self._pct:.0f}%"
        self.canvas.itemconfig(self.label_id, text=texto)

    def grid(self, **kw):
        self.frame.grid(**kw)


class Osciloscopio:
    """Grafica de linea en vivo (canvas) de la velocidad del motor, en us."""

    def __init__(self, parent, titulo, historial=CHART_HISTORY):
        self.buffer = deque(maxlen=historial)
        self.historial = historial

        self.frame = tk.Frame(parent, bg=COLOR_BG, highlightthickness=1, highlightbackground=COLOR_BORDER)
        tk.Label(
            self.frame, text=titulo, bg=COLOR_BG, fg=COLOR_TEXT,
            font=("Consolas", 10, "bold"), anchor="w",
        ).pack(fill="x", padx=6, pady=(4, 0))

        self.canvas = tk.Canvas(self.frame, bg=COLOR_CONSOLE_BG, highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.canvas.pack(fill="both", expand=True, padx=6, pady=(2, 6))
        self.canvas.bind("<Configure>", lambda e: self._redibujar())

    def push(self, valor_us):
        self.buffer.append(valor_us)
        self._redibujar()

    def _redibujar(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 2 or h < 2:
            return

        margen = 14
        alto_util = h - 2 * margen

        for us in (ESC_MIN_US, (ESC_MIN_US + ESC_MAX_US) // 2, ESC_MAX_US):
            frac = (us - ESC_MIN_US) / (ESC_MAX_US - ESC_MIN_US)
            y = margen + alto_util - frac * alto_util
            c.create_line(0, y, w, y, fill=COLOR_BORDER, dash=(2, 3))
            c.create_text(4, y, text=str(us), fill=COLOR_MUTED, font=("Consolas", 8), anchor="sw")

        puntos_validos = [v for v in self.buffer if v is not None]
        if len(puntos_validos) < 2:
            return

        n = self.historial
        pts = []
        for idx, valor in enumerate(self.buffer):
            if valor is None:
                continue
            x = w * (idx / max(1, n - 1))
            frac = (valor - ESC_MIN_US) / (ESC_MAX_US - ESC_MIN_US)
            frac = max(0.0, min(1.0, frac))
            y = margen + alto_util - frac * alto_util
            pts.extend([x, y])

        if len(pts) >= 4:
            c.create_line(*pts, fill=COLOR_BORDER, width=1.6, smooth=True)

    def grid(self, **kw):
        self.frame.grid(**kw)


class ReceptorMonitorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Monitor Receptor - Drone_v0.1")
        self.root.geometry("1200x750")
        self.root.minsize(1000, 600)
        self.root.configure(bg=COLOR_BG)
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass

        self._apply_theme()

        self.ser = None
        self.read_thread = None
        self.stop_thread = False

        self.ultimo_dato_recibido = None
        self._watchdog_alerta_enviada = False

        # Estado derivado de lo que reporta el receptor
        self.armado = False
        self.motor_us = None
        self.rssi = None
        self.snr = None
        self.loss_pct = None
        self.rating = None

        self.registro = []  # (timestamp, canal, mensaje) -> exportable

        self._build_ui()
        self._refresh_ports()
        self._poll_loop()

    # -------------------------------------------------------- Tema ----
    def _apply_theme(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=COLOR_BG, foreground=COLOR_TEXT,
                         fieldbackground=COLOR_PANEL, bordercolor=COLOR_BORDER,
                         lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                         font=("Segoe UI", 9))
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TLabelframe", background=COLOR_BG, bordercolor=COLOR_BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR_TEXT,
                         font=("Segoe UI", 9, "bold"))
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("TButton", background=COLOR_PANEL, foreground=COLOR_TEXT,
                         bordercolor=COLOR_BORDER, focuscolor=COLOR_BG, padding=6)
        style.map(
            "TButton",
            background=[("active", COLOR_BORDER), ("disabled", COLOR_BG)],
            foreground=[("disabled", COLOR_MUTED)],
        )
        style.configure("TCombobox", fieldbackground=COLOR_PANEL, background=COLOR_PANEL,
                         foreground=COLOR_TEXT, arrowcolor=COLOR_TEXT)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COLOR_PANEL)],
            foreground=[("readonly", COLOR_TEXT)],
        )

        self.root.option_add("*TCombobox*Listbox*Background", COLOR_PANEL)
        self.root.option_add("*TCombobox*Listbox*Foreground", COLOR_TEXT)
        self.root.option_add("*TCombobox*Listbox*selectBackground", COLOR_BORDER)
        self.root.option_add("*TCombobox*Listbox*selectForeground", COLOR_TEXT)

    # ---------------------------------------------------------- UI ----
    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)   # sidebar
        self.root.columnconfigure(1, weight=3)   # area central
        self.root.rowconfigure(0, weight=1)

        # ==================================================== SIDEBAR ====
        sidebar = ttk.Frame(self.root)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(10, 6), pady=10)
        sidebar.columnconfigure(0, weight=1)

        conn_frame = ttk.LabelFrame(sidebar, text="Conexion ESP32 (receptor / Drone_v0.1)")
        conn_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        conn_frame.columnconfigure(0, weight=1)

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port_var, state="readonly")
        self.port_combo.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))

        self.baud_var = tk.StringVar(value=BAUDRATE_DEFAULT)
        baud_combo = ttk.Combobox(conn_frame, textvariable=self.baud_var, values=BAUDRATES,
                                   state="readonly", width=10)
        baud_combo.grid(row=1, column=0, sticky="w", padx=8, pady=(0, 6))

        btn_row = ttk.Frame(conn_frame)
        btn_row.grid(row=2, column=0, sticky="ew", padx=8)
        ttk.Button(btn_row, text="Actualizar", command=self._refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(btn_row, text="Conectar", command=self._toggle_connection)
        self.connect_btn.pack(side="left", padx=6)

        self.status_label = ttk.Label(conn_frame, text="Desconectado", foreground=COLOR_DANGER)
        self.status_label.grid(row=3, column=0, sticky="w", padx=8, pady=(8, 2))

        self.watchdog_label = ttk.Label(conn_frame, text="Watchdog: —", foreground=COLOR_MUTED)
        self.watchdog_label.grid(row=4, column=0, sticky="w", padx=8, pady=(0, 8))

        info_frame = ttk.LabelFrame(sidebar, text="Nota")
        info_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(
            info_frame,
            text="Esta interfaz solo MONITOREA al receptor.\n"
                 "El control real llega por LoRa desde el\n"
                 "emisor + RadioMaster Pocket.",
            foreground=COLOR_MUTED, justify="left",
        ).pack(anchor="w", padx=8, pady=8)

        sidebar.rowconfigure(2, weight=1)
        ttk.Frame(sidebar).grid(row=2, column=0, sticky="nsew")  # espaciador

        # ================================================ AREA CENTRAL ====
        central = ttk.Frame(self.root)
        central.grid(row=0, column=1, sticky="nsew", padx=(6, 10), pady=10)
        central.columnconfigure(0, weight=1)
        central.columnconfigure(1, weight=1)
        central.rowconfigure(0, weight=1)
        central.rowconfigure(1, weight=2)
        central.rowconfigure(2, weight=1)

        # --- Fila 0: armado + velocidad ---
        self.armado_ind = ArmadoIndicator(central)
        self.armado_ind.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))

        self.vel_gauge = VelocidadGauge(central)
        self.vel_gauge.grid(row=0, column=1, sticky="nsew", pady=(0, 6))

        # --- Fila 1: senal + osciloscopio ---
        self.signal_meter = SignalMeter(central)
        self.signal_meter.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))

        self.osc = Osciloscopio(central, "OSCILOSCOPIO - VELOCIDAD DEL MOTOR (us)")
        self.osc.grid(row=1, column=1, sticky="nsew", pady=(0, 6))

        # --- Fila 2: consola general ---
        bottom_frame = ttk.LabelFrame(central, text="Consola del receptor (mensajes en vivo)")
        bottom_frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        bottom_frame.columnconfigure(0, weight=1)
        bottom_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(bottom_frame, height=6, state="disabled", bg=COLOR_CONSOLE_BG, fg=COLOR_TEXT,
                                 insertbackground=COLOR_TEXT, font=("Consolas", 9), relief="flat",
                                 wrap="word",
                                 highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(6, 4), pady=6)

        ttk.Button(bottom_frame, text="⭳ Exportar log...", command=self._exportar_log).grid(
            row=0, column=1, sticky="ne", padx=(0, 6), pady=6
        )

    # ------------------------------------------------------ Serial ----
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo["values"] = ports
        if ports:
            self.port_combo.current(0)

    def _toggle_connection(self):
        if self.ser and self.ser.is_open:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("Puerto no seleccionado", "Selecciona un puerto COM primero.")
            return
        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = int(BAUDRATE_DEFAULT)
        try:
            self.ser = serial.Serial(port, baud, timeout=0.2)
        except Exception as exc:
            messagebox.showerror("Error de conexion", str(exc))
            return

        self.status_label.config(text=f"Conectado ({port})", foreground=COLOR_TEXT)
        self.connect_btn.config(text="Desconectar")

        self.ultimo_dato_recibido = None
        self._watchdog_alerta_enviada = False
        self.armado = False
        self.motor_us = None
        self.rssi = self.snr = self.loss_pct = self.rating = None

        self.stop_thread = False
        self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.read_thread.start()
        self._log(f"Conectado a {port} @ {baud} baud. Esperando datos del receptor...")

    def _disconnect(self):
        self.stop_thread = True
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.status_label.config(text="Desconectado", foreground=COLOR_DANGER)
        self.connect_btn.config(text="Conectar")
        self.ultimo_dato_recibido = None
        self.armado = False
        self.motor_us = None

    def _read_loop(self):
        while not self.stop_thread and self.ser:
            try:
                data = self.ser.readline()
            except Exception:
                break
            if data:
                text = data.decode(errors="replace").rstrip()
                if text:
                    self.ultimo_dato_recibido = time.time()
                    self._procesar_linea(text)
                    self._log(text)
            time.sleep(0.02)

    # ------------------------------------------------------ Parser ----
    def _procesar_linea(self, texto):
        m = RE_RSSI.search(texto)
        if m:
            self.rssi = float(m.group(1))

        m = RE_SNR.search(texto)
        if m:
            self.snr = float(m.group(1))

        m = RE_LOSS.search(texto)
        if m:
            self.loss_pct = float(m.group(1))

        m = RE_RATING.search(texto)
        if m:
            self.rating = re.sub(r"\s+", " ", m.group(1).upper())

        if RE_FAILSAFE.search(texto):
            self.armado = False
            self.motor_us = None
            return

        if RE_ARMADO_KW.search(texto):
            self.armado = True
        elif RE_DESARMADO_KW.search(texto):
            self.armado = False
            self.motor_us = None

        m = RE_PAQUETE.search(texto)
        if m:
            comando = m.group(2).lower()
            if comando == "off":
                self.armado = False
                self.motor_us = None
            else:
                try:
                    valor = int(comando)
                except ValueError:
                    valor = None
                if valor is not None and ESC_MIN_US - 50 <= valor <= ESC_MAX_US + 50:
                    self.motor_us = valor
                    self.armado = True

        # Formato real observado en el firmware: "Throttle -> 1200 us"
        m = RE_MOTOR_US.search(texto)
        if m:
            try:
                valor = int(m.group(1))
            except ValueError:
                valor = None
            if valor is not None and ESC_MIN_US - 100 <= valor <= ESC_MAX_US + 100:
                self.motor_us = valor

    # -------------------------------------------------------- Poll ----
    def _poll_loop(self):
        conectado = bool(self.ser and self.ser.is_open)

        if not conectado:
            self.armado_ind.actualizar(False, con_datos=False)
            self.vel_gauge.actualizar(None)
        else:
            con_datos = self.ultimo_dato_recibido is not None
            self.armado_ind.actualizar(self.armado, con_datos=con_datos)
            self.vel_gauge.actualizar(self.motor_us if self.armado else None)
            self.osc.push(self.motor_us if self.armado else None)

        self._actualizar_watchdog_y_senal(conectado)
        self.root.after(100, self._poll_loop)

    def _actualizar_watchdog_y_senal(self, conectado):
        if not conectado:
            self.watchdog_label.config(text="Watchdog: —", foreground=COLOR_MUTED)
            self._watchdog_alerta_enviada = False
            self.signal_meter.actualizar(None, "Sin conexion")
            return

        if self.ultimo_dato_recibido is None:
            self.watchdog_label.config(text="Watchdog: esperando datos...", foreground=COLOR_MUTED)
            self.signal_meter.actualizar(None, "Esperando datos del receptor...")
            return

        elapsed = time.time() - self.ultimo_dato_recibido
        if elapsed <= WATCHDOG_TIMEOUT_S:
            self.watchdog_label.config(text=f"Watchdog: OK ({elapsed:.1f}s)", foreground=COLOR_TEXT)
            self._watchdog_alerta_enviada = False
            peligro = self.rating is not None and RATING_COLOR.get(self.rating) == COLOR_DANGER

            if self.rssi is not None or self.snr is not None:
                pct = self._pct_desde_rssi_snr()
                partes = []
                if self.rssi is not None:
                    partes.append(f"RSSI {self.rssi:.0f} dBm")
                if self.snr is not None:
                    partes.append(f"SNR {self.snr:.1f} dB")
                if self.loss_pct is not None:
                    partes.append(f"perdidos {self.loss_pct:.0f}%")
                if self.rating:
                    partes.append(self.rating)
                detalle = "  |  ".join(partes)
            else:
                # Sin RSSI/SNR todavia: usar frescura del dato como proxy.
                pct = max(5.0, 100.0 * (1 - elapsed / WATCHDOG_TIMEOUT_S))
                detalle = f"Ultimo dato hace {elapsed:.1f}s (sin RSSI/SNR aun)"

            self.signal_meter.actualizar(pct, detalle, peligro=peligro)
        else:
            self.watchdog_label.config(text=f"Watchdog: SIN RESPUESTA ({elapsed:.1f}s)", foreground=COLOR_DANGER)
            if not self._watchdog_alerta_enviada:
                self._log("[watchdog] El receptor dejo de responder.")
                self._watchdog_alerta_enviada = True
            self.signal_meter.actualizar(0, f"Sin respuesta hace {elapsed:.1f}s", peligro=True)

    def _pct_desde_rssi_snr(self):
        """Convierte RSSI/SNR a un porcentaje aproximado para la barra.
        Rango de referencia tomado de las pruebas reales del proyecto
        (RSSI entre -60 y -73 dBm, SNR ~8.5-9.5 dB = senal BUENA)."""
        partes = []
        if self.rssi is not None:
            # -50 dBm o mejor -> 100%, -100 dBm -> 0%
            p = (self.rssi + 100) / 50 * 100
            partes.append(max(0.0, min(100.0, p)))
        if self.snr is not None:
            # -20 dB -> 0%, +10 dB o mejor -> 100%
            p = (self.snr + 20) / 30 * 100
            partes.append(max(0.0, min(100.0, p)))
        if not partes:
            return 50.0
        return sum(partes) / len(partes)

    # ------------------------------------------------------------ Log ----
    def _registrar(self, canal, mensaje):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.registro.append((ts, canal, mensaje))

    def _log(self, texto):
        ts = time.strftime("%H:%M:%S")
        self.log_text.config(state="normal")
        self.log_text.insert("end", f"[{ts}] {texto}\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        self._registrar("receptor", texto)

    def _exportar_log(self):
        if not self.registro:
            messagebox.showinfo("Exportar log", "Todavia no hay nada para exportar.")
            return
        nombre_sugerido = f"log_receptor_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        ruta = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Texto", "*.txt")],
            initialfile=nombre_sugerido,
        )
        if not ruta:
            return
        try:
            if ruta.lower().endswith(".csv"):
                with open(ruta, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(["timestamp", "canal", "mensaje"])
                    writer.writerows(self.registro)
            else:
                with open(ruta, "w", encoding="utf-8") as f:
                    for ts, canal, mensaje in self.registro:
                        f.write(f"[{ts}] {canal}: {mensaje}\n")
        except Exception as exc:
            messagebox.showerror("Error al exportar", str(exc))
            return
        self._log(f"Log exportado a {ruta}")

    def on_close(self):
        self._disconnect()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ReceptorMonitorApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
