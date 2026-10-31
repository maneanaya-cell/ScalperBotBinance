import os
import time
import threading
import ccxt
import pandas as pd
import numpy as np
from flask import Flask

# Servidor Flask para mantener el Web Service activo 24/7 en Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot de Binance Futures con Trailing Stop y Extremos Activo 24/7 🚀"

@app.route('/health')
def health():
    return "OK", 200

# --- FUNCIONES DE INDICADORES TÉCNICOS ---
def calculate_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=period).mean()

def calculate_channels(df, period=20):
    df['channel_high'] = df['high'].rolling(window=period).max()
    df['channel_low'] = df['low'].rolling(window=period).min()
    return df

def check_rsi_divergence(df):
    if len(df) < 20:
        return None
    
    sub_df = df.iloc[-15:].copy()
    
    price_min1, price_min2 = sub_df['close'].iloc[-10], sub_df['close'].iloc[-1]
    rsi_min1, rsi_min2 = sub_df['rsi'].iloc[-10], sub_df['rsi'].iloc[-1]
    
    price_max1, price_max2 = sub_df['close'].iloc[-10], sub_df['close'].iloc[-1]
    rsi_max1, rsi_max2 = sub_df['rsi'].iloc[-10], sub_df['rsi'].iloc[-1]

    # Divergencia Alcista
    if price_min2 < price_min1 and rsi_min2 > rsi_min1 and sub_df['rsi'].iloc[-1] < 45:
        return 'BUY'
    
    # Divergencia Bajista
    if price_max2 > price_max1 and rsi_max2 < rsi_max1 and sub_df['rsi'].iloc[-1] > 55:
        return 'SELL'
        
    return None

