import os
import requests
import re
import numpy as np
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

# Force load environment variables so the API keys never get missed
load_dotenv()

# External APIs
from newsapi import NewsApiClient
from google import genai
from atproto import Client as BskyClient

# Import existing blueprints
from hansard import hansard_bp
from biography import biography_bp
from tracker import tracker_bp
from debate_scanner import debate_scanner_bp
from mp_search import mp_search_bp

basedir = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)

# ==========================================
# 1. CONFIGURATION
# ==========================================
app.config['SECRET_KEY'] = 'super-secret-key-change-this-later' 
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(basedir, 'intelligence.db'))
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ==========================================
# 2. LOGIN MANAGER
# ==========================================
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' 

# ==========================================
# 3. DATABASE MODELS
# ==========================================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    topics = db.relationship('TrackedTopic', backref='owner', lazy=True)
    stakeholders = db.relationship('TrackedStakeholder', backref='owner', lazy=True)

class TrackedTopic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    keyword = db.Column(db.String(255), nullable=False)
    department = db.Column(db.String(100), nullable=False) 
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    alerts = db.relationship('Alert', backref='topic', lazy=True)

class TrackedStakeholder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    bsky_handle = db.Column(db.String(100))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    alerts = db.relationship('Alert', backref='stakeholder', lazy=True)

class Alert(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey('tracked_topic.id'), nullable=True)
    stakeholder_id = db.Column(db.Integer, db.ForeignKey('tracked_stakeholder.id'), nullable=True)
    source = db.Column(db.String(50), default='Hansard') 
    title = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(255), nullable=False)
    snippet = db.Column(db.Text, nullable=False)
    speaker = db.Column(db.String(100), nullable=True) 
    date_found = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ==========================================
# 4. AUTO-BUILD DATABASE
# ==========================================
with app.app_context():
    db.create_all()
    if not User.query.filter_by(email='joe@university.ac.uk').first():
        joe_pass = generate_password_hash('password123', method='pbkdf2:sha256')
        joe = User(email='joe@university.ac.uk', password_hash=joe_pass)
        db.session.add(joe)
        db.session.commit()

