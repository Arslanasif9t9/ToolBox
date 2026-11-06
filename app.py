"""Arslan Toolbox - local web app. Each tool lives in tools/<name>.py and exposes a Flask Blueprint."""
import importlib
import pkgutil
import webbrowser
from threading import Timer

from flask import Flask, render_template

import tools

app = Flask(__name__)

TOOLS = []  # filled automatically from tools/ package

for m in pkgutil.iter_modules(tools.__path__):
    mod = importlib.import_module(f"tools.{m.name}")
    if hasattr(mod, "bp") and hasattr(mod, "META"):
        app.register_blueprint(mod.bp)
        TOOLS.append(mod.META)

TOOLS.sort(key=lambda t: t["order"])


@app.context_processor
def inject_tools():
    return {"tools": TOOLS}


@app.route("/")
def home():
    return render_template("home.html")


if __name__ == "__main__":
    Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
