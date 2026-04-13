#!/usr/bin/env python3
"""
VagaSystem — Central de Controle
TUI baseada em curses + MQTT (paho).

- Thread principal: desenha a tela e lê o teclado.
- Thread MQTT (paho): recebe mensagens do broker e chama os callbacks.
"""

import curses
import json
import threading
import time
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt

# ── Configurações MQTT ─────────────────────────────────────────────────────────

BROKER_HOST = "micros-ctba.microsdns.com.br"
BROKER_PORT = 1883
CLIENT_ID   = "pc_central_tui_" + uuid.uuid4().hex[:8]

TOPIC_SUB      = "vagasystem/+/status"      # ouve todas as vagas
TOPIC_CMD_VAGA = "vagasystem/{}/comandos"   # comando para uma vaga
TOPIC_CMD_ALL  = "vagasystem/all/comandos"  # comando para todas as vagas

# ── Estado global ──────────────────────────────────────────────────────────────

state_lock   = threading.Lock()   # evita leitura e escrita simultânea
needs_redraw = threading.Event()  # sinaliza que a tela precisa ser redesenhada

vagas: dict[str, dict] = {}  # { "01": { id_vaga, ocupado, distancia_cm, … } }
log_messages: list[str] = []
MAX_LOG = 100

mqtt_connected = False


def _add_log(msg: str) -> None:
    """Adiciona uma linha ao log e pede redesenho da tela."""
    ts = datetime.now().strftime("%H:%M:%S")
    with state_lock:
        log_messages.append(f"[{ts}] {msg}")
        if len(log_messages) > MAX_LOG:
            log_messages.pop(0)
    needs_redraw.set()


# ── Callbacks MQTT ─────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc, properties=None):
    """Chamado quando o cliente conecta ao broker."""
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        client.subscribe(TOPIC_SUB)
        _publish(client, TOPIC_CMD_ALL, "status")
        _add_log("Conectado ao broker. Broadcast 'status' enviado a todas as vagas.")
    else:
        _add_log(f"Falha na conexão MQTT (rc={rc}).")
    needs_redraw.set()


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    """Chamado quando a conexão cai."""
    global mqtt_connected
    mqtt_connected = False
    _add_log(f"MQTT desconectado (rc={reason_code}). Aguardando reconexão automática...")
    needs_redraw.set()


def on_message(client, userdata, msg):
    """Chamado a cada mensagem recebida. Atualiza o dicionário de vagas."""
    try:
        payload = json.loads(msg.payload.decode("utf-8"))

        id_vaga = str(payload.get("id_vaga", "")).strip()
        if not id_vaga:
            _add_log(f"Payload sem 'id_vaga' em {msg.topic}. Ignorado.")
            return

        with state_lock:
            vagas[id_vaga] = {
                "id_vaga":      id_vaga,
                "ocupado":      bool(payload.get("ocupado", False)),
                "distancia_cm": float(payload.get("distancia_cm", 0.0)),
                "luz_ambiente": int(payload.get("luz_ambiente", 0)),
                "brilho_led":   int(payload.get("brilho_led", 0)),
                "last_update":  datetime.now().strftime("%H:%M:%S"),
            }

        estado = "Ocupada" if payload.get("ocupado") else "Livre"
        _add_log(f"Vaga {id_vaga}: {estado}  |  dist={payload.get('distancia_cm', '?')} cm")

    except json.JSONDecodeError as e:
        _add_log(f"JSON inválido ({msg.topic}): {e}")
    except (TypeError, ValueError) as e:
        _add_log(f"Payload malformado ({msg.topic}): {e}")
    except Exception as e:
        _add_log(f"Erro inesperado on_message: {e}")


# ── Helpers MQTT ───────────────────────────────────────────────────────────────

def _publish(client: mqtt.Client, topic: str, command: str) -> None:
    """Publica um comando no tópico indicado."""
    payload = json.dumps({"comando": command})
    client.publish(topic, payload)


def publish_command(client: mqtt.Client, topic: str, command: str) -> None:
    """Publica um comando e registra no log."""
    _publish(client, topic, command)
    _add_log(f"CMD '{command}' → {topic}")


