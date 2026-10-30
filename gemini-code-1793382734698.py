import os
import time
import ccxt
import pandas as pd

# Cargar credenciales de forma segura desde las variables de entorno de Railway
API_KEY = os.getenv("BINANCE_API_KEY")
SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    raise ValueError("Error: No se encontraron las credenciales BINANCE_API_KEY o BINANCE_SECRET_KEY en las variables de entorno.")

# Inicializar el exchange de Binance Futures
exchange = ccxt.binance({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future',  # Configurado estrictamente para Futuros USDⓈ-M
    }
})

SYMBOL = 'BTC/USDT'
TIMEFRAME = '15m'

def run_bot():
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Conectando con Binance Futures...")
        
        # Cargar mercados y validar conexión
        exchange.load_markets()
        
        # Obtener balance de la cuenta para verificar acceso
        balance = exchange.fetch_balance()
        usdt_balance = balance['USDT']['free']
        print(f"Conexión exitosa. Saldo disponible en USDT: {usdt_balance}")

        # --- AQUÍ VA TU LÁGICA DE TRADING / ESTRATEGIA ---
        ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        print(f"Datos de {SYMBOL} obtenidos correctamente. Último precio de cierre: {df['close'].iloc[-1]}")

    except Exception as e:
        print(f"Error en la ejecución del bot: {e}")

if __name__ == "__main__":
    print("Iniciando Bot de Binance para Railway (Modo 24/7)...")
    while True:
        run_bot()
        print("Esperando 15 minutos para la siguiente ejecución...\n")
        time.sleep(900)
