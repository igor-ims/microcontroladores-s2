import os
import time
from datetime import datetime, timezone
import struct
import math
import board
import adafruit_dht
from smbus2 import SMBus
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
import requests

# ================= CONFIG GERAL =================
# Coloque sua chave aqui (ou deixe vazio e use a env var OPENWEATHER_API_KEY)
OPENWEATHER_API_KEY = "SUA_CHAVE_AQUI"

# Se preferir usar variavel de ambiente, esta linha faz fallback automatico:
if not OPENWEATHER_API_KEY:
    OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")

# DHT22 no GPIO4 (pino fisico 7)
DHT_PIN = board.D4
dht = adafruit_dht.DHT22(DHT_PIN, use_pulseio=False)

I2C_BUS = 1
BMP180_ADDR = 0x77
BMP180_OSS = 1  # 0..3 (maior = mais lento e mais preciso)

# ================= DRIVER BMP180 =================
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

        return temp_c, press_pa  # C, Pa

# ================= LEITURA DHT ROBUSTA =================
def ler_dht22(max_tentativas=8, pausa=1.0, min_ok=2):
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
    umid = sum(v[0] for v in vals) / len(vals)
    temp = sum(v[1] for v in vals) / len(vals)
    return umid, temp

# ================= PRESSAO NIVEL DO MAR =================
def pressao_nivel_mar(p_hpa, temp_c, alt_m):
    if p_hpa is None or temp_c is None or alt_m <= 0:
        return None
    T = temp_c + 273.15
    p0 = p_hpa * math.exp((9.80665 * 0.0289644 * alt_m) / (8.3144598 * T))
    return p0

# ================= OPENWEATHER =================
def fetch_openweather(lat, lon, api_key, timeout=5):
    # OpenWeather: temp C (units=metric), pressao hPa.
    # main.pressure ~ pressao ao nivel do terreno;
    # pode ter main.sea_level (MSL) e main.grnd_level (terreno).
    if not api_key:
        raise ValueError("OPENWEATHER_API_KEY nao configurada.")
    url = (
        "https://api.openweathermap.org/data/2.5/weather"
        f"?lat={lat}&lon={lon}&appid={api_key}&units=metric"
    )
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    main = j.get("main", {})
    return {
        "api_temp_c": main.get("temp"),
        "api_umid_pct": main.get("humidity"),
        "api_press_hpa": main.get("grnd_level", main.get("pressure")),
        "api_press_sl_hpa": main.get("sea_level"),
        "api_provider": "OpenWeather",
        "api_ts": datetime.fromtimestamp(j.get("dt", 0), tz=timezone.utc).isoformat(),
    }

# ================= FUNCOES DE LEITURA =================
bus = SMBus(I2C_BUS)
bmp = BMP180(bus)

def ler_sensores(altitude_m):
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

def ler_com_api(altitude_m, lat, lon):
    base = ler_sensores(altitude_m)
    try:
        api = fetch_openweather(lat, lon, OPENWEATHER_API_KEY)
        base.update(api)
    except Exception as e:
        # Mantem campos de API como None para nao quebrar graficos
        st.error(f"Erro ao consultar OpenWeather: {e}")
        base.update({
            "api_temp_c": None, "api_umid_pct": None,
            "api_press_hpa": None, "api_press_sl_hpa": None,
            "api_provider": "OpenWeather"
        })
    return base

def media_de_medicoes(medicoes):
    if not medicoes:
        return None
    chaves = ["temp_c","umid_pct","press_hpa","press_sl_hpa","temp_bmp_c",
              "api_temp_c","api_umid_pct","api_press_hpa","api_press_sl_hpa","api_provider"]
    soma = {k: 0.0 for k in chaves if k != "api_provider"}
    cont = {k: 0 for k in chaves if k != "api_provider"}
    prov = None
    for d in medicoes:
        prov = d.get("api_provider", prov)
        for k in soma:
            v = d.get(k)
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                soma[k] += v
                cont[k] += 1
    res = {k: (soma[k]/cont[k] if cont[k] > 0 else None) for k in soma}
    res["timestamp"] = datetime.now()
    res["api_provider"] = prov
    return res

def fmt_val(x):
    return f"{x:.1f}" if (x is not None and not (isinstance(x, float) and math.isnan(x))) else "-"

