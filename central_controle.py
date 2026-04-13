#!/usr/bin/env python3
"""
VagaSystem — Central de Controle
═══════════════════════════════════════════════════════════════════════════════
TUI baseada em curses  +  MQTT assíncrono via paho (loop_start).

Arquitetura de threads:
  • Thread principal  → curses: desenha tela, processa teclado.
  • Thread MQTT       → paho loop_start(): escuta broker, dispara callbacks.

Os callbacks do MQTT (on_connect, on_message) rodam na thread do paho.
Eles atualizam o dicionário 'vagas' e a lista 'log_messages' sob o
'state_lock' (threading.Lock) para evitar race conditions, e sinalizam
'needs_redraw' (threading.Event) para que a thread principal redesenhe
a tela na próxima iteração do seu loop principal, sem bloquear.
"""

import curses
import json
import threading
import time
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURAÇÕES MQTT
# ══════════════════════════════════════════════════════════════════════════════

BROKER_HOST = "micros-ctba.microsdns.com.br"
BROKER_PORT = 1883
# Client ID único por sessão para evitar conflito de sessão no broker
CLIENT_ID = "pc_central_tui_" + uuid.uuid4().hex[:8]

TOPIC_SUB      = "vagasystem/+/status"           # wildcard: escuta todas as vagas
TOPIC_CMD_VAGA = "vagasystem/{}/comandos"         # comando para vaga específica
TOPIC_CMD_ALL  = "vagasystem/all/comandos"        # broadcast para todas as vagas

# ══════════════════════════════════════════════════════════════════════════════
#  ESTADO GLOBAL COMPARTILHADO (lido/escrito por ambas as threads)
# ══════════════════════════════════════════════════════════════════════════════

state_lock    = threading.Lock()     # protege 'vagas' e 'log_messages'
needs_redraw  = threading.Event()    # sinaliza: thread MQTT → thread curses

# Dicionário principal: { "01": { id_vaga, ocupado, distancia_cm, … }, … }
vagas: dict[str, dict] = {}

# Log circular de eventos para exibição na TUI
log_messages: list[str] = []
MAX_LOG = 100

# Estado da conexão MQTT (atualizado pelo callback on_connect/on_disconnect)
mqtt_connected = False


def _add_log(msg: str) -> None:
    """
    Registra uma linha no log e sinaliza redesenho.
    Chamado tanto pela thread MQTT quanto pela thread principal.
    Usa state_lock para acesso seguro à lista log_messages.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    with state_lock:
        log_messages.append(f"[{ts}] {msg}")
        if len(log_messages) > MAX_LOG:
            log_messages.pop(0)
    needs_redraw.set()


# ══════════════════════════════════════════════════════════════════════════════
#  CALLBACKS MQTT  (executados na thread interna do paho, não na thread curses)
# ══════════════════════════════════════════════════════════════════════════════

def on_connect(client, userdata, flags, rc, properties=None):
    """
    Disparado pela thread paho quando a conexão ao broker é estabelecida.
    Após conectar: subscribe no wildcard e faz broadcast de 'status' para
    forçar todas as vagas já ligadas a se apresentarem imediatamente.
    """
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        client.subscribe(TOPIC_SUB)
        # Boot do sistema: solicita estado atual de todas as vagas
        _publish(client, TOPIC_CMD_ALL, "status")
        _add_log("Conectado ao broker. Broadcast 'status' enviado a todas as vagas.")
    else:
        _add_log(f"Falha na conexão MQTT (rc={rc}).")
    needs_redraw.set()


def on_disconnect(client, userdata, rc, properties=None):
    """Disparado quando a conexão cai. Atualiza flag e agenda redesenho."""
    global mqtt_connected
    mqtt_connected = False
    _add_log(f"MQTT desconectado (rc={rc}). Aguardando reconexão automática...")
    needs_redraw.set()


def on_message(client, userdata, msg):
    """
    Disparado pela thread paho a cada mensagem recebida em 'vagasystem/+/status'.

    Fluxo:
      1. Decodifica e valida o JSON recebido.
      2. Atualiza o dict 'vagas' sob state_lock (seguro entre threads).
      3. Chama needs_redraw.set() → thread curses redesenha na próxima iteração.

    Tratamento de erros: JSON mal-formado ou payload incompleto são logados
    e descartados silenciosamente — a TUI jamais trava por causa disso.
    """
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


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS MQTT
# ══════════════════════════════════════════════════════════════════════════════

def _publish(client: mqtt.Client, topic: str, command: str) -> None:
    """Envelopa o comando em JSON e publica. Thread-safe (paho é thread-safe)."""
    payload = json.dumps({"comando": command})
    client.publish(topic, payload)


def publish_command(client: mqtt.Client, topic: str, command: str) -> None:
    """Versão pública: publica e registra no log."""
    _publish(client, topic, command)
    _add_log(f"CMD '{command}' → {topic}")


def start_mqtt() -> mqtt.Client:
    """
    Cria e configura o cliente MQTT.
    loop_start() inicia uma thread daemon separada que gerencia todo o I/O
    de rede (reconexões, keep-alive, callbacks) sem bloquear a thread curses.
    """
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
    client.loop_start()   # ← thread secundária não-bloqueante
    return client


# ══════════════════════════════════════════════════════════════════════════════
#  CONSTANTES DE COR (índices dos pares de cor do curses)
# ══════════════════════════════════════════════════════════════════════════════

CP_HEADER    = 1   # fundo ciano, texto preto  — cabeçalho / rodapé
CP_OCCUPIED  = 2   # fundo vermelho, texto branco
CP_FREE      = 3   # fundo verde, texto preto
CP_SELECTED  = 4   # fundo amarelo, texto preto  — linha selecionada
CP_OK        = 5   # verde   — status MQTT ok
CP_ERR       = 6   # vermelho — status MQTT erro
CP_LOG       = 7   # ciano   — linhas de log
CP_BUTTON    = 8   # fundo branco, texto preto  — botões
CP_TITLE     = 9   # amarelo — subtítulos de seção
CP_DIM       = 10  # cinza   — textos secundários


def _init_colors() -> None:
    curses.start_color()
    curses.use_default_colors()   # -1 = cor padrão do terminal
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


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITÁRIOS DE DESENHO
# ══════════════════════════════════════════════════════════════════════════════

def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr sem estourar os limites da janela (curses.error ignorado)."""
    try:
        h, w = win.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            win.addstr(y, x, text[: w - x], attr)
    except curses.error:
        pass


