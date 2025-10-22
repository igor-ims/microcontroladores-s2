import json
import tkinter as tk
from tkinter import ttk
import paho.mqtt.client as mqtt

# ================= CONFIG MQTT =================
BROKER = "broker.hivemq.com"
PORT = 1883
TOPIC = "raspberrypi/estacao"

# ================= FUNÇÃO DE FORMATAÇÃO =================
def fmt(v):
            if v is None:
                return "-"
            try:
                return f"{float(v):.1f}"
            except Exception:
                return str(v)

# ================= GUI =================
janela = tk.Tk()
janela.title("Estação Raspberry Pi - Monitor MQTT")
janela.geometry("420x260")
janela.resizable(False, False)

titulo = tk.Label(janela, text="📡 Estação Meteorológica - MQTT", font=("Arial", 14, "bold"))
titulo.pack(pady=10)

frame = ttk.Frame(janela, padding=10)
frame.pack(fill="both", expand=True)

# Campos de exibição
labels = {}
for i, campo in enumerate(["Temperatura (°C)", "Umidade (%)", "Pressão (hPa)", "Pressão SL (hPa)", "Horário"]):
    ttk.Label(frame, text=campo + ":", font=("Arial", 11)).grid(row=i, column=0, sticky="w", pady=5)
    labels[campo] = ttk.Label(frame, text="---", font=("Arial", 11, "bold"))
    labels[campo].grid(row=i, column=1, sticky="w", padx=10)

status = tk.Label(janela, text="Aguardando dados MQTT...", fg="gray")
status.pack(pady=5)

# ================= FUNÇÕES MQTT =================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        status.config(text=f"Conectado ao broker MQTT ({BROKER})", fg="green")
        client.subscribe(TOPIC)
    else:
        status.config(text=f"Falha na conexão MQTT: código {rc}", fg="red")

def on_message(client, userdata, msg):
    try:
        dados = json.loads(msg.payload.decode())

        labels["Temperatura (°C)"].config(text=fmt(dados.get("temp_c")))
        labels["Umidade (%)"].config(text=fmt(dados.get("umid_pct")))
        labels["Pressão (hPa)"].config(text=fmt(dados.get("press_hpa")))
        labels["Pressão SL (hPa)"].config(text=fmt(dados.get("press_sl_hpa")))
        labels["Horário"].config(text=dados.get("timestamp", "-"))

    except Exception as e:
        status.config(text=f"Erro ao processar JSON: {e}", fg="red")
        print(f"Erro ao processar JSON: {e}")

# ================= MQTT CLIENT =================
mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(BROKER, PORT, 60)

# Roda o loop MQTT em segundo plano
mqtt_client.loop_start()

# ================= LOOP PRINCIPAL =================
janela.mainloop()
