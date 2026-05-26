from flask import Flask, jsonify, request
import os
from datetime import datetime

app = Flask(__name__)

# Credenciales desde variables de entorno
MT5_LOGIN = os.environ.get("MT5_LOGIN", "5049942150")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "@ilaKg1n")
MT5_SERVER = os.environ.get("MT5_SERVER", "MetaQuotes-Demo")

# Intentar importar MT5
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("⚠️ MetaTrader5 no disponible - usando modo simulado")

def init_mt5():
    """Inicializa conexión a MT5"""
    if not MT5_AVAILABLE:
        return False
    
    try:
        if not mt5.initialize():
            print(f"Error inicializando MT5: {mt5.last_error()}")
            return False
        
        if not mt5.login(int(MT5_LOGIN), MT5_PASSWORD, MT5_SERVER):
            print(f"Error login MT5: {mt5.last_error()}")
            return False
        
        print(f"✅ MT5 conectado: {MT5_LOGIN} @ {MT5_SERVER}")
        return True
    except Exception as e:
        print(f"Error MT5: {e}")
        return False

# Inicializar al arrancar
mt5_connected = init_mt5()

@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({
        "status": "ok",
        "mt5": "connected" if mt5_connected else "disconnected"
    })

@app.route('/api/tick/<symbol>', methods=['GET'])
def get_tick(symbol):
    """Obtiene tick actual"""
    if not MT5_AVAILABLE or not mt5_connected:
        return jsonify({
            "bid": 1.0850,
            "ask": 1.0852,
            "spread_pips": 2.0,
            "time": datetime.now().isoformat()
        }), 200
    
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return jsonify({"error": f"Símbolo {symbol} no encontrado"}), 404
        
        return jsonify({
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "spread_pips": round((tick.ask - tick.bid) / 0.0001, 1),
            "time": datetime.fromtimestamp(tick.time).isoformat()
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/account', methods=['GET'])
def get_account():
    """Obtiene info de cuenta"""
    if not MT5_AVAILABLE or not mt5_connected:
        return jsonify({
            "balance": 10000.0,
            "equity": 10000.0,
            "profit": 0.0,
            "margin_free": 10000.0,
            "leverage": 100,
            "currency": "USD",
            "server": MT5_SERVER,
            "name": "Demo Account"
        }), 200
    
    try:
        info = mt5.account_info()
        if info is None:
            return jsonify({"error": "No account info"}), 404
        
        return jsonify({
            "balance": float(info.balance),
            "equity": float(info.equity),
            "profit": float(info.profit),
            "margin_free": float(info.margin_free),
            "leverage": int(info.leverage),
            "currency": info.currency,
            "server": info.server,
            "name": info.name
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/candles/<symbol>/<timeframe>/<int:count>', methods=['GET'])
def get_candles(symbol, timeframe, count):
    """Obtiene velas históricas"""
    if not MT5_AVAILABLE or not mt5_connected:
        # Retornar datos simulados
        candles = []
        base_price = 1.0850
        for i in range(count):
            candles.append({
                "time": (datetime.now() - __import__('datetime').timedelta(hours=count-i)).isoformat(),
                "open": base_price + (i * 0.0001),
                "high": base_price + (i * 0.0001) + 0.0005,
                "low": base_price + (i * 0.0001) - 0.0005,
                "close": base_price + (i * 0.0001) + 0.0002,
                "volume": 1000 + (i * 10)
            })
        return jsonify({"candles": candles}), 200
    
    try:
        tf_map = {
            "1m": mt5.TIMEFRAME_M1,
            "5m": mt5.TIMEFRAME_M5,
            "15m": mt5.TIMEFRAME_M15,
            "1h": mt5.TIMEFRAME_H1,
            "4h": mt5.TIMEFRAME_H4,
            "1d": mt5.TIMEFRAME_D1,
        }
        
        tf = tf_map.get(timeframe, mt5.TIMEFRAME_H1)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        
        if rates is None or len(rates) == 0:
            return jsonify({"error": f"No data for {symbol}"}), 404
        
        candles = []
        for rate in rates:
            candles.append({
                "time": datetime.fromtimestamp(rate['time']).isoformat(),
                "open": float(rate['open']),
                "high": float(rate['high']),
                "low": float(rate['low']),
                "close": float(rate['close']),
                "volume": int(rate['tick_volume'])
            })
        
        return jsonify({"candles": candles}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/order', methods=['POST'])
def place_order():
    """Coloca una orden"""
    if not MT5_AVAILABLE or not mt5_connected:
        # Simular orden
        return jsonify({
            "order": 123456789,
            "retcode": 10009,
            "deal": 0,
            "volume": request.json.get("volume", 0.01),
            "price": request.json.get("price", 0),
            "comment": "SIMULATED"
        }), 200
    
    try:
        data = request.json
        symbol = data.get("symbol", "EURUSD")
        direction = data.get("direction", "LONG")
        volume = float(data.get("volume", 0.01))
        price = float(data.get("price", 0))
        sl = float(data.get("sl", 0))
        tp = float(data.get("tp", 0))
        
        order_type = mt5.ORDER_TYPE_BUY if direction == "LONG" else mt5.ORDER_TYPE_SELL
        
        request_obj = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "magic": 123456,
            "comment": "SMC Pro Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        
        result = mt5.order_send(request_obj)
        
        if result is None:
            return jsonify({"error": "Order send failed"}), 500
        
        return jsonify({
            "order": result.order,
            "retcode": result.retcode,
            "deal": result.deal,
            "volume": result.volume,
            "price": result.price,
            "comment": result.comment if hasattr(result, 'comment') else ""
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/positions', methods=['GET'])
def get_positions():
    """Obtiene posiciones abiertas"""
    if not MT5_AVAILABLE or not mt5_connected:
        return jsonify({"positions": []}), 200
    
    try:
        positions = mt5.positions_get()
        if positions is None:
            return jsonify({"positions": []})
        
        pos_list = []
        for p in positions:
            pos_list.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "BUY" if p.type == 0 else "SELL",
                "volume": float(p.volume),
                "price_open": float(p.price_open),
                "price_current": float(p.price_current),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "profit": float(p.profit),
                "time": datetime.fromtimestamp(p.time).isoformat()
            })
        
        return jsonify({"positions": pos_list}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)

