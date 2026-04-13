from machine import Pin, ADC, PWM, time_pulse_us
import time
import network
import json 
from umqtt.simple import MQTTClient

import dht

ID_VAGA = "01" 
MQTT_CLIENT_ID = f"vaga_{ID_VAGA}"
MQTT_BROKER = "micros-ctba.microsdns.com.br" 
MQTT_USER = ""
MQTT_PASSWORD = ""

WIFI_SSID = "Eggsbennedit"
WIFI_PASSWORD = "eggsSenha"

TOPIC_STATUS = f"vagasystem/{ID_VAGA}/status"
TOPIC_COMANDOS = f"vagasystem/{ID_VAGA}/comandos"
TOPIC_COMANDOS_ALL = f"vagasystem/all/comandos"

segmentos = {
    'a': Pin(13, Pin.OUT), 
    'b': Pin(12, Pin.OUT), 
    'c': Pin(14, Pin.OUT),
    'd': Pin(27, Pin.OUT), 
    'e': Pin(26, Pin.OUT), 
    'f': Pin(25, Pin.OUT), 
    'g': Pin(33, Pin.OUT)
}

def exibir_O(): # ocupado
    for seg in ['a', 'b', 'c', 'd', 'e', 'f']: segmentos[seg].value(0)
    segmentos['g'].value(1)

def exibir_L(): # livre
    for seg in ['f', 'e', 'd']: segmentos[seg].value(0)
    for seg in ['a', 'b', 'c', 'g']: segmentos[seg].value(1)

def desliga():
    for seg in segmentos.values(): seg.value(0)

trig = Pin(5, Pin.OUT)
echo = Pin(18, Pin.IN)
ldr = ADC(Pin(34))
ldr.atten(ADC.ATTN_11DB)

sensor_dht = dht.DHT11(Pin(32))

led = PWM(Pin(2, Pin.OUT))
led.freq(1000)

def pega_distancia():
    trig.value(0)
    time.sleep_us(2)
    trig.value(1)
    time.sleep_us(10)
    trig.value(0)
        
    duracao = time_pulse_us(echo, 1, 30000) # timeout 30ms
    return (duracao * 0.0343) / 2 if duracao > 0 else 400

# controle geral
forca_publicacao = False
modo_auto = True
led_forcado_ligado = False

def recebe_comando(topic, msg):
    global forca_publicacao, modo_auto, led_forcado_ligado
    try:
        pacote = json.loads(msg.decode('utf-8'))
        comando = pacote.get("comando", "")
        print(f"\n[!] Comando recebido: {comando}")

        if comando == "status":
            forca_publicacao = True # Força envio de dados no próximo loop
        elif comando == "luz_on":
            modo_auto = False
            led_forcado_ligado = True
        elif comando == "luz_off":
            modo_auto = False
            led_forcado_ligado = False
        elif comando == "auto":
            modo_auto = True
    except Exception as e:
        print("Erro ao interpretar comando:", e)

# wifi
print("Conectando ao Wi-Fi...", end="")
sta_if = network.WLAN(network.STA_IF)
sta_if.active(True)
sta_if.connect(WIFI_SSID, WIFI_PASSWORD) 
while not sta_if.isconnected():
    print(".", end="")
    time.sleep(0.1)
print(" Conectado!")

# mqtt
print("Conectando ao Broker MQTT...", end="")
client = MQTTClient(MQTT_CLIENT_ID, MQTT_BROKER)
client.set_callback(recebe_comando) 
client.connect()
client.subscribe(TOPIC_COMANDOS)
client.subscribe(TOPIC_COMANDOS_ALL)
print(" Conectado e escutando comandos!")

# loop
last_state = -1 # guatda se estava livre ou ocupado
last_time = time.ticks_ms()

while True:
    try:
        client.check_msg()
    except:
        pass

    now = time.ticks_ms()
    if time.ticks_diff(now, last_time) > 500:
        try:
            distancia = pega_distancia()
            valor_luz = ldr.read()
            try:
                sensor_dht.measure()
                umidade = sensor_dht.humidity()
            except:
                umidade = 0
            esta_ocupado = (0 < distancia < 50)
            
            brilho_led_atual = 0

            if modo_auto:
                if esta_ocupado:
                    exibir_O()
                    brilho_calculado = 1023 - int((valor_luz / 4095) * 923)
                    brilho_led_atual = max(100, brilho_calculado)
                    led.duty(brilho_led_atual) 
                else:
                    exibir_L()
                    led.duty(0)
            else:
                # se o controle mandou, ignora o LDR
                if esta_ocupado: exibir_O()
                else: exibir_L()
                
                if led_forcado_ligado:
                    brilho_led_atual = 1023
                    led.duty(1023)
                else:
                    led.duty(0)

            estado_atual = 1 if esta_ocupado else 0
            
            if estado_atual != last_state or forca_publicacao:
                dados = {
                    "id_vaga": ID_VAGA,
                    "ocupado": esta_ocupado,
                    "distancia_cm": round(distancia, 1),
                    "luz_ambiente": valor_luz,
                    "brilho_led": brilho_led_atual,
                    "umidade": umidade
                }
                
                client.publish(TOPIC_STATUS, json.dumps(dados), qos=1)
                
                print(f"[PUB] {dados}")
                last_state = estado_atual
                forca_publicacao = False

        except Exception as e:
            print("Erro no loop:", e)
            time.sleep(2)
            
        last_time = now