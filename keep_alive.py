import hmac
import os
from threading import Thread

from flask import Flask, jsonify, request

app = Flask("keep_alive")

KEEP_ALIVE_SECRET = os.getenv("KEEP_ALIVE_SECRET")


@app.before_request
def _check_auth():
    if not KEEP_ALIVE_SECRET:
        return None  # auth disabled — backwards-compatible default
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return jsonify({"error": "Unauthorized"}), 401
    token = header[len("Bearer "):]
    if not hmac.compare_digest(token, KEEP_ALIVE_SECRET):
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.route("/")
def home():
    return "Bot is alive."


def _run():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    Thread(target=_run, daemon=True).start()
