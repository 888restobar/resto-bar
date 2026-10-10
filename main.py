import os
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# Kukunin nito ang database URL mula sa Vercel Environment Variables o kaya ay local fallback
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Halimbawa ng Database Model (Table para sa Menu o Orders)
class Menu(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)

# I-create ang tables kung wala pa (para sa first run)
with app.app_context():
    db.create_all()

@app.route('/')
def index():
    # Kunin lahat ng menu items mula sa database
    items = Menu.query.all()
    return f"Total menu items in DB: {len(items)}"

if __name__ == '__main__':
    app.run(debug=True)
