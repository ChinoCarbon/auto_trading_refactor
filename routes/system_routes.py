from flask import Blueprint, jsonify
import time

system_bp = Blueprint("system", __name__)

@system_bp.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": int(time.time() * 1000)
    })
