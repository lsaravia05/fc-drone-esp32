"""
joystick_control_gui.py
------------------------
Interfaz grafica estilo "radiocontrol" para pruebas de banco del ESC/motor
via ESP32-S3, controlada con la RadioMaster Pocket (modo USB Joystick).

Tema visual: verde oscuro, con paneles intercalados en dos tonos de verde.

Layout (pantalla completa, sidebar + area central):
  - Sidebar izquierda: conexion + watchdog, calidad de senal, estado del
    joystick, calibracion de ejes (incluye elegir que boton fisico arma/
    desarma, con detector visual de botones), armado del motor y parada
    de emergencia.
  - Area central: sticks visuales (Throttle/Yaw y Pitch/Roll) junto al
    osciloscopio en vivo del throttle, 4 sub-pantallas con historial por
    eje (Throttle/Pitch/Roll/Yaw), y consola general con exportar a
    CSV/TXT.

El throttle se envia al ESP32-S3 por el mismo protocolo serial de main.c
(comandos de texto: "on" / "off" / <numero> en microsegundos).

Requisitos:
    pip install pygame-ce pyserial

Uso:
    python joystick_control_gui.py
"""

import csv
import json
import os
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

try:
    import pygame
except ImportError:
    raise SystemExit("Falta pygame-ce. Instalalo con:\n\n    pip install pygame-ce\n")

# ==== Configuracion ====
BAUDRATE = 115200

# Mapeo de ejes AETR por defecto (se puede recalibrar desde la UI; ver
# "Calibrar ejes..." en el panel de control -> se guarda en CALIB_FILE)
AXIS_ROLL = 0
AXIS_PITCH = 1
AXIS_THROTTLE = 2
AXIS_YAW = 3

THROTTLE_MIN_RAW = -1.0
THROTTLE_MAX_RAW = 1.0
INVERT_THROTTLE = False

ESC_MIN_US = 1000
ESC_MAX_US = 2000

ARM_BUTTON = 0          # boton fisico del control para armar/desarmar. None desactiva
SEND_INTERVAL = 0.1
DEADZONE_US = 5

# El emisor (main.c) retransmite el comando_actual por LoRa cada 200ms sin
# parar mientras esa sea la variable compartida (ver tarea_transmision).
# Si mandamos "on" y en menos de 200ms lo pisamos con un numero, es posible
# que "on" nunca llegue a transmitirse ni una vez -> el receptor se queda
# "sin armar" si ese unico intento se pierde por LoRa. Por eso mantenemos
# "on" quieto varios ciclos (5 x 200ms) antes de mandar el primer throttle.
ARM_HOLD_S = 1.0

STICK_SIZE = 220         # tamano en pixeles de cada recuadro de stick
CHART_HISTORY = 150      # muestras del osciloscopio (a SEND_INTERVAL, ~15s)
WATCHDOG_TIMEOUT_S = 3.0

CALIB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "joystick_calib.json")

# ==== Tema: solo negro y el morado del logo AESS, intercalados ====
COLOR_BG = "#001b0f"        # verde muy oscuro (fondo general y paneles oscuros)
COLOR_PANEL = "#0e4429"     # verde mas claro (paneles alternos)
COLOR_BORDER = "#2f9e63"    # verde brillante, para bordes sobre cualquiera de los dos
COLOR_TEXT = "#e8fff0"      # texto principal (blanco con tinte verde)
COLOR_MUTED = "#8fc7a8"     # texto secundario / deshabilitado
COLOR_CONSOLE_BG = "#000f08"  # fondo de "pantallas" (consolas, osciloscopio, sticks)
COLOR_DANGER = "#ff4d4d"    # rojo reservado solo para peligro: desconectado, desarmado,
                             # sin joystick, watchdog caido y parada de emergencia



def mapear_us(valor_crudo, invertir=False):
    valor_crudo = max(min(valor_crudo, THROTTLE_MAX_RAW), THROTTLE_MIN_RAW)
    frac = (valor_crudo - THROTTLE_MIN_RAW) / (THROTTLE_MAX_RAW - THROTTLE_MIN_RAW)
    if invertir:
        frac = 1.0 - frac
    return int(ESC_MIN_US + frac * (ESC_MAX_US - ESC_MIN_US))


