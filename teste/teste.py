from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "API Sicoob rodando no Render", 200

