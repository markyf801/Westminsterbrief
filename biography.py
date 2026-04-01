import os, requests, io, docx, wikipedia, time
from flask import Blueprint, render_template, request, jsonify, make_response
from datetime import datetime
from docx import Document

biography_bp = Blueprint('biography', __name__)

def get_verified_model():
    api_key = os.environ.get("GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            for pref in ['gemini-1.5-flash', 'gemini-pro']:
                for m in resp.json().get('models', []):
                    if pref in m['name']: return m['name']
    except: pass
    return "models/gemini-1.5-flash"

def generate_mp_biography(mp_name, member_id):
    api_key = os.environ.get("GEMINI_API_KEY")
    try:
        search = wikipedia.search(f"{mp_name} UK politician")
        wiki_text = wikipedia.page(search[0], auto_suggest=False).content[:5000] if search else ""
    except: wiki_text = ""
    prompt = f"Summarize political interests and career for {mp_name} using: {wiki_text}"
    url = f"https://generativelanguage.googleapis.com/v1beta/{get_verified_model()}:generateContent?key={api_key}"
    try:
        resp = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=30)
        return resp.json()['candidates'][0]['content']['parts'][0]['text']
    except: return "AI Summary unavailable."

@biography_bp.route('/biography', methods=['GET', 'POST'])
def biography_home():
    mp_data, error = None, None
    if request.method == 'POST':
        member_id = request.form.get('member_id')
        if member_id:
            try:
                resp = requests.get(f"https://members-api.parliament.uk/api/Members/{member_id}").json().get('value')
                mp_data = {'id': resp.get('id'), 'name': resp.get('nameDisplayAs'), 'party': resp.get('latestParty', {}).get('name'), 'constituency': resp.get('latestHouseMembership', {}).get('membershipFrom'), 'image_url': resp.get('thumbnailUrl')}
            except: error = "Could not load member."
    return render_template('biography.html', mp_data=mp_data, error_message=error)

@biography_bp.route('/api/search_members')
def api_search_members():
    term = request.args.get('q', '')
    if len(term) < 3: return jsonify({"results": []})
    try:
        items = requests.get(f"https://members-api.parliament.uk/api/Members/Search?Name={term}&take=15").json().get('items') or []
        return jsonify({"results": [{"id": i['value']['id'], "text": i['value']['nameFullTitle']} for i in items]})
    except: return jsonify({"results": []})

@biography_bp.route("/api/biography", methods=["POST"])
def api_biography():
    data = request.get_json()
    return jsonify({"biography": generate_mp_biography(data.get("mp_name"), data.get("member_id"))})