def run_bot():
    API_KEY = os.getenv("BINANCE_API_KEY")
    SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

    if not API_KEY or not SECRET_KEY:
        print("Error: No se encontraron las credenciales BINANCE_API_KEY o SECRET_KEY.")
        return

    exchange = ccxt.binance({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
        }
    })

    SYMBOL = 'BTC/USDT'
    TIMEFRAME = '15m'
    LEVERAGE = 10

    # Variables de control para el Trailing Stop en memoria
    position_state = {
        'active': False,
        'side': None,       # 'BUY' o 'SELL'
        'extreme_price': 0  # Máximo alcanzado en Long o Mínimo alcanzado en Short
    }

    try:
        exchange.load_markets()
        exchange.set_leverage(LEVERAGE, SYMBOL)
        print(f"Apalancamiento configurado a {LEVERAGE}x para {SYMBOL}")
    except Exception as e:
        print(f"Nota al configurar apalancamiento: {e}")

    while True:
        try:
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Analizando mercado para {SYMBOL}...")
            
            balance = exchange.fetch_balance()
            usdt_balance = balance['USDT']['free']
            print(f"Saldo disponible USDT: {usdt_balance}")

            ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            # Indicadores
            df['ema_200'] = calculate_ema(df['close'], 200)
            df['rsi'] = calculate_rsi(df['close'], 14)
            df['atr'] = calculate_atr(df, 14)
            df = calculate_channels(df, period=20)

            last_close = df['close'].iloc[-1]
            last_rsi = df['rsi'].iloc[-1]
            last_atr = df['atr'].iloc[-1]
            ema_200 = df['ema_200'].iloc[-1]
            ch_high = df['channel_high'].iloc[-1]
            ch_low = df['channel_low'].iloc[-1]

            ch_range = ch_high - ch_low
            channel_position = (last_close - ch_low) / ch_range if ch_range > 0 else 0.5

            print(f"Precio: {last_close} | RSI: {last_rsi:.2f} | ATR: {last_atr:.2f} | Canal Pos: {channel_position:.2f}")

            # Verificar estado real de posiciones en Binance
            positions = exchange.fetch_positions([SYMBOL])
            has_open_position = any(float(p['contracts']) > 0 for p in positions if p['symbol'] == SYMBOL)

            if has_open_position:
                # --- GESTIÓN DE TRAILING STOP PARA POSICIÓN ABIERTA ---
                print("Posición activa detectada. Evaluando Trailing Stop...")
                
                # Identificar el lado de la posición abierta en Binance
                current_pos = next(p for p in positions if p['symbol'] == SYMBOL and float(p['contracts']) > 0)
                pos_side = current_pos['side']  # 'long' o 'short'
                contracts_amount = float(current_pos['contracts'])

                if pos_side == 'long':
                    if not position_state['active'] or position_state['side'] != 'BUY':
                        position_state['active'] = True
                        position_state['side'] = 'BUY'
                        position_state['extreme_price'] = last_close

                    # Actualizar el precio máximo alcanzado
                    if last_close > position_state['extreme_price']:
                        position_state['extreme_price'] = last_close

                    # Calcular nivel de Stop Dinámico (ej: 2 veces el ATR por debajo del máximo)
                    trailing_stop_price = position_state['extreme_price'] - (2 * last_atr)
                    print(f"Trailing Stop Long activo -> Máximo: {position_state['extreme_price']} | Stop Loss dinámico: {trailing_stop_price:.2f}")

                    # Si el precio toca o cae por debajo del trailing stop, cerramos
                    if last_close <= trailing_stop_price:
                        print("¡Precio alcanzó el Trailing Stop! Cerrando posición LONG...")
                        exchange.create_market_sell_order(SYMBOL, contracts_amount, params={'reduceOnly': True})
                        position_state['active'] = False

                elif pos_side == 'short':
                    if not position_state['active'] or position_state['side'] != 'SELL':
                        position_state['active'] = True
                        position_state['side'] = 'SELL'
                        position_state['extreme_price'] = last_close

                    # Actualizar el precio mínimo alcanzado
                    if last_close < position_state['extreme_price']:
                        position_state['extreme_price'] = last_close

                    # Calcular nivel de Stop Dinámico para Short (ej: 2 veces el ATR por encima del mínimo)
                    trailing_stop_price = position_state['extreme_price'] + (2 * last_atr)
                    print(f"Trailing Stop Short activo -> Mínimo: {position_state['extreme_price']} | Stop Loss dinámico: {trailing_stop_price:.2f}")

                    # Si el precio sube y toca el trailing stop, cerramos
                    if last_close >= trailing_stop_price:
                        print("¡Precio alcanzó el Trailing Stop! Cerrando posición SHORT...")
                        exchange.create_market_buy_order(SYMBOL, contracts_amount, params={'reduceOnly': True})
                        position_state['active'] = False

            else:
                # Si no hay posición abierta, reiniciamos el estado del trailing
                position_state['active'] = False
                
                # --- EVALUAR NUEVAS ENTRADAS (FILTRO DE EXTREMOS + DIVERGENCIA) ---
                signal = check_rsi_divergence(df)

                if channel_position < 0.2:
                    print("Precio en EXTREMO INFERIOR (Soporte).")
                    if signal == 'BUY' and last_close > ema_200:
                        print("¡Entrada BUY confirmada en soporte con divergencia alcista!")
                        amount = 0.001
                        exchange.create_market_buy_order(SYMBOL, amount)
                    else:
                        print("En soporte, pero sin señal clara de divergencia/tendencia.")

                elif channel_position > 0.8:
                    print("Precio en EXTREMO SUPERIOR (Resistencia).")
                    if signal == 'SELL' and last_close < ema_200:
                        print("¡Entrada SELL confirmada en resistencia con divergencia bajista!")
                        amount = 0.001
                        exchange.create_market_sell_order(SYMBOL, amount)
                    else:
                        print("En resistencia, pero sin señal clara de divergencia/tendencia.")
                else:
                    print("Precio en el CENTRO del canal. Operación descartada por seguridad.")

        except Exception as e:
            print(f"Error en la ejecución de la estrategia: {e}")

        print("Esperando 15 minutos para el siguiente ciclo...\n")
        time.sleep(900)

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