# ==========================================
# 5. ROUTES
# ==========================================
@app.route('/home')
def home():
    return render_template('home.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('my_alerts'))
        flash('Invalid email or password')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/my_alerts')
@login_required
def my_alerts():
    user_topics = TrackedTopic.query.filter_by(user_id=current_user.id).all()
    user_stakeholders = TrackedStakeholder.query.filter_by(user_id=current_user.id).all()
    return render_template('my_alerts.html', topics=user_topics, stakeholders=user_stakeholders)

@app.route('/add_topic', methods=['POST'])
@login_required
def add_topic():
    keyword = request.form.get('keyword')
    dept = request.form.get('department')
    if keyword:
        db.session.add(TrackedTopic(keyword=keyword, department=dept, user_id=current_user.id))
        db.session.commit()
    return redirect(url_for('my_alerts'))

@app.route('/remove_topic/<int:topic_id>', methods=['POST'])
@login_required
def remove_topic(topic_id):
    topic = TrackedTopic.query.filter_by(id=topic_id, user_id=current_user.id).first()
    if topic:
        Alert.query.filter_by(topic_id=topic.id).delete()
        db.session.delete(topic)
        db.session.commit()
    return redirect(url_for('my_alerts'))

@app.route('/add_stakeholder', methods=['POST'])
@login_required
def add_stakeholder():
    name = request.form.get('name')
    handle = request.form.get('bsky_handle')
    if name:
        db.session.add(TrackedStakeholder(name=name, bsky_handle=handle, user_id=current_user.id))
        db.session.commit()
    return redirect(url_for('my_alerts'))

# Helper for Semantic Scoring (Cosine Similarity)
def get_similarity(vec1, vec2):
    return np.dot(vec1, vec2) / (np.linalg.norm(vec1) * np.linalg.norm(vec2))

# ==========================================
# 6. SMART AI SCANNER (WITH NEW GEMINI MODEL)
# ==========================================
@app.route('/run_manual_scan', methods=['POST'])
@login_required
def run_manual_scan():
    TWFY_API_KEY = os.environ.get("TWFY_API_KEY")
    NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    BSKY_HANDLE = os.environ.get("BSKY_HANDLE")
    BSKY_PASSWORD = os.environ.get("BSKY_PASSWORD")
    
    newsapi = NewsApiClient(api_key=NEWS_API_KEY) if NEWS_API_KEY else None
    
    ai_client = None
    if GEMINI_API_KEY:
        try:
            ai_client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            print(f"Failed to initialize Gemini: {e}")
    
    new_count = 0
    three_days_ago = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')

    user_topics = TrackedTopic.query.filter_by(user_id=current_user.id).all()
    
    for topic in user_topics:
        target_emb = None
        if ai_client:
            try:
                # 🎯 THE FIX: Upgraded to gemini-embedding-001
                target_emb = ai_client.models.embed_content(
                    model='gemini-embedding-001', 
                    contents=topic.keyword
                ).embeddings[0].values
            except Exception as e: 
                print(f"Gemini Target Embedding Error: {e}")
                
        if not target_emb:
            flash(f"⚠️ Warning: AI Filtering unavailable for '{topic.keyword}'. Check GEMINI_API_KEY.")

        broad_search = topic.department if topic.department and topic.department != "All Departments" else "Higher Education"

        # --- 1. MEDIA SCAN ---
        if newsapi:
            try:
                all_articles = newsapi.get_everything(q=broad_search, from_param=three_days_ago, language='en', sort_by='relevancy', page_size=50)
                for art in all_articles.get('articles', []):
                    if not Alert.query.filter_by(url=art['url']).first():
                        snippet = art['description'] if art['description'] else art['title']
                        is_relevant = False 
                        
                        if ai_client and target_emb and snippet:
                            try:
                                # 🎯 THE FIX: Upgraded to gemini-embedding-001
                                art_emb = ai_client.models.embed_content(model='gemini-embedding-001', contents=snippet).embeddings[0].values
                                score = get_similarity(target_emb, art_emb)
                                
                                if score >= 0.62:
                                    is_relevant = True
                            except: pass

                        if is_relevant:
                            db.session.add(Alert(topic_id=topic.id, source='Media', title=art['title'], url=art['url'], snippet=snippet[:200], speaker=art['source']['name']))
                            new_count += 1
            except Exception as e: print(f"News API Error: {e}")

        # --- 2. HANSARD SCAN ---
        if TWFY_API_KEY:
            try:
                resp = requests.get(f"https://www.theyworkforyou.com/api/getDebates?key={TWFY_API_KEY}&search={broad_search}&output=json&num=15", timeout=5)
                if resp.status_code == 200:
                    for row in resp.json().get('rows', []):
                        url = "https://www.theyworkforyou.com" + row.get('listurl', '')
                        if not Alert.query.filter_by(url=url).first():
                            snippet = re.sub('<[^>]+>', '', row.get('body', ''))[:300]
                            is_relevant = False
                            
                            if ai_client and target_emb and snippet:
                                try:
                                    # 🎯 THE FIX: Upgraded to gemini-embedding-001
                                    art_emb = ai_client.models.embed_content(model='gemini-embedding-001', contents=snippet).embeddings[0].values
                                    if get_similarity(target_emb, art_emb) >= 0.62:
                                        is_relevant = True
                                except: pass
                                
                            if is_relevant:
                                db.session.add(Alert(topic_id=topic.id, source='Hansard', title=row.get('parent', {}).get('body', 'Parliamentary Debate'), url=url, snippet=snippet, speaker=row.get('speaker', {}).get('name')))
                                new_count += 1
            except: pass

    # --- 3. BLUESKY SCAN ---
    if BSKY_HANDLE and BSKY_PASSWORD:
        try:
            bsky = BskyClient()
            bsky.login(BSKY_HANDLE, BSKY_PASSWORD)
            user_stakeholders = TrackedStakeholder.query.filter_by(user_id=current_user.id).all()
            for sh in user_stakeholders:
                if sh.bsky_handle:
                    handle = sh.bsky_handle.replace('@', '')
                    feed = bsky.get_author_feed(actor=handle, limit=5)
                    for feed_view in feed.feed:
                        post = feed_view.post
                        post_url = f"https://bsky.app/profile/{handle}/post/{post.uri.split('/')[-1]}"
                        if not Alert.query.filter_by(url=post_url).first():
                            db.session.add(Alert(stakeholder_id=sh.id, source='Bluesky', title=f"New Statement from {sh.name}", url=post_url, snippet=post.record.text[:250], speaker=handle))
                            new_count += 1
        except Exception as e: print(f"Bluesky Error: {e}")

    db.session.commit()
    flash(f"Smart AI Scan Complete! Discovered {new_count} highly relevant updates.")
    return redirect(url_for('my_alerts'))

# ==========================================
# 7. BLUEPRINTS
# ==========================================
app.register_blueprint(hansard_bp)
app.register_blueprint(biography_bp)
app.register_blueprint(tracker_bp)
app.register_blueprint(debate_scanner_bp)
app.register_blueprint(mp_search_bp)

if __name__ == '__main__':
    app.run(debug=True)