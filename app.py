from flask import Flask
from flask_cors import CORS
from routes.orders_routes import orders_bp
from routes.positions_routes import positions_bp
from routes.leverage_routes import leverage_bp
from routes.symbol_routes import symbol_bp
from routes.system_routes import system_bp
from routes.users_routes import users_bp

def create_app():
    app = Flask(__name__)

    # ① 启用跨域支持
    # allow all origins for now; you can restrict later
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # ② 注册蓝图
    app.register_blueprint(orders_bp, url_prefix="/api/orders")
    app.register_blueprint(positions_bp, url_prefix="/api/positions")
    app.register_blueprint(leverage_bp, url_prefix="/api/leverage")
    app.register_blueprint(symbol_bp, url_prefix="/api/symbols")
    app.register_blueprint(system_bp, url_prefix="/api")
    app.register_blueprint(users_bp, url_prefix="/api/users")

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)
