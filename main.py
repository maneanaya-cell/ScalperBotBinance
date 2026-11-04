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
    return "Bot de Binance Futures (BTC/USDC) con Trailing Ajustado Activo 24/7 🚀"

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

def calculate_channels(df, period=134):
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
        print("Error: No se encontraron las credenciales BINANCE_API_KEY o SECRET_KEY.", flush=True)
        return

    exchange = ccxt.binance({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'settle': 'USDC' 
        }
    })

    SYMBOL = 'BTC/USDC'
    TIMEFRAME = '15m'
    LEVERAGE = 10
    MIN_NOTIONAL_USDC = 8.0  # Margen seguro por encima del mínimo de 7.5 USDC de Binance

    # Variables de control para el Trailing Stop en memoria
    position_state = {
        'active': False,
        'side': None,            # 'BUY' o 'SELL'
        'entry_price': 0,        # Precio de entrada
        'extreme_price': 0,      # Máximo/Mínimo favorable alcanzado
        'trailing_active': False,# Se vuelve True al alcanzar 0.3% de ganancia
        'current_stop': 0        # Nivel actual del Stop Loss dinámico
    }

    try:
        exchange.load_markets()
        exchange.set_leverage(LEVERAGE, SYMBOL)
        print(f"Apalancamiento configurado a {LEVERAGE}x para {SYMBOL}", flush=True)
    except Exception as e:
        print(f"Nota al configurar apalancamiento: {e}", flush=True)

    while True:
        try:
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Analizando mercado para {SYMBOL}...", flush=True)
            
            balance = exchange.fetch_balance()
            usdc_balance = balance['USDC']['free'] if 'USDC' in balance else 0
            print(f"Saldo disponible USDC: {usdc_balance}", flush=True)

            ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=300)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            # Indicadores
            df['ema_200'] = calculate_ema(df['close'], 200)
            df['rsi'] = calculate_rsi(df['close'], 14)
            df['atr'] = calculate_atr(df, 14)
            df = calculate_channels(df, period=134)

            last_close = df['close'].iloc[-1]
            last_rsi = df['rsi'].iloc[-1]
            last_atr = df['atr'].iloc[-1]
            ema_200 = df['ema_200'].iloc[-1]
            ch_high = df['channel_high'].iloc[-1]
            ch_low = df['channel_low'].iloc[-1]

            ch_range = ch_high - ch_low
            channel_position = (last_close - ch_low) / ch_range if ch_range > 0 else 0.5

            print(f"Precio: {last_close} | RSI: {last_rsi:.2f} | ATR: {last_atr:.2f} | Canal Pos: {channel_position:.2f}", flush=True)

            # Verificar estado real de posiciones en Binance
            positions = exchange.fetch_positions([SYMBOL])
            has_open_position = any(float(p['contracts']) > 0 for p in positions if p['symbol'] == SYMBOL)

            if has_open_position:
                # --- GESTIÓN DE TRAILING STOP (Gatillo 0.3% -> Fija 0.2% -> Distancia 1.5*ATR) ---
                print("Posición activa detectada. Evaluando Trailing Stop...", flush=True)
                
                current_pos = next(p for p in positions if p['symbol'] == SYMBOL and float(p['contracts']) > 0)
                pos_side = current_pos['side']  # 'long' o 'short'
                contracts_amount = float(current_pos['contracts'])
                entry_price = float(current_pos.get('entryPrice', last_close))

                if pos_side == 'long':
                    if not position_state['active'] or position_state['side'] != 'BUY':
                        position_state['active'] = True
                        position_state['side'] = 'BUY'
                        position_state['entry_price'] = entry_price
                        position_state['extreme_price'] = last_close
                        position_state['trailing_active'] = False
                        position_state['current_stop'] = 0

                    # Actualizar precio máximo favorable
                    if last_close > position_state['extreme_price']:
                        position_state['extreme_price'] = last_close

                    # Calcular beneficio porcentual actual
                    profit_pct = (last_close - position_state['entry_price']) / position_state['entry_price']

                    # 1. Si alcanza el 0.3% de ganancia, activamos el trailing y fijamos el stop en el 0.2% de ganancia
                    if not position_state['trailing_active'] and profit_pct >= 0.003:
                        position_state['trailing_active'] = True
                        # Precio exacto que representa un 0.2% de ganancia sobre la entrada
                        position_state['current_stop'] = position_state['entry_price'] * 1.002
                        print(f"¡Ganancia del 0.3% alcanzada! Trailing activado. Stop inicial asegurado en 0.2% de ganancia: {position_state['current_stop']:.2f}", flush=True)

                    if position_state['trailing_active']:
                        # 2. Calcular la distancia dinámica a 1.5 * ATR desde el precio extremo alcanzado
                        atr_based_stop = position_state['extreme_price'] - (1.5 * last_atr)
                        
                        # El stop nunca retrocede: toma el valor más alto entre el stop asegurado (0.2%) y el avance del ATR
                        if atr_based_stop > position_state['current_stop']:
                            position_state['current_stop'] = atr_based_stop

                        print(f"Trailing Long -> Máximo: {position_state['extreme_price']} | Stop Dinámico: {position_state['current_stop']:.2f}", flush=True)

                        # Si el precio toca o cae por debajo del stop dinámico, cerramos
                        if last_close <= position_state['current_stop']:
                            print("¡Precio tocó el Trailing Stop! Cerrando posición LONG...", flush=True)
                            exchange.create_market_sell_order(SYMBOL, contracts_amount, params={'reduceOnly': True})
                            position_state['active'] = False
                    else:
                        print(f"Ganancia actual: {profit_pct*100:.3f}%. Esperando llegar a 0.3% para activar trailing.", flush=True)

                elif pos_side == 'short':
                    if not position_state['active'] or position_state['side'] != 'SELL':
                        position_state['active'] = True
                        position_state['side'] = 'SELL'
                        position_state['entry_price'] = entry_price
                        position_state['extreme_price'] = last_close
                        position_state['trailing_active'] = False
                        position_state['current_stop'] = 0

                    # Actualizar precio mínimo favorable
                    if last_close < position_state['extreme_price']:
                        position_state['extreme_price'] = last_close

                    # Calcular beneficio porcentual para short
                    profit_pct = (position_state['entry_price'] - last_close) / position_state['entry_price']

                    # 1. Activación al 0.3% de ganancia -> Stop asegurado en 0.2% de ganancia bajista
                    if not position_state['trailing_active'] and profit_pct >= 0.003:
                        position_state['trailing_active'] = True
                        position_state['current_stop'] = position_state['entry_price'] * 0.998
                        print(f"¡Ganancia del 0.3% alcanzada! Trailing activado. Stop inicial asegurado en 0.2% de ganancia: {position_state['current_stop']:.2f}", flush=True)

                    if position_state['trailing_active']:
                        # 2. Distancia a 1.5 * ATR para Short
                        atr_based_stop = position_state['extreme_price'] + (1.5 * last_atr)
                        
                        # El stop para short baja (se vuelve más restrictivo) sin retroceder
                        if atr_based_stop < position_state['current_stop']:
                            position_state['current_stop'] = atr_based_stop

                        print(f"Trailing Short -> Mínimo: {position_state['extreme_price']} | Stop Dinámico: {position_state['current_stop']:.2f}", flush=True)

                        if last_close >= position_state['current_stop']:
                            print("¡Precio tocó el Trailing Stop! Cerrando posición SHORT...", flush=True)
                            exchange.create_market_buy_order(SYMBOL, contracts_amount, params={'reduceOnly': True})
                            position_state['active'] = False
                    else:
                        print(f"Ganancia actual: {profit_pct*100:.3f}%. Esperando llegar a 0.3% para activar trailing.", flush=True)

            else:
                position_state['active'] = False
                position_state['trailing_active'] = False
                
                # --- EVALUAR NUEVAS ENTRADAS (EXTREMOS DEL CANAL + 7.5 USDC MÍNIMO) ---
                signal = check_rsi_divergence(df)

                notional_target = MIN_NOTIONAL_USDC
                calculated_amount = round(notional_target / last_close, 4)

                if channel_position < 0.2:
                    print("Precio en EXTREMO INFERIOR (Soporte).", flush=True)
                    if signal == 'BUY' and last_close > ema_200:
                        print(f"¡Entrada BUY confirmada! Ejecutando con tamaño: {calculated_amount} BTC", flush=True)
                        exchange.create_market_buy_order(SYMBOL, calculated_amount)
                    else:
                        print("En soporte, pero sin señal clara de divergencia/tendencia.", flush=True)

                elif channel_position > 0.8:
                    print("Precio en EXTREMO RESISTENCIA.", flush=True)
                    if signal == 'SELL' and last_close < ema_200:
                        print(f"¡Entrada SELL confirmada! Ejecutando con tamaño: {calculated_amount} BTC", flush=True)
                        exchange.create_market_sell_order(SYMBOL, calculated_amount)
                    else:
                        print("En resistencia, pero sin señal clara de divergencia/tendencia.", flush=True)
                else:
                    print("Precio en el CENTRO del canal. Operación descartada por seguridad.", flush=True)

        except Exception as e:
            print(f"Error en la ejecución de la estrategia: {e}", flush=True)

        print("Esperando 15 minutos para el siguiente ciclo...\n", flush=True)
        time.sleep(900)

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
