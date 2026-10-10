
from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "Flask Python is working!"

@app.route("/time")
def get_time():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Manila"))

    return jsonify({
        "status": "success",
        "datetime": now.isoformat(),
        "timezone": "Asia/Manila"
    })

if __name__ == "__main__":
    app.run(debug=True)
