import os
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# --- CONFIGURACIÓN DE CREDENCIALES Y BINANCE ---
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_SECRET_KEY')

exchange = ccxt.binance({
    'apiKey': api_key,
    'secret': api_secret,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future',  # Mercado de Futuros Perpetuos (USDT-M)
    }
})

# Cargar mercados para asegurar precisión de decimales y reglas
exchange.load_markets()

# --- PARÁMETROS DE TRADING ---
SYMBOL = 'BTC/USDT'
TIMEFRAME = '15m'
LEVERAGE = 10
MARGEN_USDT = 1.0  # Margen de $1.00 USDT
CALLBACK_RATE = 0.3  # Retroceso del 0.3% para el Trailing Stop Nativo de Binance

# --- 1. VERIFICACIÓN DE HORARIO INSTITUCIONAL ---
def es_horario_operativo():
    now_utc = datetime.now(timezone.utc)
    hora_decimal = now_utc.hour + now_utc.minute / 60.0
    
    tokio = 0.0 <= hora_decimal <= 9.0
    londres = 8.0 <= hora_decimal <= 16.5
    ny = 13.5 <= hora_decimal <= 20.0
    
    return tokio or londres or ny

# --- 2. INDICADORES TÉCNICOS ---
def calcular_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calcular_canal_sr(df, lookback=130):
    highs = df['high'].rolling(window=lookback).max()
    lows = df['low'].rolling(window=lookback).min()
    atr = (df['high'] - df['low']).rolling(window=200).mean()
    resistencia = highs - (atr * 0.5)
    soporte = lows + (atr * 0.5)
    return soporte, resistencia

def detectar_divergencias(df):
    close = df['close']
    rsi = df['rsi']
    
    div_alcista = (close.iloc[-1] < close.iloc[-5]) and (rsi.iloc[-1] > rsi.iloc[-5]) and (rsi.iloc[-1] < 45)
    div_bajista = (close.iloc[-1] > close.iloc[-5]) and (rsi.iloc[-1] < rsi.iloc[-15]) and (rsi.iloc[-1] > 55)
    
    return bool(div_alcista), bool(div_bajista)

# --- 3. REGISTRO EN BITÁCORA (CSV) ---
def registrar_en_csv(evento, tipo, precio_entrada, precio_salida, resultado_pct, estado, id_orden=""):
    archivo = "historial_operaciones.csv"
    columnas = ["Fecha_Hora", "Evento", "Simbolo", "Tipo", "Precio_Entrada", "Precio_Salida", "Resultado_Pct", "Estado", "ID_Orden"]
    
    if not os.path.exists(archivo):
        df_init = pd.DataFrame(columns=columnas)
        df_init.to_csv(archivo, index=False)
        
    nueva_fila = {
        "Fecha_Hora": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "Evento": evento,
        "Simbolo": SYMBOL,
        "Tipo": tipo,
        "Precio_Entrada": precio_entrada,
        "Precio_Salida": precio_salida,
        "Resultado_Pct": resultado_pct,
        "Estado": estado,
        "ID_Orden": id_orden
    }
    
    df = pd.DataFrame([nueva_fila])
    df.to_csv(archivo, mode='a', header=False, index=False)

