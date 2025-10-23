from flask import Blueprint, jsonify
import json, os

users_bp = Blueprint("users", __name__)
DATA_PATH = os.path.join("data", "api_keys.json")

@users_bp.route("", methods=["GET"])
def get_users():
    if not os.path.exists(DATA_PATH):
        return jsonify({"success": False, "error": "api_keys.json not found"}), 404
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    # 期望结构 { "api_keys": [ {...}, {...} ] }
    keys = config.get("api_keys", [])
    users_list = []
    for u in keys:
        users_list.append({
            "id": u.get("id"),
            "alias": u.get("alias"),
            "use_testnet": u.get("use_testnet", False),
            "is_active": u.get("is_active", True),
            "created_at": u.get("created_at", None)
        })
    return jsonify({
        "success": True,
        "data": {
            "users": users_list,
            "total_users": len(users_list)
        }
    })
