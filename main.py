import os
import time
import threading
import ccxt
import pandas as pd
from flask import Flask

# Servidor Flask para cumplir con el requisito del Web Service gratuito de Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot de Binance Futures Activo 24/7 🚀"

@app.route('/health')
def health():
    return "OK", 200

def run_bot():
    API_KEY = os.getenv("BINANCE_API_KEY")
    SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

    if not API_KEY or not SECRET_KEY:
        print("Error: No se encontraron las credenciales BINANCE_API_KEY o BINANCE_SECRET_KEY en las variables de entorno.")
        return

    # Inicializar el exchange de Binance Futures
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

    while True:
        try:
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Conectando con Binance Futures...")
            exchange.load_markets()
            
            balance = exchange.fetch_balance()
            usdt_balance = balance['USDT']['free']
            print(f"Conexión exitosa. Saldo disponible en USDT: {usdt_balance}")

            # Lectura de velas OHLCV para la estrategia
            ohlcv = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            print(f"Datos de {SYMBOL} obtenidos correctamente. Último precio de cierre: {df['close'].iloc[-1]}")

        except Exception as e:
            print(f"Error en la ejecución del bot: {e}")

        print("Esperando 15 minutos para la siguiente ejecución...\n")
        time.sleep(900)

if __name__ == "__main__":
    # Iniciar el bot de trading en un hilo en segundo plano
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()

    # Iniciar el servidor web de Flask en el puerto que asigna Render automáticamente
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