def _draw_hline(win, y: int, char: str = "─") -> None:
    """Linha horizontal simples."""
    _, w = win.getmaxyx()
    _safe_addstr(win, y, 0, char * w)


def _draw_header(win, title: str) -> None:
    """Faixa de cabeçalho colorida com título centralizado e status MQTT."""
    h, w = win.getmaxyx()
    conn_str = "● CONECTADO " if mqtt_connected else "○ DESCONECTADO "
    conn_col = curses.color_pair(CP_OK) | curses.A_BOLD if mqtt_connected \
               else curses.color_pair(CP_ERR) | curses.A_BOLD

    # Fundo ciano na linha 0
    win.attron(curses.color_pair(CP_HEADER) | curses.A_BOLD)
    try:
        win.addstr(0, 0, " " * w)
        win.addstr(0, max(0, (w - len(title)) // 2), title[: w])
    except curses.error:
        pass
    win.attroff(curses.color_pair(CP_HEADER) | curses.A_BOLD)

    # Status MQTT sobreposto à direita
    x_conn = w - len(conn_str) - 1
    if x_conn > 0:
        win.attron(conn_col)
        try:
            win.addstr(0, x_conn, conn_str[: w - x_conn])
        except curses.error:
            pass
        win.attroff(conn_col)


def _draw_footer(win, text: str) -> None:
    """Faixa de rodapé colorida com atalhos."""
    h, w = win.getmaxyx()
    padded = text.center(w)
    win.attron(curses.color_pair(CP_HEADER))
    try:
        win.addstr(h - 1, 0, padded[: w])
    except curses.error:
        pass
    win.attroff(curses.color_pair(CP_HEADER))


def _draw_log_section(win, start_row: int, n_lines: int = 4) -> None:
    """Desenha as últimas N linhas do log de eventos."""
    h, w = win.getmaxyx()
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


# ══════════════════════════════════════════════════════════════════════════════
#  TELA 1 — DASHBOARD PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def draw_dashboard(stdscr, selected_idx: int) -> None:
    """
    Renderiza a visão geral do estacionamento.

    Estrutura (de cima para baixo):
      Linha 0   : cabeçalho + status MQTT
      Linha 2   : título "VAGAS DO ESTACIONAMENTO"
      Linha 3   : cabeçalho da tabela
      Linhas 4+ : uma linha por vaga (scroll implícito por limite de altura)
      …         : separador horizontal
      …         : seção "Modo Manutenção Global" com botões
      …         : log de eventos
      Última    : rodapé com atalhos
    """
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    _draw_header(stdscr, "  VagaSystem — Central de Controle  ")

    # ── Título da seção ──────────────────────────────────────────────────────
    stdscr.attron(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    _safe_addstr(stdscr, 2, 2, "VAGAS DO ESTACIONAMENTO")
    stdscr.attroff(curses.color_pair(CP_TITLE) | curses.A_BOLD)

    # ── Cabeçalho da tabela ──────────────────────────────────────────────────
    col_id     = 2
    col_estado = 14
    col_update = 32
    hdr = f"{'ID DA VAGA':<10}  {'ESTADO':<14}  {'ATUALIZADO':<10}"
    stdscr.attron(curses.A_BOLD | curses.A_UNDERLINE)
    _safe_addstr(stdscr, 3, col_id, hdr)
    stdscr.attroff(curses.A_BOLD | curses.A_UNDERLINE)

    # ── Linhas de vagas ──────────────────────────────────────────────────────
    # Reserva linhas no final para: separador(1) + título cmd(1) + botões(1)
    # + espaço(1) + título log(1) + log(4) + rodapé(1) = 10 linhas reservadas
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

    # ── Separador ────────────────────────────────────────────────────────────
    sep_row = h - 10
    _draw_hline(stdscr, sep_row)

    # ── Modo Manutenção Global ────────────────────────────────────────────────
    maint_row = sep_row + 1
    stdscr.attron(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    _safe_addstr(stdscr, maint_row, 2, "MODO MANUTENÇÃO GLOBAL  (envia para todas as vagas)")
    stdscr.attroff(curses.color_pair(CP_TITLE) | curses.A_BOLD)

    btn_row = maint_row + 1
    x = 2
    x = _draw_button(stdscr, btn_row, x, "[L] Luz ON")
    x = _draw_button(stdscr, btn_row, x, "[X] Luz OFF")
    x = _draw_button(stdscr, btn_row, x, "[A] Modo Auto")

    # ── Log de eventos ───────────────────────────────────────────────────────
    _draw_log_section(stdscr, btn_row + 2, n_lines=4)

    # ── Rodapé ───────────────────────────────────────────────────────────────
    _draw_footer(
        stdscr,
        " [↑↓] Navegar  [Enter] Ver Detalhes  [L] Luz ON  [X] Luz OFF  [A] Auto  [Q] Sair "
    )

    stdscr.refresh()


# ══════════════════════════════════════════════════════════════════════════════
#  TELA 2 — DETALHES DE UMA VAGA
# ══════════════════════════════════════════════════════════════════════════════

def draw_detail(stdscr, id_vaga: str) -> None:
    """
    Renderiza os dados completos de uma vaga específica.

    Reatividade: como needs_redraw é setado pelo on_message sempre que um
    novo payload chega, esta função é chamada novamente pelo loop principal
    automaticamente, mantendo os dados sempre atuais sem intervenção do usuário.

    Nota: o comando 'status' já foi enviado no momento em que o usuário
    selecionou esta tela (veja o loop principal). Não há botão "Atualizar"
    nem nenhuma menção ao comando 'status' aqui — conforme especificação.
    """
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    _draw_header(stdscr, f"  VagaSystem — Detalhes: Vaga {id_vaga}  ")

    with state_lock:
        vaga = vagas.get(id_vaga)

    row = 2

    # ── Título da seção ──────────────────────────────────────────────────────
    stdscr.attron(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    _safe_addstr(stdscr, row, 2, f"INFORMAÇÕES DA VAGA {id_vaga}")
    stdscr.attroff(curses.color_pair(CP_TITLE) | curses.A_BOLD)
    row += 2

    if vaga is None:
        # Vaga ainda não enviou dados (aguarda resposta do comando 'status')
        stdscr.attron(curses.color_pair(CP_DIM))
        _safe_addstr(stdscr, row, 4, f"Aguardando resposta da vaga {id_vaga}...")
        stdscr.attroff(curses.color_pair(CP_DIM))
        row += 1
    else:
        # ── Badge de ocupação ────────────────────────────────────────────────
        ocupado = vaga["ocupado"]
        badge   = "  ●  VAGA OCUPADA  " if ocupado else "  ○  VAGA LIVRE   "
        badge_attr = curses.color_pair(CP_OCCUPIED) | curses.A_BOLD if ocupado \
                     else curses.color_pair(CP_FREE) | curses.A_BOLD
        stdscr.attron(badge_attr)
        _safe_addstr(stdscr, row, 4, badge)
        stdscr.attroff(badge_attr)
        row += 2

        # ── Tabela de dados do sensor ────────────────────────────────────────
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

    # ── Separador ────────────────────────────────────────────────────────────
    sep_row = h - 9
    _draw_hline(stdscr, sep_row)

    # ── Botões de controle ───────────────────────────────────────────────────
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

    # ── Log de eventos ───────────────────────────────────────────────────────
    _draw_log_section(stdscr, btn_row + 2, n_lines=3)

    # ── Rodapé ───────────────────────────────────────────────────────────────
    _draw_footer(
        stdscr,
        " [L] Luz ON  [X] Luz OFF  [A] Auto  [B / Esc] Voltar ao Dashboard  [Q] Sair "
    )

    stdscr.refresh()


def _bar(value: int, max_value: int, width: int = 20) -> str:
    """Barra de progresso ASCII simples. Ex: [████████░░░░░░░░░░░░]"""
    if max_value == 0:
        filled = 0
    else:
        filled = int((value / max_value) * width)
    filled = max(0, min(filled, width))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# ══════════════════════════════════════════════════════════════════════════════
#  LOOP PRINCIPAL (thread curses / thread principal)
# ══════════════════════════════════════════════════════════════════════════════

def main(stdscr) -> None:
    """
    Ponto de entrada da TUI. Gerencia o ciclo:
      1. Verificar se needs_redraw foi setado (pela thread MQTT ou por input).
      2. Redesenhar a tela correta se necessário.
      3. Ler tecla (não-bloqueante via nodelay).
      4. Processar tecla → mudar estado → setar needs_redraw → volta ao passo 1.

    A conexão MQTT roda em paralelo na thread do paho (loop_start).
    A interface nunca bloqueia esperando rede.
    """
    curses.curs_set(0)      # esconde cursor
    stdscr.nodelay(True)    # getch() não bloqueia — retorna ERR imediatamente
    stdscr.keypad(True)     # decodifica teclas especiais (setas, F-keys, etc.)
    _init_colors()

    # ── Iniciar MQTT em background ───────────────────────────────────────────
    client = start_mqtt()
    _add_log(f"Iniciando... Client ID: {CLIENT_ID}")
    _add_log(f"Broker: {BROKER_HOST}:{BROKER_PORT}")

    # ── Estado da navegação da TUI ───────────────────────────────────────────
    screen       = "dashboard"   # "dashboard" | "detail"
    selected_idx = 0             # índice da vaga selecionada no dashboard
    detail_id    = None          # id_vaga exibida na tela de detalhes

    last_draw    = 0.0           # timestamp do último redesenho (monotonic)
    REFRESH_SEC  = 1.0           # redesenho periódico mínimo (heartbeat visual)

    try:
        while True:
            now = time.monotonic()

            # ── Redesenho ────────────────────────────────────────────────────
            # Redesenha quando: (a) evento MQTT setou needs_redraw, ou
            # (b) passou mais de REFRESH_SEC desde o último desenho (heartbeat).
            if needs_redraw.is_set() or (now - last_draw) >= REFRESH_SEC:
                needs_redraw.clear()
                last_draw = now

                # Garante que selected_idx não aponte para vaga inexistente
                with state_lock:
                    n = len(vagas)
                if n > 0:
                    selected_idx = min(selected_idx, n - 1)

                if screen == "dashboard":
                    draw_dashboard(stdscr, selected_idx)
                else:
                    draw_detail(stdscr, detail_id)

            # ── Leitura de tecla (não-bloqueante) ────────────────────────────
            key = stdscr.getch()

            if key == curses.ERR:
                # Nenhuma tecla pressionada — dorme brevemente para não
                # consumir 100% de CPU no spin loop.
                time.sleep(0.04)   # ~25 fps máximo de resposta a input
                continue

            # ── Tecla global ─────────────────────────────────────────────────
            if key in (ord("q"), ord("Q")):
                break

            # ── Teclas do Dashboard ──────────────────────────────────────────
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
                        # Gatilho obrigatório: solicita estado atualizado da
                        # vaga no exato momento em que o usuário abre a tela.
                        # O comando 'status' é transparente — sem botão na UI.
                        publish_command(
                            client,
                            TOPIC_CMD_VAGA.format(detail_id),
                            "status",
                        )
                        needs_redraw.set()

                elif key in (ord("l"), ord("L")):
                    publish_command(client, TOPIC_CMD_ALL, "luz_on")

                elif key in (ord("x"), ord("X")):
                    publish_command(client, TOPIC_CMD_ALL, "luz_off")

                elif key in (ord("a"), ord("A")):
                    publish_command(client, TOPIC_CMD_ALL, "auto")

            # ── Teclas da Tela de Detalhes ───────────────────────────────────
            elif screen == "detail":
                if key in (ord("b"), ord("B"), 27):   # 27 = Esc
                    screen = "dashboard"
                    needs_redraw.set()

                elif key in (ord("l"), ord("L")):
                    publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "luz_on")

                elif key in (ord("x"), ord("X")):
                    publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "luz_off")

                elif key in (ord("a"), ord("A")):
                    publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "auto")

    finally:
        # Encerra graciosamente a thread MQTT antes de sair
        client.loop_stop()
        client.disconnect()


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    curses.wrapper(main)