# --- 4. EJECUCIÓN PRINCIPAL DEL BOT ---
def ejecutar_bot():
    print("--------------------------------------------------")
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] Iniciando ciclo del bot (Binance)...")
    
    if not es_horario_operativo():
        print("⏳ Fuera de horario institucional (Tokio/Londres/NY). El bot se pausa por seguridad.")
        return

    try:
        # Configurar apalancamiento y tipo de margen en Binance
        try:
            exchange.set_margin_mode('ISOLATED', SYMBOL)
        except Exception:
            pass  # Si ya está ajustado a Aislado, continúa
            
        try:
            exchange.set_leverage(LEVERAGE, SYMBOL)
        except Exception:
            pass

        balance = exchange.fetch_balance()
        usdt_free = balance['USDT']['free']
        print(f"💰 Balance disponible en USDT (Binance): {usdt_free:.4f}")

        # Obtener datos de mercado (Velas de 15m)
        bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=250)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        df['ema_9'] = df['close'].ewm(span=9).mean()
        df['ema_21'] = df['close'].ewm(span=21).mean()
        df['ema_200'] = df['close'].ewm(span=200).mean()
        df['rsi'] = calcular_rsi(df['close'], 14)
        
        soporte, resistencia = calcular_canal_sr(df, lookback=130)
        df['soporte'] = soporte
        df['resistencia'] = resistencia
        
        precio_actual = df['close'].iloc[-1]
        ema_200 = df['ema_200'].iloc[-1]
        sup_actual = df['soporte'].iloc[-1]
        res_actual = df['resistencia'].iloc[-1]
        rsi_actual = df['rsi'].iloc[-1]
        
        div_alcista, div_bajista = detectar_divergencias(df)
        
        print(f"📈 Precio Actual BTC: {precio_actual}")
        print(f"📉 EMA 200 (Tendencia): {ema_200:.2f}")
        print(f"📊 Canal S&R -> Soporte: {sup_actual:.2f} | Resistencia: {res_actual:.2f}")
        print(f"📊 RSI Actual: {rsi_actual:.2f} | Div Alcista: {div_alcista} | Div Bajista: {div_bajista}")

        monto_nominal_usdt = MARGEN_USDT * LEVERAGE
        
        # Ajustar la cantidad al tamaño de lote permitido por Binance
        market = exchange.market(SYMBOL)
        cantidad_btc = exchange.amount_to_precision(SYMBOL, monto_nominal_usdt / precio_actual)

        # Verificar posiciones abiertas en Binance
        posiciones = exchange.fetch_positions([SYMBOL])
        posicion_activa = next((p for p in posiciones if p['symbol'] == SYMBOL and float(p['contracts']) > 0), None)

        if not posicion_activa:
            # --- LÓGICA DE APERTURA ---
            condicion_long = (
                precio_actual > ema_200 and 
                rsi_actual < 45 and 
                div_alcista and 
                (res_actual - precio_actual) > (precio_actual * 0.005)
            )

            condicion_short = (
                precio_actual < ema_200 and 
                rsi_actual > 55 and 
                div_bajista and 
                (precio_actual - sup_actual) > (precio_actual * 0.005)
            )

            if condicion_long:
                print("🚀 ¡Señal LONG detectada en Binance! Abriendo posición...")
                order = exchange.create_market_buy_order(SYMBOL, cantidad_btc)
                
                # 1. Stop Loss Fijo nativo (1% por debajo)
                precio_sl = float(exchange.price_to_precision(SYMBOL, precio_actual * 0.99))
                try:
                    exchange.create_order(
                        SYMBOL, 'STOP_MARKET', 'sell', cantidad_btc, None,
                        {'stopPrice': precio_sl, 'reduceOnly': True}
                    )
                    print(f"🛡️ Stop Loss nativo activado en Binance en: {precio_sl}")
                except Exception as e:
                    print(f"⚠️ Error al crear Stop Loss en Binance: {e}")

                # 2. Trailing Stop Nativo de Binance (persigue el precio con 0.3% de retroceso)
                try:
                    exchange.create_order(
                        SYMBOL, 'TRAILING_STOP_MARKET', 'sell', cantidad_btc, None,
                        {'callbackRate': CALLBACK_RATE, 'reduceOnly': True}
                    )
                    print(f"🎯 Trailing Stop nativo configurado con callbackRate: {CALLBACK_RATE}%")
                except Exception as e:
                    print(f"⚠️ Error al crear Trailing Stop nativo en Binance: {e}")

                registrar_en_csv("APERTURA", "LONG", precio_actual, 0.0, 0.0, "ABIERTA", order.get('id', ''))
                
            elif condicion_short:
                print("📉 ¡Señal SHORT detectada en Binance! Abriendo posición...")
                order = exchange.create_market_sell_order(SYMBOL, cantidad_btc)
                
                # 1. Stop Loss Fijo nativo (1% por encima)
                precio_sl = float(exchange.price_to_precision(SYMBOL, precio_actual * 1.01))
                try:
                    exchange.create_order(
                        SYMBOL, 'STOP_MARKET', 'buy', cantidad_btc, None,
                        {'stopPrice': precio_sl, 'reduceOnly': True}
                    )
                    print(f"🛡️ Stop Loss nativo activado en Binance en: {precio_sl}")
                except Exception as e:
                    print(f"⚠️ Error al crear Stop Loss en Binance: {e}")

                # 2. Trailing Stop Nativo de Binance
                try:
                    exchange.create_order(
                        SYMBOL, 'TRAILING_STOP_MARKET', 'buy', cantidad_btc, None,
                        {'callbackRate': CALLBACK_RATE, 'reduceOnly': True}
                    )
                    print(f"🎯 Trailing Stop nativo configurado con callbackRate: {CALLBACK_RATE}%")
                except Exception as e:
                    print(f"⚠️ Error al crear Trailing Stop nativo en Binance: {e}")

                registrar_en_csv("APERTURA", "SHORT", precio_actual, 0.0, 0.0, "ABIERTA", order.get('id', ''))
            else:
                print("⏳ Sin señales válidas en este ciclo.")
        
        else:
            side = posicion_activa['side']
            entry_price = float(posicion_activa['entryPrice'])
            print(f"🔒 Posición activa detectada ({side.upper()}). Entrada: {entry_price}")
            
            if side == 'long':
                profit_pct = ((precio_actual - entry_price) / entry_price) * 100
            else:
                profit_pct = ((entry_price - precio_actual) / entry_price) * 100
                
            print(f"📊 Beneficio flotante: {profit_pct:.2f}% (Protegido 24/7 por Trailing Stop de Binance)")
            registrar_en_csv("MONITOREO", side.upper(), entry_price, precio_actual, round(profit_pct, 2), "ACTIVA")

    except Exception as e:
        print(f"❌ Error en ejecución de Binance: {e}")

if __name__ == "__main__":
    ejecutar_bot()