# ================= UI =================
st.set_page_config(page_title="Estacao Pi: DHT22 + BMP180 + OpenWeather", layout="wide")
st.title("Estacao Raspberry Pi (DHT22 + BMP180) + Comparativo OpenWeather")
st.caption("Leituras em tempo real e comparacao com a API do OpenWeather.")

with st.sidebar:
    st.header("Configuracoes")
    altitude_m = st.number_input("Altitude do local (m)", min_value=0, max_value=5000, value=0, step=1)

    st.subheader("Leitura local")
    modo = st.radio("Tipo de leitura", ["Unica", "Media de N"], horizontal=True)
    if modo == "Media de N":
        n_amostras = st.number_input("Numero de amostras (N)", min_value=2, max_value=20, value=3, step=1)
        pausa_amostras = st.number_input("Pausa entre amostras (s)", min_value=0.5, max_value=5.0, value=1.5, step=0.5)
    else:
        n_amostras = 1
        pausa_amostras = 0.0

    st.subheader("OpenWeather")
    # Mostra o status da chave sem expor a chave:
    st.write("Chave configurada:", "OK" if OPENWEATHER_API_KEY else "NAO (defina em OPENWEATHER_API_KEY)")
    lat = st.number_input("Latitude", value=-23.550000, format="%.6f")
    lon = st.number_input("Longitude", value=-46.633000, format="%.6f")

    limite = st.number_input("Historico max. (amostras)", min_value=10, max_value=5000, value=500, step=10)
    if st.button("Limpar historico"):
        st.session_state.historico = pd.DataFrame(columns=[
            "timestamp","temp_c","umid_pct","press_hpa","press_sl_hpa","temp_bmp_c",
            "api_temp_c","api_umid_pct","api_press_hpa","api_press_sl_hpa","api_provider"
        ])

if "historico" not in st.session_state:
    st.session_state.historico = pd.DataFrame(columns=[
        "timestamp","temp_c","umid_pct","press_hpa","press_sl_hpa","temp_bmp_c",
        "api_temp_c","api_umid_pct","api_press_hpa","api_press_sl_hpa","api_provider"
    ])

# Botao principal
if st.button("Ler agora"):
    medicoes = []
    for i in range(int(n_amostras)):
        medicoes.append(ler_com_api(altitude_m, lat, lon))
        if i < int(n_amostras) - 1:
            time.sleep(float(pausa_amostras))
    dados = media_de_medicoes(medicoes) if int(n_amostras) > 1 else medicoes[0]

    df = pd.DataFrame([dados])
    st.session_state.historico = pd.concat([st.session_state.historico, df], ignore_index=True)
    if len(st.session_state.historico) > limite:
        st.session_state.historico = st.session_state.historico.iloc[-limite:]

hist = st.session_state.historico
ultima = hist.iloc[-1] if not hist.empty else None

# ----- Metricas -----
c1, c2, c3, c4 = st.columns(4)
if ultima is not None:
    c1.metric("Temp DHT22 (C)", fmt_val(ultima["temp_c"]))
    c2.metric("Umidade (%)", fmt_val(ultima["umid_pct"]))
    c3.metric("Pressao (hPa)", fmt_val(ultima["press_hpa"]))
    c4.metric("Pressao SL (hPa)", fmt_val(ultima["press_sl_hpa"]))
    st.caption(f"Ultima leitura: {ultima['timestamp']} | OpenWeather")

st.divider()
aba1, aba2, aba3 = st.tabs(["Graficos", "Erros (Sensor - API)", "Dados"])