def start_mqtt() -> mqtt.Client:
    """Cria o cliente MQTT e inicia a thread de rede em background."""
    client = mqtt.Client(
        client_id=CLIENT_ID,
        protocol=mqtt.MQTTv5,
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    client.connect_async(BROKER_HOST, BROKER_PORT, keepalive=60)
    client.loop_start()
    return client


# ── Constantes de cor ──────────────────────────────────────────────────────────

CP_HEADER   = 1
CP_OCCUPIED = 2
CP_FREE     = 3
CP_SELECTED = 4
CP_OK       = 5
CP_ERR      = 6
CP_LOG      = 7
CP_BUTTON   = 8
CP_TITLE    = 9
CP_DIM      = 10


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(CP_HEADER,   curses.COLOR_BLACK,  curses.COLOR_CYAN)
    curses.init_pair(CP_OCCUPIED, curses.COLOR_WHITE,  curses.COLOR_RED)
    curses.init_pair(CP_FREE,     curses.COLOR_BLACK,  curses.COLOR_GREEN)
    curses.init_pair(CP_SELECTED, curses.COLOR_BLACK,  curses.COLOR_YELLOW)
    curses.init_pair(CP_OK,       curses.COLOR_GREEN,  -1)
    curses.init_pair(CP_ERR,      curses.COLOR_RED,    -1)
    curses.init_pair(CP_LOG,      curses.COLOR_CYAN,   -1)
    curses.init_pair(CP_BUTTON,   curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(CP_TITLE,    curses.COLOR_YELLOW, -1)
    curses.init_pair(CP_DIM,      curses.COLOR_WHITE,  -1)


# ── Utilitários de desenho ─────────────────────────────────────────────────────

def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Escreve texto na tela sem estourar os limites da janela."""
    try:
        h, w = win.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            win.addstr(y, x, text[: w - x], attr)
    except curses.error:
        pass


def _draw_hline(win, y: int, char: str = "─") -> None:
    """Desenha uma linha horizontal."""
    _, w = win.getmaxyx()
    _safe_addstr(win, y, 0, char * w)


def _draw_header(win, title: str) -> None:
    """Desenha o cabeçalho com título e status da conexão MQTT."""
    h, w = win.getmaxyx()
    conn_str = "● CONECTADO " if mqtt_connected else "○ DESCONECTADO "
    conn_col = curses.color_pair(CP_OK) | curses.A_BOLD if mqtt_connected \
               else curses.color_pair(CP_ERR) | curses.A_BOLD

    win.attron(curses.color_pair(CP_HEADER) | curses.A_BOLD)
    try:
        win.addstr(0, 0, " " * w)
        win.addstr(0, max(0, (w - len(title)) // 2), title[:w])
    except curses.error:
        pass
    win.attroff(curses.color_pair(CP_HEADER) | curses.A_BOLD)

    x_conn = w - len(conn_str) - 1
    if x_conn > 0:
        win.attron(conn_col)
        try:
            win.addstr(0, x_conn, conn_str[: w - x_conn])
        except curses.error:
            pass
        win.attroff(conn_col)


def _draw_footer(win, text: str) -> None:
    """Desenha o rodapé com os atalhos de teclado."""
    h, w = win.getmaxyx()
    padded = text.center(w)
    win.attron(curses.color_pair(CP_HEADER))
    try:
        win.addstr(h - 1, 0, padded[:w])
    except curses.error:
        pass
    win.attroff(curses.color_pair(CP_HEADER))


def _draw_log_section(win, start_row: int, n_lines: int = 4) -> None:
    """Desenha as últimas N linhas do log de eventos."""
    win.attron(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    _safe_addstr(win, start_row, 2, "LOG DE EVENTOS")
    win.attroff(curses.color_pair(CP_TITLE) | curses.A_BOLD)

    with state_lock:
        recent = log_messages[-n_lines:]

    for i, line in enumerate(recent):
        win.attron(curses.color_pair(CP_LOG))
        _safe_addstr(win, start_row + 1 + i, 4, line)
        win.attroff(curses.color_pair(CP_LOG))


def _draw_button(win, y: int, x: int, label: str) -> int:
    """Desenha um botão e retorna a posição x após ele."""
    btn = f"  {label}  "
    win.attron(curses.color_pair(CP_BUTTON) | curses.A_BOLD)
    _safe_addstr(win, y, x, btn)
    win.attroff(curses.color_pair(CP_BUTTON) | curses.A_BOLD)
    return x + len(btn) + 2


# ── Tela 1 — Dashboard ────────────────────────────────────────────────────────

def draw_dashboard(stdscr, selected_idx: int) -> None:
    """Desenha a lista de todas as vagas do estacionamento."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    _draw_header(stdscr, "  VagaSystem — Central de Controle  ")

    stdscr.attron(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    _safe_addstr(stdscr, 2, 2, "VAGAS DO ESTACIONAMENTO")
    stdscr.attroff(curses.color_pair(CP_TITLE) | curses.A_BOLD)

    hdr = f"{'ID DA VAGA':<10}  {'ESTADO':<14}  {'ATUALIZADO':<10}"
    stdscr.attron(curses.A_BOLD | curses.A_UNDERLINE)
    _safe_addstr(stdscr, 3, 2, hdr)
    stdscr.attroff(curses.A_BOLD | curses.A_UNDERLINE)

    max_rows = h - 14
    row = 4

    with state_lock:
        vaga_list = sorted(vagas.values(), key=lambda v: v["id_vaga"])

    if not vaga_list:
        stdscr.attron(curses.color_pair(CP_DIM))
        _safe_addstr(stdscr, row, 4, "Aguardando dados das vagas...")
        stdscr.attroff(curses.color_pair(CP_DIM))
        row += 1
    else:
        for idx, vaga in enumerate(vaga_list[:max_rows]):
            ocupado    = vaga["ocupado"]
            estado_str = "  OCUPADA" if ocupado else "  LIVRE  "
            linha = (
                f" {vaga['id_vaga']:<10}"
                f"  {estado_str:<14}"
                f"  {vaga['last_update']:<10}"
            )

            if idx == selected_idx:
                attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
            elif ocupado:
                attr = curses.color_pair(CP_OCCUPIED)
            else:
                attr = curses.color_pair(CP_FREE)

            stdscr.attron(attr)
            try:
                stdscr.addstr(row, 0, linha.ljust(w - 1)[: w - 1])
            except curses.error:
                pass
            stdscr.attroff(attr)
            row += 1

    sep_row = h - 10
    _draw_hline(stdscr, sep_row)

    maint_row = sep_row + 1
    stdscr.attron(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    _safe_addstr(stdscr, maint_row, 2, "MODO MANUTENÇÃO GLOBAL  (envia para todas as vagas)")
    stdscr.attroff(curses.color_pair(CP_TITLE) | curses.A_BOLD)

    btn_row = maint_row + 1
    x = 2
    x = _draw_button(stdscr, btn_row, x, "[L] Luz ON")
    x = _draw_button(stdscr, btn_row, x, "[X] Luz OFF")
    x = _draw_button(stdscr, btn_row, x, "[A] Modo Auto")

    _draw_log_section(stdscr, btn_row + 2, n_lines=4)

    _draw_footer(
        stdscr,
        " [↑↓] Navegar  [Enter] Ver Detalhes  [L] Luz ON  [X] Luz OFF  [A] Auto  [Q] Sair ",
    )

    stdscr.refresh()


# ── Tela 2 — Detalhes de uma vaga ─────────────────────────────────────────────

def draw_detail(stdscr, id_vaga: str) -> None:
    """Desenha os dados completos de uma vaga específica."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    _draw_header(stdscr, f"  VagaSystem — Detalhes: Vaga {id_vaga}  ")

    with state_lock:
        vaga = vagas.get(id_vaga)

    row = 2

    stdscr.attron(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    _safe_addstr(stdscr, row, 2, f"INFORMAÇÕES DA VAGA {id_vaga}")
    stdscr.attroff(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    row += 2

    if vaga is None:
        stdscr.attron(curses.color_pair(CP_DIM))
        _safe_addstr(stdscr, row, 4, f"Aguardando resposta da vaga {id_vaga}...")
        stdscr.attroff(curses.color_pair(CP_DIM))
        row += 1
    else:
        ocupado    = vaga["ocupado"]
        badge      = "  ●  VAGA OCUPADA  " if ocupado else "  ○  VAGA LIVRE   "
        badge_attr = curses.color_pair(CP_OCCUPIED) | curses.A_BOLD if ocupado \
                     else curses.color_pair(CP_FREE) | curses.A_BOLD
        stdscr.attron(badge_attr)
        _safe_addstr(stdscr, row, 4, badge)
        stdscr.attroff(badge_attr)
        row += 2

        barra_ldr = _bar(vaga["luz_ambiente"], 4095, 20)
        barra_pwm = _bar(vaga["brilho_led"],   1023, 20)

        fields = [
            ("Distância (ultrassônico)",  f"{vaga['distancia_cm']:.1f} cm"),
            ("Nível LDR (luz ambiente) ", f"{vaga['luz_ambiente']:>4} / 4095  {barra_ldr}"),
            ("Brilho LED (PWM)         ", f"{vaga['brilho_led']:>4} / 1023  {barra_pwm}"),
            ("Última atualização       ", vaga["last_update"]),
        ]

        label_w = 26
        for label, value in fields:
            stdscr.attron(curses.A_BOLD)
            _safe_addstr(stdscr, row, 4, f"{label}:")
            stdscr.attroff(curses.A_BOLD)
            stdscr.attron(curses.color_pair(CP_DIM))
            _safe_addstr(stdscr, row, 4 + label_w + 2, value)
            stdscr.attroff(curses.color_pair(CP_DIM))
            row += 1

    sep_row = h - 9
    _draw_hline(stdscr, sep_row)

    ctrl_row = sep_row + 1
    stdscr.attron(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    _safe_addstr(stdscr, ctrl_row, 2, f"CONTROLE DA VAGA {id_vaga}")
    stdscr.attroff(curses.color_pair(CP_TITLE) | curses.A_BOLD)

    btn_row = ctrl_row + 1
    x = 2
    x = _draw_button(stdscr, btn_row, x, "[L] Forçar Luz ON")
    x = _draw_button(stdscr, btn_row, x, "[X] Forçar Luz OFF")
    x = _draw_button(stdscr, btn_row, x, "[A] Modo Automático")
    x = _draw_button(stdscr, btn_row, x, "[B] Voltar")

    _draw_log_section(stdscr, btn_row + 2, n_lines=3)

    _draw_footer(
        stdscr,
        " [L] Luz ON  [X] Luz OFF  [A] Auto  [B / Esc] Voltar ao Dashboard  [Q] Sair ",
    )

    stdscr.refresh()


def _bar(value: int, max_value: int, width: int = 20) -> str:
    """Barra de progresso ASCII. Ex: [████████░░░░░░░░░░░░]"""
    filled = int((value / max_value) * width) if max_value > 0 else 0
    filled = max(0, min(filled, width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# ── Loop principal ─────────────────────────────────────────────────────────────

def main(stdscr) -> None:
    """Loop da TUI: redesenha a tela e responde ao teclado."""
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)
    _init_colors()

    client = start_mqtt()
    _add_log(f"Iniciando... Client ID: {CLIENT_ID}")
    _add_log(f"Broker: {BROKER_HOST}:{BROKER_PORT}")

    screen       = "dashboard"  # "dashboard" ou "detail"
    selected_idx = 0
    detail_id    = None

    last_draw   = 0.0
    REFRESH_SEC = 1.0  # redesenho mínimo a cada 1 segundo

    try:
        while True:
            now = time.monotonic()

            # Redesenha quando há novidade MQTT ou passou REFRESH_SEC
            if needs_redraw.is_set() or (now - last_draw) >= REFRESH_SEC:
                needs_redraw.clear()
                last_draw = now

                with state_lock:
                    n = len(vagas)
                if n > 0:
                    selected_idx = min(selected_idx, n - 1)

                if screen == "dashboard":
                    draw_dashboard(stdscr, selected_idx)
                else:
                    draw_detail(stdscr, detail_id)

            key = stdscr.getch()

            if key == curses.ERR:
                time.sleep(0.04)
                continue

            if key in (ord("q"), ord("Q")):
                break

            if screen == "dashboard":
                with state_lock:
                    vaga_list = sorted(vagas.values(), key=lambda v: v["id_vaga"])
                n = len(vaga_list)

                if key == curses.KEY_UP:
                    selected_idx = max(0, selected_idx - 1)
                    needs_redraw.set()

                elif key == curses.KEY_DOWN:
                    selected_idx = min(max(0, n - 1), selected_idx + 1)
                    needs_redraw.set()

                elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")):
                    if n > 0:
                        detail_id = vaga_list[selected_idx]["id_vaga"]
                        screen = "detail"
                        publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "status")
                        needs_redraw.set()

                elif key in (ord("l"), ord("L")):
                    publish_command(client, TOPIC_CMD_ALL, "luz_on")

                elif key in (ord("x"), ord("X")):
                    publish_command(client, TOPIC_CMD_ALL, "luz_off")

                elif key in (ord("a"), ord("A")):
                    publish_command(client, TOPIC_CMD_ALL, "auto")

            elif screen == "detail":
                if key in (ord("b"), ord("B"), 27):  # 27 = Esc
                    screen = "dashboard"
                    needs_redraw.set()

                elif key in (ord("l"), ord("L")):
                    publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "luz_on")

                elif key in (ord("x"), ord("X")):
                    publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "luz_off")

                elif key in (ord("a"), ord("A")):
                    publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "auto")

    finally:
        client.loop_stop()
        client.disconnect()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    curses.wrapper(main)