class StickWidget:
    """Recuadro cuadrado con cruz y un punto que representa un stick de 2 ejes."""

    def __init__(self, parent, titulo, color, label_x, label_y):
        self.frame = ttk.Frame(parent)
        ttk.Label(self.frame, text=titulo, font=("Segoe UI", 10, "bold")).pack()

        self.canvas = tk.Canvas(self.frame, width=STICK_SIZE, height=STICK_SIZE, bg=COLOR_CONSOLE_BG, highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.canvas.pack(pady=4)

        c = STICK_SIZE // 2
        self.canvas.create_line(0, c, STICK_SIZE, c, fill=COLOR_BORDER)
        self.canvas.create_line(c, 0, c, STICK_SIZE, fill=COLOR_BORDER)
        self.canvas.create_oval(8, 8, STICK_SIZE - 8, STICK_SIZE - 8, outline=COLOR_BORDER)

        r = 11
        self.dot = self.canvas.create_oval(c - r, c - r, c + r, c + r, fill=color, outline="")

        info = ttk.Frame(self.frame)
        info.pack()
        self.x_var = tk.StringVar(value="+0.00")
        self.y_var = tk.StringVar(value="+0.00")
        ttk.Label(info, text=f"{label_x}:").grid(row=0, column=0, sticky="e")
        ttk.Label(info, textvariable=self.x_var, width=6).grid(row=0, column=1)
        ttk.Label(info, text=f"{label_y}:").grid(row=1, column=0, sticky="e")
        ttk.Label(info, textvariable=self.y_var, width=6).grid(row=1, column=1)

    def actualizar(self, x_val, y_val):
        c = STICK_SIZE // 2
        margen = 20
        px = c + x_val * (c - margen)
        py = c - y_val * (c - margen)  # invertido: +1 hacia arriba
        r = 11
        self.canvas.coords(self.dot, px - r, py - r, px + r, py + r)
        self.x_var.set(f"{x_val:+.2f}")
        self.y_var.set(f"{y_val:+.2f}")

    def pack(self, **kw):
        self.frame.pack(**kw)


class AxisConsole:
    """Mini consola con historial (tipo log) para un solo eje: titulo coloreado
    + caja de texto que va agregando lineas con timestamp y el valor en vivo."""

    MAX_LINES = 200

    def __init__(self, parent, titulo, bg):
        self.bg = bg
        self.frame = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=COLOR_BORDER)

        tk.Label(
            self.frame, text=titulo, bg=bg, fg=COLOR_TEXT,
            font=("Consolas", 10, "bold"), anchor="w",
        ).pack(fill="x", padx=6, pady=(4, 0))

        self.text = tk.Text(
            self.frame, height=7, width=26, state="disabled",
            bg=COLOR_CONSOLE_BG, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
            font=("Consolas", 9), relief="flat",
            highlightthickness=1, highlightbackground=COLOR_BORDER,
        )
        self.text.pack(fill="both", expand=True, padx=6, pady=(2, 6))

    def push(self, valor_texto):
        ts = time.strftime("%H:%M:%S")
        linea = f"[{ts}] {valor_texto}"
        self.text.config(state="normal")
        self.text.insert("end", linea + "\n")
        num_lineas = int(self.text.index("end-1c").split(".")[0])
        if num_lineas > self.MAX_LINES:
            self.text.delete("1.0", f"{num_lineas - self.MAX_LINES}.0")
        self.text.see("end")
        self.text.config(state="disabled")

    def grid(self, **kw):
        self.frame.grid(**kw)


class Osciloscopio:
    """Grafica de linea en vivo (canvas) del throttle enviado, en microsegundos."""

    def __init__(self, parent, historial=CHART_HISTORY):
        self.buffer = deque(maxlen=historial)
        self.historial = historial

        self.frame = tk.Frame(parent, bg=COLOR_BG, highlightthickness=1, highlightbackground=COLOR_BORDER)
        tk.Label(
            self.frame, text="OSCILOSCOPIO - THROTTLE EN VIVO (us)", bg=COLOR_BG,
            fg=COLOR_TEXT, font=("Consolas", 10, "bold"), anchor="w",
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

        if len(self.buffer) < 2:
            return

        n = self.historial
        pts = []
        for idx, valor in enumerate(self.buffer):
            x = w * (idx / max(1, n - 1))
            frac = (valor - ESC_MIN_US) / (ESC_MAX_US - ESC_MIN_US)
            frac = max(0.0, min(1.0, frac))
            y = margen + alto_util - frac * alto_util
            pts.extend([x, y])

        if len(pts) >= 4:
            c.create_line(*pts, fill=COLOR_BORDER, width=1.6, smooth=True)

    def grid(self, **kw):
        self.frame.grid(**kw)


class SignalMeter:
    """Barra de calidad de senal: por ahora, un proxy calculado a partir de
    que tan reciente es el ultimo dato recibido del ESP32 por serial (no hay
    RSSI/SNR real todavia porque el enlace LoRa aun no esta implementado en
    el firmware). Cuando main.c mande RSSI/SNR, este mismo panel se puede
    alimentar con esos valores en vez del proxy."""

    def __init__(self, parent, bg):
        self.bg = bg
        self._pct = None
        self._color = COLOR_BORDER

        self.frame = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=COLOR_BORDER)

        tk.Label(
            self.frame, text="CALIDAD DE SEÑAL (enlace ESP32)", bg=bg, fg=COLOR_TEXT,
            font=("Consolas", 9, "bold"), anchor="w",
        ).pack(fill="x", padx=8, pady=(6, 2))

        self.canvas = tk.Canvas(self.frame, height=18, bg=COLOR_CONSOLE_BG, highlightthickness=1,
                                 highlightbackground=COLOR_BORDER)
        self.canvas.pack(fill="x", padx=8, pady=(0, 4))
        self.bar_id = self.canvas.create_rectangle(0, 0, 0, 18, fill=COLOR_BORDER, outline="")
        self.label_id = self.canvas.create_text(6, 9, text="—", fill=COLOR_TEXT, font=("Consolas", 8, "bold"), anchor="w")
        self.canvas.bind("<Configure>", lambda e: self._redibujar())

        self.detalle_var = tk.StringVar(value="Sin datos")
        tk.Label(self.frame, textvariable=self.detalle_var, bg=bg, fg=COLOR_MUTED,
                 font=("Consolas", 8), anchor="w").pack(fill="x", padx=8, pady=(0, 6))

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


class JoystickEscApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Control Joystick - Drone_v0.1")
        self.root.geometry("1200x750")
        self.root.resizable(True, True)
        self.root.minsize(1000, 600)
        self.root.configure(bg=COLOR_BG)
        try:
            self.root.state("zoomed")  # abre maximizada (Windows)
        except tk.TclError:
            pass

        self._apply_theme()
        self._cargar_calibracion()

        self.ser = None
        self.read_thread = None
        self.stop_thread = False

        self.armado = False
        self.ultimo_us_enviado = None
        self.ultimo_envio = 0.0
        self.ultimo_envio_ejes = 0.0
        self.boton_anterior = False
        self.arm_hold_hasta = 0.0

        self.ultimo_dato_recibido = None
        self._watchdog_alerta_enviada = False

        self.registro = []  # (timestamp, canal, mensaje) -> exportable

        pygame.init()
        pygame.joystick.init()
        self.joystick = None
        self.num_axes = 0
        self.num_buttons = 0
        self._calib_win = None

        self._build_ui()
        self._refresh_ports()
        self._init_joystick()
        self.root.bind_all("<space>", self._emergency_stop)
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
        style.configure("TSeparator", background=COLOR_BORDER)
        style.configure("TCheckbutton", background=COLOR_BG, foreground=COLOR_TEXT)
        style.map("TCheckbutton", background=[("active", COLOR_BG)])

        # Variante "Prp" (morada): mismos paneles pero con fondo morado, para
        # poder intercalar paneles negros y morados por toda la interfaz.
        style.configure("Prp.TFrame", background=COLOR_PANEL)
        style.configure("Prp.TLabelframe", background=COLOR_PANEL, bordercolor=COLOR_BORDER, relief="solid")
        style.configure("Prp.TLabelframe.Label", background=COLOR_PANEL, foreground=COLOR_TEXT,
                         font=("Segoe UI", 9, "bold"))
        style.configure("Prp.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT)

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
        sidebar.rowconfigure(4, weight=1)  # espaciador: empuja la emergencia al fondo

        # --- Conexion + watchdog (panel morado) ---
        conn_frame = ttk.LabelFrame(sidebar, text="Conexion ESP32", style="Prp.TLabelframe")
        conn_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        conn_frame.columnconfigure(0, weight=1)

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(conn_frame, textvariable=self.port_var, state="readonly")
        self.port_combo.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))

        btn_row = ttk.Frame(conn_frame, style="Prp.TFrame")
        btn_row.grid(row=1, column=0, sticky="ew", padx=8)
        ttk.Button(btn_row, text="Actualizar", command=self._refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(btn_row, text="Conectar", command=self._toggle_connection)
        self.connect_btn.pack(side="left", padx=6)

        self.status_label = ttk.Label(conn_frame, text="Desconectado", foreground=COLOR_DANGER, style="Prp.TLabel")
        self.status_label.grid(row=2, column=0, sticky="w", padx=8, pady=(8, 2))

        self.watchdog_label = ttk.Label(conn_frame, text="Watchdog: —", foreground=COLOR_MUTED, style="Prp.TLabel")
        self.watchdog_label.grid(row=3, column=0, sticky="w", padx=8, pady=(0, 8))

        # --- Calidad de senal (panel negro) ---
        self.signal_meter = SignalMeter(sidebar, bg=COLOR_BG)
        self.signal_meter.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        # --- Estado del joystick + calibracion (panel morado) ---
        js_frame = ttk.LabelFrame(sidebar, text="Joystick (RadioMaster Pocket)", style="Prp.TLabelframe")
        js_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))

        self.js_status_label = ttk.Label(js_frame, text="Buscando joystick...", wraplength=230, justify="left",
                                          style="Prp.TLabel")
        self.js_status_label.pack(anchor="w", padx=8, pady=(8, 6), fill="x")

        js_btn_row = ttk.Frame(js_frame, style="Prp.TFrame")
        js_btn_row.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(js_btn_row, text="Redetectar", command=self._init_joystick).pack(side="left")
        ttk.Button(js_btn_row, text="Calibrar ejes...", command=self._abrir_calibracion).pack(side="left", padx=6)

        # --- Motor / armado (panel negro) ---
        ctrl_frame = ttk.LabelFrame(sidebar, text=f"Motor (throttle -> {ESC_MIN_US}-{ESC_MAX_US} us)")
        ctrl_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))

        self.arm_btn = ttk.Button(ctrl_frame, text="ARMAR", command=self._toggle_arm, state="disabled")
        self.arm_btn.pack(padx=10, pady=(10, 6), ipady=6, fill="x")

        self.arm_status_label = ttk.Label(ctrl_frame, text="DESARMADO", foreground=COLOR_DANGER,
                                           font=("Segoe UI", 12, "bold"))
        self.arm_status_label.pack(pady=(0, 4))

        self.us_value_label = ttk.Label(ctrl_frame, text="-- us", font=("Segoe UI", 14, "bold"))
        self.us_value_label.pack(pady=(0, 4))

        self.arm_button_hint_var = tk.StringVar(
            value=f"Boton {self.arm_button} del control\ntambien arma/desarma"
        )
        ttk.Label(ctrl_frame, textvariable=self.arm_button_hint_var, foreground=COLOR_MUTED,
                  justify="center").pack(pady=(0, 10))

        ttk.Frame(sidebar).grid(row=4, column=0, sticky="nsew")  # espaciador

        # --- Parada de emergencia ---
        self.emg_btn = tk.Button(
            sidebar, text="⏻  PARADA DE EMERGENCIA\n(barra espaciadora)",
            command=self._emergency_stop, bg="#3a0000", fg="#ffb3b3",
            activebackground="#5a0000", activeforeground="#ffdddd",
            font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2", justify="center",
        )
        self.emg_btn.grid(row=5, column=0, sticky="ew", pady=(0, 4), ipady=10)

        # ================================================ AREA CENTRAL ====
        central = ttk.Frame(self.root)
        central.grid(row=0, column=1, sticky="nsew", padx=(6, 10), pady=10)
        central.columnconfigure(0, weight=3)   # sticks: mas anchos
        central.columnconfigure(1, weight=2)   # osciloscopio: mas chico
        central.rowconfigure(0, weight=2)
        central.rowconfigure(1, weight=2)
        central.rowconfigure(2, weight=1)

        # --- Sticks visuales (grandes, lado a lado; panel morado) ---
        sticks_frame = ttk.LabelFrame(central, text="Control (RadioMaster Pocket)", style="Prp.TLabelframe")
        sticks_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))

        sticks_body = ttk.Frame(sticks_frame)
        sticks_body.pack(expand=True)

        self.stick_izq = StickWidget(sticks_body, "Throttle / Yaw", COLOR_BORDER, "Yaw", "Thr")
        self.stick_izq.pack(side="left", padx=16, pady=10)

        self.stick_der = StickWidget(sticks_body, "Pitch / Roll", COLOR_TEXT, "Roll", "Pitch")
        self.stick_der.pack(side="left", padx=16, pady=10)

        # --- Osciloscopio (mas chico, al lado de los sticks) ---
        self.osc = Osciloscopio(central)
        self.osc.grid(row=0, column=1, sticky="nsew", pady=(0, 6))

        ejes_frame = ttk.LabelFrame(central, text="Ejes en vivo")
        ejes_frame.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(0, 6))

        grid_ejes = ttk.Frame(ejes_frame)
        grid_ejes.pack(fill="both", expand=True, padx=6, pady=6)
        for c in range(2):
            grid_ejes.columnconfigure(c, weight=1)
        for r in range(2):
            grid_ejes.rowconfigure(r, weight=1)

        # Tablero de ejes en damero (intercalado morado/negro)
        self.axis_console_throttle = AxisConsole(grid_ejes, "THROTTLE", COLOR_PANEL)
        self.axis_console_throttle.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        self.axis_console_yaw = AxisConsole(grid_ejes, "YAW", COLOR_BG)
        self.axis_console_yaw.grid(row=0, column=1, sticky="nsew", padx=4, pady=4)

        self.axis_console_pitch = AxisConsole(grid_ejes, "PITCH", COLOR_BG)
        self.axis_console_pitch.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        self.axis_console_roll = AxisConsole(grid_ejes, "ROLL", COLOR_PANEL)
        self.axis_console_roll.grid(row=1, column=1, sticky="nsew", padx=4, pady=4)

        bottom_frame = ttk.LabelFrame(central, text="Consola general (mensajes de conexion / estado)", style="Prp.TLabelframe")
        bottom_frame.grid(row=2, column=0, columnspan=2, sticky="nsew")
        bottom_frame.columnconfigure(0, weight=1)
        bottom_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(bottom_frame, height=6, state="disabled", bg=COLOR_CONSOLE_BG, fg=COLOR_TEXT,
                                 insertbackground=COLOR_TEXT, font=("Consolas", 9), relief="flat",
                                 highlightthickness=1, highlightbackground=COLOR_BORDER)
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(6, 4), pady=6)

        ttk.Button(bottom_frame, text="⭳ Exportar log...", command=self._exportar_log).grid(
            row=0, column=1, sticky="ne", padx=(0, 6), pady=6
        )

    # ------------------------------------------------------ Joystick ----
    def _init_joystick(self):
        pygame.joystick.quit()
        pygame.joystick.init()

        if pygame.joystick.get_count() == 0:
            self.joystick = None
            self.num_axes = 0
            self.num_buttons = 0
            self.js_status_label.config(text="No se detecto ningun joystick.", foreground=COLOR_DANGER)
            return

        self.joystick = pygame.joystick.Joystick(0)
        self.joystick.init()
        self.num_axes = self.joystick.get_numaxes()
        self.num_buttons = self.joystick.get_numbuttons()
        self.js_status_label.config(
            text=f"Detectado: {self.joystick.get_name()} ({self.num_axes} ejes, {self.num_buttons} botones)",
            foreground=COLOR_TEXT,
        )
        if self.ser:
            self.arm_btn.config(state="normal")

    def _eje(self, indice):
        if self.joystick and indice is not None and 0 <= indice < self.num_axes:
            return self.joystick.get_axis(indice)
        return 0.0

    def _eje_canal(self, nombre):
        return self._eje(self.axis_map.get(nombre))

    def _mapear_us(self, valor_crudo):
        return mapear_us(valor_crudo, self.invert_throttle)

    # ---------------------------------------------------- Calibracion ----
    def _cargar_calibracion(self):
        self.axis_map = {"roll": AXIS_ROLL, "pitch": AXIS_PITCH, "throttle": AXIS_THROTTLE, "yaw": AXIS_YAW}
        self.invert_throttle = INVERT_THROTTLE
        self.arm_button = ARM_BUTTON
        if os.path.exists(CALIB_FILE):
            try:
                with open(CALIB_FILE, "r", encoding="utf-8") as f:
                    datos = json.load(f)
                mapa = datos.get("axis_map", {})
                for canal in self.axis_map:
                    if canal in mapa:
                        self.axis_map[canal] = int(mapa[canal])
                self.invert_throttle = bool(datos.get("invert_throttle", self.invert_throttle))
                if "arm_button" in datos and datos["arm_button"] is not None:
                    self.arm_button = int(datos["arm_button"])
            except Exception:
                pass  # archivo corrupto o invalido -> usar valores por defecto

    def _guardar_calibracion(self):
        datos = {
            "axis_map": self.axis_map,
            "invert_throttle": self.invert_throttle,
            "arm_button": self.arm_button,
        }
        try:
            with open(CALIB_FILE, "w", encoding="utf-8") as f:
                json.dump(datos, f, indent=2)
        except Exception as exc:
            self._log(f"[error al guardar calibracion] {exc}")

    def _abrir_calibracion(self):
        if self._calib_win is not None and self._calib_win.winfo_exists():
            self._calib_win.lift()
            return

        win = tk.Toplevel(self.root)
        self._calib_win = win
        win.title("Calibracion de ejes")
        win.configure(bg=COLOR_BG)
        win.geometry("460x680")
        win.resizable(True, True)
        win.transient(self.root)

        ttk.Label(
            win, text="Mueve los sticks para identificar cada eje fisico.\n"
                      "Luego asigna cual es Roll / Pitch / Throttle / Yaw.",
            justify="left",
        ).pack(anchor="w", padx=12, pady=(12, 8))

        bars_frame = ttk.Frame(win)
        bars_frame.pack(fill="x", padx=12)

        bar_widgets = []
        num_axes = self.num_axes if self.joystick else 0
        for i in range(num_axes):
            fila = ttk.Frame(bars_frame)
            fila.pack(fill="x", pady=2)
            ttk.Label(fila, text=f"eje {i}", width=7).pack(side="left")
            canvas = tk.Canvas(fila, width=220, height=14, bg=COLOR_PANEL, highlightthickness=0)
            canvas.pack(side="left", padx=6)
            rect_id = canvas.create_rectangle(110, 1, 110, 13, fill=COLOR_BORDER, outline="")
            val_var = tk.StringVar(value="+0.00")
            ttk.Label(fila, textvariable=val_var, width=6).pack(side="left")
            bar_widgets.append((canvas, rect_id, val_var))

        if num_axes == 0:
            ttk.Label(bars_frame, text="No hay joystick detectado.", foreground=COLOR_DANGER).pack(anchor="w", pady=4)

        ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=10)

        map_frame = ttk.Frame(win)
        map_frame.pack(fill="x", padx=12)

        opciones = [str(i) for i in range(num_axes)] or ["0"]
        vars_canal = {}
        for fila_idx, canal in enumerate(["roll", "pitch", "throttle", "yaw"]):
            ttk.Label(map_frame, text=canal.capitalize() + " = eje").grid(row=fila_idx, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(self.axis_map.get(canal, 0)))
            ttk.Combobox(map_frame, textvariable=var, values=opciones, width=6, state="readonly").grid(
                row=fila_idx, column=1, padx=8
            )
            vars_canal[canal] = var

        invert_var = tk.BooleanVar(value=self.invert_throttle)
        ttk.Checkbutton(win, text="Invertir throttle", variable=invert_var).pack(anchor="w", padx=12, pady=(10, 4))

        ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=10)

        # --- Botones: identificar cual usar para armar/desarmar ---
        ttk.Label(
            win, text="Apreta cada boton del control para identificarlo\n"
                      "(se ilumina abajo). Despues elegi cual arma/desarma.",
            justify="left",
        ).pack(anchor="w", padx=12)

        botones_frame = ttk.Frame(win)
        botones_frame.pack(fill="x", padx=12, pady=(8, 4))

        num_botones = self.num_buttons if self.joystick else 0
        boton_widgets = []
        POR_FILA = 8
        for i in range(num_botones):
            fila_idx, col_idx = divmod(i, POR_FILA)
            fila = botones_frame.grid_slaves(row=fila_idx)
            if not fila:
                fc = ttk.Frame(botones_frame)
                fc.grid(row=fila_idx, column=0, sticky="w")
            else:
                fc = fila[0]
            celda = tk.Frame(fc, width=26, height=26, bg=COLOR_CONSOLE_BG,
                              highlightthickness=1, highlightbackground=COLOR_BORDER)
            celda.grid(row=0, column=col_idx, padx=2, pady=2)
            celda.grid_propagate(False)
            lbl = tk.Label(celda, text=str(i), bg=COLOR_CONSOLE_BG, fg=COLOR_MUTED, font=("Consolas", 8))
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            boton_widgets.append((celda, lbl))

        if num_botones == 0:
            ttk.Label(botones_frame, text="No hay joystick detectado.", foreground=COLOR_DANGER).pack(anchor="w")

        arm_frame = ttk.Frame(win)
        arm_frame.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(arm_frame, text="Boton de armado/desarmado:").pack(side="left")
        opciones_botones = [str(i) for i in range(num_botones)] or ["0"]
        arm_var = tk.StringVar(value=str(self.arm_button if self.arm_button is not None else 0))
        ttk.Combobox(arm_frame, textvariable=arm_var, values=opciones_botones, width=6,
                     state="readonly").pack(side="left", padx=8)

        estado_var = tk.StringVar(value="")
        ttk.Label(win, textvariable=estado_var, foreground=COLOR_MUTED).pack(anchor="w", padx=12, pady=(10, 0))

        def guardar():
            try:
                nuevo_map = {canal: int(var.get()) for canal, var in vars_canal.items()}
            except ValueError:
                messagebox.showerror("Error", "Selecciona un eje valido para cada canal.")
                return
            try:
                nuevo_arm_button = int(arm_var.get())
            except ValueError:
                messagebox.showerror("Error", "Selecciona un boton valido para armar/desarmar.")
                return
            self.axis_map = nuevo_map
            self.invert_throttle = bool(invert_var.get())
            self.arm_button = nuevo_arm_button
            self.boton_anterior = False  # evita un toggle falso al cambiar de boton
            self.arm_button_hint_var.set(f"Boton {self.arm_button} del control\ntambien arma/desarma")
            self._guardar_calibracion()
            self._log(
                f"Calibracion guardada: {self.axis_map} (invertir throttle={self.invert_throttle}, "
                f"boton armado={self.arm_button})"
            )
            estado_var.set("Guardado.")

        def restablecer():
            for canal, var in vars_canal.items():
                var.set(str({"roll": AXIS_ROLL, "pitch": AXIS_PITCH,
                              "throttle": AXIS_THROTTLE, "yaw": AXIS_YAW}[canal]))
            invert_var.set(INVERT_THROTTLE)
            arm_var.set(str(ARM_BUTTON))
            estado_var.set("Valores por defecto cargados (todavia sin guardar).")

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=14)
        ttk.Button(btns, text="Guardar", command=guardar).pack(side="left")
        ttk.Button(btns, text="Restablecer por defecto", command=restablecer).pack(side="left", padx=8)
        ttk.Button(btns, text="Cerrar", command=win.destroy).pack(side="left", padx=8)

        def refrescar():
            if not win.winfo_exists():
                return
            if self.joystick:
                pygame.event.pump()
                for i, (canvas, rect_id, val_var) in enumerate(bar_widgets):
                    valor = self._eje(i)
                    val_var.set(f"{valor:+.2f}")
                    x = 110 + valor * 105
                    canvas.coords(rect_id, 110, 1, x, 13)
                for i, (celda, lbl) in enumerate(boton_widgets):
                    presionado = self.joystick.get_button(i)
                    color = COLOR_BORDER if presionado else COLOR_CONSOLE_BG
                    fg = COLOR_BG if presionado else COLOR_MUTED
                    celda.configure(bg=color)
                    lbl.configure(bg=color, fg=fg)
            win.after(50, refrescar)

        refrescar()

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
            self.ser = serial.Serial(port, BAUDRATE, timeout=0.2)
        except Exception as exc:
            messagebox.showerror("Error de conexion", str(exc))
            return

        self.status_label.config(text=f"Conectado ({port})", foreground=COLOR_TEXT)
        self.connect_btn.config(text="Desconectar")
        if self.joystick:
            self.arm_btn.config(state="normal")

        self.ultimo_dato_recibido = None
        self._watchdog_alerta_enviada = False

        self.stop_thread = False
        self.read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self.read_thread.start()

        self._log("Enviando confirmacion inicial para completar calibracion del ESC...")
        self.ser.write(b"off\n")

    def _disconnect(self):
        if self.armado:
            self._toggle_arm()
        self.stop_thread = True
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.status_label.config(text="Desconectado", foreground=COLOR_DANGER)
        self.connect_btn.config(text="Conectar")
        self.arm_btn.config(state="disabled")
        self.ultimo_dato_recibido = None

    def _read_loop(self):
        while not self.stop_thread and self.ser:
            try:
                data = self.ser.readline()
            except Exception:
                break
            if data:
                self.ultimo_dato_recibido = time.time()
                text = data.decode(errors="replace").rstrip()
                self._log(text)
            time.sleep(0.02)

    # ------------------------------------------------------ Armado ----
    def _toggle_arm(self):
        if not self.ser or not self.ser.is_open:
            return
        self.armado = not self.armado
        if self.armado:
            self.arm_status_label.config(text="ARMADO", foreground=COLOR_TEXT)
            self.arm_btn.config(text="DESARMAR")
            self._log(">> Motor ARMADO")
            self._send_line("on")
            # Mantiene "on" quieto ARM_HOLD_S antes de mandar el primer
            # numero, para que el emisor alcance a retransmitirlo varias
            # veces por LoRa (ver ARM_HOLD_S mas arriba).
            self.arm_hold_hasta = time.time() + ARM_HOLD_S
        else:
            self.arm_status_label.config(text="DESARMADO", foreground=COLOR_DANGER)
            self.arm_btn.config(text="ARMAR")
            self._log(">> Motor DESARMADO")
            self._send_line("off")
            self.ultimo_us_enviado = None
            self.us_value_label.config(text="-- us")

    def _emergency_stop(self, event=None):
        """Corta el throttle de inmediato (boton o barra espaciadora)."""
        self._log(">> PARADA DE EMERGENCIA")
        if self.armado:
            self.armado = False
            self.arm_status_label.config(text="DESARMADO", foreground=COLOR_DANGER)
            self.arm_btn.config(text="ARMAR")
        self._send_line("off")
        self.ultimo_us_enviado = None
        self.us_value_label.config(text="-- us")

    def _send_line(self, text):
        if not self.ser or not self.ser.is_open:
            return
        try:
            # CRLF: igual que lo que manda una terminal real al apretar Enter
            # (mas compatible que solo "\n" con parsers tipo fgets/readline).
            self.ser.write((text + "\r\n").encode())
            self.ser.flush()
        except Exception as exc:
            self._log(f"[error al enviar] {exc}")

    # -------------------------------------------------------- Poll ----
    def _poll_loop(self):
        if self.joystick:
            pygame.event.pump()

            self.stick_izq.actualizar(self._eje_canal("yaw"), self._eje_canal("throttle"))
            self.stick_der.actualizar(self._eje_canal("roll"), self._eje_canal("pitch"))

            if self.arm_button is not None and self.arm_button < self.num_buttons:
                boton_actual = self.joystick.get_button(self.arm_button)
                if boton_actual and not self.boton_anterior:
                    self._toggle_arm()
                self.boton_anterior = boton_actual

            ahora = time.time()

            # Sub-pantallas de ejes + osciloscopio: en vivo, independiente de
            # si esta armado/conectado (tambien sirve para calibrar).
            if ahora - self.ultimo_envio_ejes >= SEND_INTERVAL:
                self.ultimo_envio_ejes = ahora
                thr_raw = self._eje_canal("throttle")
                us_preview = self._mapear_us(thr_raw)

                txt_thr = f"{thr_raw:+.2f}  ->  {us_preview} us"
                self.axis_console_throttle.push(txt_thr)
                self._registrar("throttle", txt_thr)

                txt_pitch = f"{self._eje_canal('pitch'):+.2f}"
                self.axis_console_pitch.push(txt_pitch)
                self._registrar("pitch", txt_pitch)

                txt_roll = f"{self._eje_canal('roll'):+.2f}"
                self.axis_console_roll.push(txt_roll)
                self._registrar("roll", txt_roll)

                txt_yaw = f"{self._eje_canal('yaw'):+.2f}"
                self.axis_console_yaw.push(txt_yaw)
                self._registrar("yaw", txt_yaw)

                self.osc.push(us_preview)

            if self.armado and ahora >= self.arm_hold_hasta:
                valor_crudo = self._eje_canal("throttle")
                us = self._mapear_us(valor_crudo)
                debe_enviar = (ahora - self.ultimo_envio) >= SEND_INTERVAL
                cambio_suficiente = (
                    self.ultimo_us_enviado is None or abs(us - self.ultimo_us_enviado) >= DEADZONE_US
                )
                if debe_enviar and cambio_suficiente:
                    self._send_line(str(us))
                    self.ultimo_us_enviado = us
                    self.ultimo_envio = ahora
                    self.us_value_label.config(text=f"{us} us")

        self._actualizar_watchdog()
        self.root.after(20, self._poll_loop)

    def _actualizar_watchdog(self):
        if not (self.ser and self.ser.is_open):
            self.watchdog_label.config(text="Watchdog: —", foreground=COLOR_MUTED)
            self._watchdog_alerta_enviada = False
            self.signal_meter.actualizar(None, "Sin conexion")
            return
        if self.ultimo_dato_recibido is None:
            self.watchdog_label.config(text="Watchdog: esperando datos...", foreground=COLOR_MUTED)
            self.signal_meter.actualizar(None, "Esperando datos...")
            return
        elapsed = time.time() - self.ultimo_dato_recibido
        if elapsed <= WATCHDOG_TIMEOUT_S:
            self.watchdog_label.config(text=f"Watchdog: OK ({elapsed:.1f}s)", foreground=COLOR_TEXT)
            self._watchdog_alerta_enviada = False
            pct = max(5.0, 100.0 * (1 - elapsed / WATCHDOG_TIMEOUT_S))
            self.signal_meter.actualizar(pct, f"Ultimo dato hace {elapsed:.1f}s")
        else:
            self.watchdog_label.config(text=f"Watchdog: SIN RESPUESTA ({elapsed:.1f}s)", foreground=COLOR_DANGER)
            if not self._watchdog_alerta_enviada:
                self._log("[watchdog] El ESP32 dejo de responder.")
                self._watchdog_alerta_enviada = True
            self.signal_meter.actualizar(0, f"Sin respuesta hace {elapsed:.1f}s", peligro=True)

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
        self._registrar("sistema", texto)

    def _exportar_log(self):
        if not self.registro:
            messagebox.showinfo("Exportar log", "Todavia no hay nada para exportar.")
            return
        nombre_sugerido = f"log_dron_{time.strftime('%Y%m%d_%H%M%S')}.csv"
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
                    writer.writerow(["timestamp", "canal", "valor"])
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
        pygame.quit()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = JoystickEscApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
