import time
from datetime import datetime
import struct
import math
import board
import adafruit_dht
from smbus2 import SMBus
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt

# ----------------- CONFIG -----------------
# DHT22 no GPIO4 (pino fisico 7)
DHT_PIN = board.D4
dht = adafruit_dht.DHT22(DHT_PIN, use_pulseio=False)

I2C_BUS = 1
BMP180_ADDR = 0x77
BMP180_OSS = 1  # 0..3 (maior = mais lento e mais preciso)

# ----------------- DRIVER BMP180 -----------------
class BMP180:
    REG_CONTROL = 0xF4
    REG_RESULT = 0xF6
    CMD_TEMP = 0x2E
    CMD_PRESS = 0x34

    def __init__(self, bus, addr=BMP180_ADDR, oss=BMP180_OSS):
        self.bus = bus
        self.addr = addr
        self.oss = oss
        self._read_calibration()

    def _rS16(self, reg):
        b = self.bus.read_i2c_block_data(self.addr, reg, 2)
        return struct.unpack('>h', bytes(b))[0]

    def _rU16(self, reg):
        b = self.bus.read_i2c_block_data(self.addr, reg, 2)
        return struct.unpack('>H', bytes(b))[0]

    def _read_calibration(self):
        self.AC1 = self._rS16(0xAA); self.AC2 = self._rS16(0xAC); self.AC3 = self._rS16(0xAE)
        self.AC4 = self._rU16(0xB0); self.AC5 = self._rU16(0xB2); self.AC6 = self._rU16(0xB4)
        self.B1  = self._rS16(0xB6); self.B2  = self._rS16(0xB8)
        self.MB  = self._rS16(0xBA); self.MC  = self._rS16(0xBC); self.MD  = self._rS16(0xBE)

    def _raw_temp(self):
        self.bus.write_byte_data(self.addr, self.REG_CONTROL, self.CMD_TEMP)
        time.sleep(0.005)
        msb, lsb = self.bus.read_i2c_block_data(self.addr, self.REG_RESULT, 2)
        return (msb << 8) + lsb

    def _raw_press(self):
        self.bus.write_byte_data(self.addr, self.REG_CONTROL, self.CMD_PRESS + (self.oss << 6))
        time.sleep(0.005 + 0.003 * (1 << self.oss))
        msb, lsb, xlsb = self.bus.read_i2c_block_data(self.addr, self.REG_RESULT, 3)
        up = ((msb << 16) + (lsb << 8) + xlsb) >> (8 - self.oss)
        return up

    def read_temperature_pressure(self):
        ut = self._raw_temp()
        up = self._raw_press()

        x1 = ((ut - self.AC6) * self.AC5) >> 15
        x2 = (self.MC << 11) // (x1 + self.MD)
        b5 = x1 + x2
        temp_c = ((b5 + 8) >> 4) / 10.0

        b6 = b5 - 4000
        x1 = (self.B2 * (b6 * b6 >> 12)) >> 11
        x2 = (self.AC2 * b6) >> 11
        x3 = x1 + x2
        b3 = (((self.AC1 * 4 + x3) << self.oss) + 2) >> 2

        x1 = (self.AC3 * b6) >> 13
        x2 = (self.B1 * (b6 * b6 >> 12)) >> 16
        x3 = ((x1 + x2) + 2) >> 2
        b4 = (self.AC4 * (x3 + 32768)) >> 15
        b7 = (up - b3) * (50000 >> self.oss)

        if b7 < 0x80000000:
            p = (b7 * 2) // b4
        else:
            p = (b7 // b4) * 2

        x1 = (p >> 8) * (p >> 8)
        x1 = (x1 * 3038) >> 16
        x2 = (-7357 * p) >> 16
        press_pa = p + ((x1 + x2 + 3791) >> 4)

        return temp_c, press_pa  # graus C, Pa

# ----------------- LEITURA DHT ROBUSTA -----------------
def ler_dht22(max_tentativas=8, pausa=1.0, min_ok=2):
    """Le o DHT22 com varias tentativas, retorna media simples das leituras boas."""
    vals = []
    for _ in range(max_tentativas):
        try:
            t = dht.temperature
            h = dht.humidity
            if t is not None and h is not None:
                vals.append((h, t))
                if len(vals) >= min_ok:
                    break
        except RuntimeError:
            pass
        time.sleep(pausa)
    if not vals:
        return None, None
    umid = sum(v[0] for v in vals)/len(vals)
    temp = sum(v[1] for v in vals)/len(vals)
    return umid, temp

# ----------------- PRESSAO NIVEL DO MAR -----------------
def pressao_nivel_mar(p_hpa, temp_c, alt_m):
    if p_hpa is None or temp_c is None or alt_m <= 0:
        return None
    T = temp_c + 273.15
    p0 = p_hpa * math.exp((9.80665 * 0.0289644 * alt_m) / (8.3144598 * T))
    return p0

# ----------------- FUNCOES DE LEITURA -----------------
bus = SMBus(I2C_BUS)
bmp = BMP180(bus)

def ler_sensores(altitude_m):
    """Uma leitura (DHT22 com retries + BMP180)."""
    umid, temp_c_dht = ler_dht22()
    try:
        temp_c_bmp, press_pa = bmp.read_temperature_pressure()
        press_hpa = press_pa / 100.0
    except Exception:
        temp_c_bmp = None
        press_hpa = None

    temp_para_sl = temp_c_bmp if temp_c_bmp is not None else temp_c_dht
    press_sl_hpa = pressao_nivel_mar(press_hpa, temp_para_sl, altitude_m)

    return {
        "timestamp": datetime.now(),
        "temp_c": temp_c_dht,
        "umid_pct": umid,
        "press_hpa": press_hpa,
        "press_sl_hpa": press_sl_hpa,
        "temp_bmp_c": temp_c_bmp,
    }

def media_de_medicoes(medicoes):
    """Media campo-a-campo ignorando None."""
    if not medicoes:
        return None
    chaves = ["temp_c","umid_pct","press_hpa","press_sl_hpa","temp_bmp_c"]
    soma = {k: 0.0 for k in chaves}
    cont = {k: 0 for k in chaves}
    for d in medicoes:
        for k in chaves:
            v = d.get(k)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                soma[k] += v
                cont[k] += 1
    res = {k: (soma[k]/cont[k] if cont[k] > 0 else None) for k in chaves}
    res["timestamp"] = datetime.now()
    return res

def fmt_val(x):
    return f"{x:.1f}" if (x is not None and not (isinstance(x, float) and math.isnan(x))) else "-"

# ----------------- UI -----------------
st.set_page_config(page_title="Estacao Pi: DHT22 + BMP180", layout="wide")
st.title("Estacao Raspberry Pi (DHT22 + BMP180)")
st.caption("Leituras em tempo real de temperatura, umidade e pressao.")

with st.sidebar:
    st.header("Configuracoes")
    altitude_m = st.number_input("Altitude do local (m)", min_value=0, max_value=5000, value=0, step=1)
    modo = st.radio("Tipo de leitura", ["Unica", "Media de N"], horizontal=True)
    if modo == "Media de N":
        n_amostras = st.number_input("Numero de amostras (N)", min_value=2, max_value=20, value=3, step=1)
        pausa_amostras = st.number_input("Pausa entre amostras (s)", min_value=0.5, max_value=5.0, value=1.5, step=0.5)
    else:
        n_amostras = 1
        pausa_amostras = 0.0
    limite = st.number_input("Historico max. (amostras)", min_value=10, max_value=5000, value=500, step=10)
    if st.button("Limpar historico"):
        st.session_state.historico = pd.DataFrame(columns=["timestamp","temp_c","umid_pct","press_hpa","press_sl_hpa","temp_bmp_c"])

if "historico" not in st.session_state:
    st.session_state.historico = pd.DataFrame(columns=["timestamp","temp_c","umid_pct","press_hpa","press_sl_hpa","temp_bmp_c"])

# Botao principal: uma amostra (ou media de N) por clique
if st.button("Ler agora"):
    medicoes = []
    for i in range(int(n_amostras)):
        medicoes.append(ler_sensores(altitude_m))
        if i < int(n_amostras) - 1:
            time.sleep(float(pausa_amostras))
    dados = media_de_medicoes(medicoes) if int(n_amostras) > 1 else medicoes[0]

    df = pd.DataFrame([dados])
    st.session_state.historico = pd.concat([st.session_state.historico, df], ignore_index=True)
    if len(st.session_state.historico) > limite:
        st.session_state.historico = st.session_state.historico.iloc[-limite:]

hist = st.session_state.historico
ultima = hist.iloc[-1] if not hist.empty else None

c1, c2, c3, c4 = st.columns(4)
if ultima is not None:
    c1.metric("Temp DHT22 (C)", fmt_val(ultima["temp_c"]))
    c2.metric("Umidade (%)", fmt_val(ultima["umid_pct"]))
    c3.metric("Pressao (hPa)", fmt_val(ultima["press_hpa"]))
    c4.metric("Pressao SL (hPa)", fmt_val(ultima["press_sl_hpa"]))
    st.caption(f"Ultima leitura: {ultima['timestamp']}")

st.divider()
aba1, aba2 = st.tabs(["Graficos", "Dados"])

with aba1:
    if not hist.empty:
        df = hist.set_index("timestamp")

        # --- Grafico de temperatura(s) ---
        fig1, ax1 = plt.subplots()
        if "temp_c" in df.columns:
            ax1.plot(df.index, df["temp_c"], label="Temp DHT22 (C)")
        if "temp_bmp_c" in df.columns:
            ax1.plot(df.index, df["temp_bmp_c"], label="Temp BMP180 (C)")
        ax1.set_xlabel("Tempo")
        ax1.set_ylabel("C")
        ax1.legend()
        st.pyplot(fig1, clear_figure=True)

        # --- Grafico de umidade ---
        fig2, ax2 = plt.subplots()
        if "umid_pct" in df.columns:
            ax2.plot(df.index, df["umid_pct"], label="Umidade (%)")
        ax2.set_xlabel("Tempo")
        ax2.set_ylabel("%")
        ax2.legend()
        st.pyplot(fig2, clear_figure=True)

        # --- Grafico de pressao ---
        fig3, ax3 = plt.subplots()
        if "press_hpa" in df.columns:
            ax3.plot(df.index, df["press_hpa"], label="Pressao (hPa)")
        if "press_sl_hpa" in df.columns:
            ax3.plot(df.index, df["press_sl_hpa"], label="Pressao SL (hPa)")
        ax3.set_xlabel("Tempo")
        ax3.set_ylabel("hPa")
        ax3.legend()
        st.pyplot(fig3, clear_figure=True)

with aba2:
    if not hist.empty:
        tail = hist.tail(500).copy()
        tail["timestamp"] = tail["timestamp"].astype(str)
        st.code(tail.to_string(index=False))
        csv = tail.to_csv(index=False).encode("utf-8")
        st.download_button("Baixar CSV", csv, "historico.csv", "text/csv")
    else:
        st.write("Sem dados ainda. Clique em 'Ler agora' para registrar uma amostra.")
