import json
import os
import threading
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt

BROKER_HOST = "micros-ctba.microsdns.com.br"
BROKER_PORT = 1883
CLIENT_ID   = "pc_central_tui_" + uuid.uuid4().hex[:8]

TOPIC_SUB      = "vagasystem/+/status"
TOPIC_CMD_VAGA = "vagasystem/{}/comandos"
TOPIC_CMD_ALL  = "vagasystem/all/comandos"

state_lock = threading.Lock()

vagas: dict[str, dict] = {}
log_messages: list[str] = []
MAX_LOG = 100
mqtt_connected = False


def _add_log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with state_lock:
        log_messages.append(f"[{ts}] {msg}")
        if len(log_messages) > MAX_LOG:
            log_messages.pop(0)


def on_connect(client, userdata, flags, rc, properties=None):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        client.subscribe(TOPIC_SUB)
        client.publish(TOPIC_CMD_ALL, json.dumps({"comando": "status"}))
        _add_log("Conectado ao broker. Solicitando status de todas as vagas...")
    else:
        _add_log(f"Falha na conexão MQTT (rc={rc}).")


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    global mqtt_connected
    mqtt_connected = False
    _add_log(f"MQTT desconectado (rc={reason_code}). Reconectando...")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
        id_vaga = str(payload.get("id_vaga", "")).strip()
        if not id_vaga:
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
        _add_log(f"Vaga {id_vaga}: {estado} | dist={payload.get('distancia_cm', '?')} cm")

    except (json.JSONDecodeError, TypeError, ValueError) as e:
        _add_log(f"Payload inválido ({msg.topic}): {e}")
    except Exception as e:
        _add_log(f"Erro inesperado: {e}")


def publish_command(client: mqtt.Client, topic: str, command: str) -> None:
    client.publish(topic, json.dumps({"comando": command}))
    _add_log(f"CMD '{command}' -> {topic}")


def start_mqtt() -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    client.connect(BROKER_HOST, BROKER_PORT, 60)
    client.loop_start()
    return client


def draw_dashboard() -> None:
    os.system("clear")
    status = "CONECTADO" if mqtt_connected else "DESCONECTADO"
    print(f"VagaSystem — Central de Controle  [{status}]\n")

    with state_lock:
        vaga_list  = sorted(vagas.values(), key=lambda v: v["id_vaga"])
        log_recent = log_messages[-5:]

    if not vaga_list:
        print("  Aguardando dados das vagas...\n")
    else:
        print(f"  {'Número':<4}  {'ID':<8}  {'ESTADO':<10}  ATUALIZADO")
        print(f"  {'─'*4}  {'─'*8}  {'─'*10}  {'─'*8}")
        for i, vaga in enumerate(vaga_list, start=1):
            estado = "OCUPADA" if vaga["ocupado"] else "LIVRE"
            print(f"  {i:<4}  {vaga['id_vaga']:<8}  {estado:<10}  {vaga['last_update']}")

    print("\nLOG (últimas mensagens)")
    for linha in log_recent:
        print(f"  {linha}")

    print("\nComandos: [número] Ver detalhes  [l] Luz ON  [x] Luz OFF  [a] Auto  [q] Sair")


def draw_detail(id_vaga: str) -> None:
    os.system("clear")
    print(f"VagaSystem — Detalhes: Vaga {id_vaga}\n")

    with state_lock:
        vaga       = vagas.get(id_vaga)
        log_recent = log_messages[-5:]

    if vaga is None:
        print(f"  Aguardando dados da vaga {id_vaga}...\n")
    else:
        estado = "OCUPADA" if vaga["ocupado"] else "LIVRE"
        print(f"  Estado:              {estado}")
        print(f"  Distância:           {vaga['distancia_cm']:.1f} cm")
        print(f"  Luz ambiente (LDR):  {vaga['luz_ambiente']} / 4095")
        print(f"  Brilho LED (PWM):    {vaga['brilho_led']} / 1023")
        print(f"  Última atualização:  {vaga['last_update']}")

    print("\nLOG (últimas mensagens)")
    for linha in log_recent:
        print(f"  {linha}")

    print("\nComandos: [l] Luz ON  [x] Luz OFF  [a] Auto  [b] Voltar  [q] Sair")


def main() -> None:
    client = start_mqtt()
    _add_log(f"Iniciando... Broker: {BROKER_HOST}:{BROKER_PORT}")

    screen    = "dashboard"
    detail_id = None

    while True:
        if screen == "dashboard":
            draw_dashboard()
            cmd = input("\n> ").strip().lower()

            if cmd == "q":
                break
            elif cmd == "l":
                publish_command(client, TOPIC_CMD_ALL, "luz_on")
            elif cmd == "x":
                publish_command(client, TOPIC_CMD_ALL, "luz_off")
            elif cmd == "a":
                publish_command(client, TOPIC_CMD_ALL, "auto")
            elif cmd.isdigit():
                with state_lock:
                    vaga_list = sorted(vagas.values(), key=lambda v: v["id_vaga"])
                idx = int(cmd) - 1
                if not vaga_list:
                    print("Nenhuma vaga disponível ainda. Aguarde...")
                    input("Pressione Enter para continuar...")
                elif 0 <= idx < len(vaga_list):
                    detail_id = vaga_list[idx]["id_vaga"]
                    screen    = "detail"
                    publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "status")
                else:
                    print(f"Número inválido. Digite entre 1 e {len(vaga_list)}.")
                    input("Pressione Enter para continuar...")

        elif screen == "detail":
            draw_detail(detail_id)
            cmd = input("\n> ").strip().lower()

            if cmd == "q":
                break
            elif cmd in ("b", ""):
                screen = "dashboard"
            elif cmd == "l":
                publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "luz_on")
            elif cmd == "x":
                publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "luz_off")
            elif cmd == "a":
                publish_command(client, TOPIC_CMD_VAGA.format(detail_id), "auto")

    client.loop_stop()
    client.disconnect()
    print("Sistema encerrado.")

if __name__ == "__main__":
    main()