with aba1:
    if not hist.empty:
        df = hist.set_index("timestamp")

        # --- Temperatura ---
        fig1, ax1 = plt.subplots()
        if "temp_c" in df.columns:
            ax1.plot(df.index, df["temp_c"], label="Temp DHT22 (C)")
        if "temp_bmp_c" in df.columns:
            ax1.plot(df.index, df["temp_bmp_c"], label="Temp BMP180 (C)")
        if "api_temp_c" in df.columns and df["api_temp_c"].notna().any():
            ax1.plot(df.index, df["api_temp_c"], label="Temp OpenWeather (C)")
        ax1.set_xlabel("Tempo"); ax1.set_ylabel("C"); ax1.legend()
        st.pyplot(fig1, clear_figure=True)

        # --- Umidade ---
        fig2, ax2 = plt.subplots()
        if "umid_pct" in df.columns:
            ax2.plot(df.index, df["umid_pct"], label="Umidade Sensor (%)")
        if "api_umid_pct" in df.columns and df["api_umid_pct"].notna().any():
            ax2.plot(df.index, df["api_umid_pct"], label="Umidade OpenWeather (%)")
        ax2.set_xlabel("Tempo"); ax2.set_ylabel("%"); ax2.legend()
        st.pyplot(fig2, clear_figure=True)

        # --- Pressao (terreno e MSL) ---
        fig3, ax3 = plt.subplots()
        if "press_hpa" in df.columns:
            ax3.plot(df.index, df["press_hpa"], label="Pressao Sensor (hPa)")
        if "api_press_hpa" in df.columns and df["api_press_hpa"].notna().any():
            ax3.plot(df.index, df["api_press_hpa"], label="Pressao OpenWeather (hPa)")
        if "press_sl_hpa" in df.columns and df["press_sl_hpa"].notna().any():
            ax3.plot(df.index, df["press_sl_hpa"], label="Pressao SL Sensor (hPa)")
        if "api_press_sl_hpa" in df.columns and df["api_press_sl_hpa"].notna().any():
            ax3.plot(df.index, df["api_press_sl_hpa"], label="Pressao SL OpenWeather (hPa)")
        ax3.set_xlabel("Tempo"); ax3.set_ylabel("hPa"); ax3.legend()
        st.pyplot(fig3, clear_figure=True)

with aba2:
    st.write("Diferenca (Sensor - OpenWeather). Positivo = sensor acima da API.")
    if not hist.empty and hist["api_temp_c"].notna().any():
        df = hist.tail(200).copy()

        df["dif_temp_C"] = df["temp_c"] - df["api_temp_c"]
        df["dif_umid_%"] = df["umid_pct"] - df["api_umid_pct"]
        df["dif_press_hPa"] = df["press_hpa"] - df["api_press_hpa"]
        df["dif_pressSL_hPa"] = df["press_sl_hpa"] - df["api_press_sl_hpa"]

        def mae(s):
            s = s.dropna()
            return s.abs().mean() if not s.empty else None

        colA, colB, colC, colD = st.columns(4)
        colA.metric("MAE Temp (C)", fmt_val(mae(df["dif_temp_C"])))
        colB.metric("MAE Umidade (%)", fmt_val(mae(df["dif_umid_%"])))
        colC.metric("MAE Pressao (hPa)", fmt_val(mae(df["dif_press_hPa"])))
        colD.metric("MAE Pressao SL (hPa)", fmt_val(mae(df["dif_pressSL_hPa"])))

        figd1, axd1 = plt.subplots()
        if df["dif_temp_C"].notna().any(): axd1.plot(df["timestamp"], df["dif_temp_C"], label="Delta Temp (C)")
        if df["dif_umid_%"].notna().any(): axd1.plot(df["timestamp"], df["dif_umid_%"], label="Delta Umidade (%)")
        axd1.set_xlabel("Tempo"); axd1.set_ylabel("Diferenca"); axd1.legend()
        st.pyplot(figd1, clear_figure=True)

        figd2, axd2 = plt.subplots()
        if df["dif_press_hPa"].notna().any(): axd2.plot(df["timestamp"], df["dif_press_hPa"], label="Delta Pressao (hPa)")
        if df["dif_pressSL_hPa"].notna().any(): axd2.plot(df["timestamp"], df["dif_pressSL_hPa"], label="Delta Pressao SL (hPa)")
        axd2.set_xlabel("Tempo"); axd2.set_ylabel("Diferenca"); axd2.legend()
        st.pyplot(figd2, clear_figure=True)
    else:
        st.info("Faca ao menos uma leitura com OpenWeather para calcular as diferencas.")

with aba3:
    if not hist.empty:
        tail = hist.tail(500).copy()
        tail["timestamp"] = tail["timestamp"].astype(str)
        st.code(tail.to_string(index=False))
        csv = tail.to_csv(index=False).encode("utf-8")
        st.download_button("Baixar CSV", csv, "historico.csv", "text/csv")
    else:
        st.write("Sem dados ainda. Clique em Ler agora para registrar uma amostra.")
