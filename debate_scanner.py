import requests, os, json, re, io
from flask import Blueprint, render_template, request, send_file
from datetime import datetime
from cache_models import CachedTranscript

# Import Word Document libraries
try:
    import docx
    from docx import Document
    from docx.shared import Pt, RGBColor
except ImportError:
    Document = None

debate_scanner_bp = Blueprint('debates', __name__)

TWFY_API_KEY = os.environ.get("TWFY_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TWFY_API_URL = "https://www.theyworkforyou.com/api/getDebates"

DEPARTMENTS_TWFY = [
    "All Departments", "Department for Education", "Department of Health and Social Care",
    "HM Treasury", "Home Office", "Ministry of Defence", "Ministry of Justice",
    "Department for Science, Innovation and Technology", "Cabinet Office"
]

DEPT_KEYWORDS = {
    "Department for Education": '("Education" OR "Schools" OR "Skills" OR "Childcare" OR "Universities" OR "SEND")',
    "Department of Health and Social Care": '("Health" OR "NHS" OR "Social Care")',
    "HM Treasury": '("Treasury" OR "Tax" OR "Economy" OR "Spending")',
    "Home Office": '("Home Office" OR "Police" OR "Immigration")',
    "Ministry of Defence": '("Defence" OR "Military" OR "Armed Forces")',
    "Ministry of Justice": '("Justice" OR "Prisons" OR "Courts")',
    "Department for Science, Innovation and Technology": '("Science" OR "Technology" OR "Innovation")',
    "Cabinet Office": '("Cabinet Office" OR "Civil Service")'
}

def get_working_model(api_key):
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        resp = requests.get(url, timeout=5)
        if resp.status_code == 200:
            available = [m['name'] for m in resp.json().get('models', []) 
                         if 'generateContent' in m.get('supportedGenerationMethods', [])]
            for pref in ['models/gemini-1.5-flash', 'models/gemini-1.5-pro']:
                if pref in available: return pref
            if available: return available[0]
    except: pass
    return "models/gemini-pro"

def get_twfy_date_range(start_str, end_str):
    def parse_date(d_str):
        if not d_str: return ""
        if re.match(r'^\d{4}-\d{2}-\d{2}$', d_str): return d_str.replace('-', '')
        if '/' in d_str or '-' in d_str:
            parts = d_str.replace('/', '-').split('-')
            if len(parts) == 3 and len(parts[2]) == 4:
                return f"{parts[2]}{parts[1].zfill(2)}{parts[0].zfill(2)}"
        return d_str.replace('-', '').replace('/', '')

    s = parse_date(start_str)
    e = parse_date(end_str)

    if s and e: return f"{s}..{e}"
    elif s: return f"{s}..{datetime.now().strftime('%Y%m%d')}"
    elif e: return f"19000101..{e}"
    return ""

def get_debate_type(title):
    t = title.lower()
    if 'question' in t: return '🗣️ Oral Question'
    if 'statement' in t: return '📜 Ministerial Statement'
    if 'bill' in t or 'reading' in t or 'amendment' in t: return '⚖️ Legislation'
    if 'motion' in t: return '📝 Motion'
    if 'westminster hall' in t: return '🏛️ Westminster Hall'
    return '💬 General Debate'

# ==========================================
# ROUTE 1: SCAN AND GROUP BY THEME (STEP 1)
# ==========================================
@debate_scanner_bp.route('/debates', methods=['GET', 'POST'])
def scan_debates():
    grouped_debates = {}
    error_message = None
    
    start_date = ""
    end_date = ""
    selected_dept = "All Departments"
    selected_house = "all"
    content_type = "exclude_bills"

    if request.method == 'POST':
        action = request.form.get('action', 'search')
        
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        selected_dept = request.form.get('department', 'All Departments').strip()
        selected_house = request.form.get('house', 'all')
        content_type = request.form.get('content_type', 'exclude_bills')

        if action == 'search':
            try:
                date_query = get_twfy_date_range(start_date, end_date)
                
                search_term = DEPT_KEYWORDS.get(selected_dept, '("Oral questions" OR "Debate")')
                houses_to_search = ['commons', 'lords'] if selected_house == 'all' else [selected_house]
                all_rows = []

                for h in houses_to_search:
                    query = f"{search_term} {date_query}".strip()

                    params = {
                        'key': TWFY_API_KEY,
                        'search': query,
                        'type': h,
                        'output': 'json',
                        'num': 1000 
                    }
                    
                    resp = requests.get(TWFY_API_URL, params=params, timeout=15)
                    if resp.status_code == 200:
                        all_rows.extend(resp.json().get('rows', []))

                if all_rows:
                    unique_debates = {}
                    
                    for r in all_rows:
                        raw_title = r.get('parent', {}).get('body', 'General Debate/Question')
                        title = re.sub(r'<[^>]+>', '', raw_title)
                        date_val = r.get('hdate', '')
                        
                        url = "https://www.theyworkforyou.com" + r.get('listurl', '')
                        speaker = r.get('speaker', {}).get('name', 'Unknown')
                        
                        if start_date and date_val < start_date: continue
                        if end_date and date_val > end_date: continue
                        
                        is_bill_or_amendment = bool(re.search(r'\b(bill|amendment)\b', title, re.IGNORECASE))
                        if content_type == 'exclude_bills' and is_bill_or_amendment: continue
                        if content_type == 'only_bills' and not is_bill_or_amendment: continue

                        key = f"{title}_{date_val}"
                        if key not in unique_debates:
                            unique_debates[key] = {
                                'id': key,
                                'title': title,
                                'date': date_val,
                                'minister': speaker,
                                'contributions': 1,
                                'url': url,
                                'type': get_debate_type(title)
                            }
                        else:
                            unique_debates[key]['contributions'] += 1
                            
                    debate_list = list(unique_debates.values())
                    debate_list.sort(key=lambda x: x['date'], reverse=True)
                    
                    if debate_list and GEMINI_API_KEY:
                        try:
                            MAX_AI_ITEMS = 120
                            process_list = debate_list[:MAX_AI_ITEMS]
                            leftover_list = debate_list[MAX_AI_ITEMS:]

                            titles_only = [{"id": d['id'], "title": d['title'], "minister": d['minister']} for d in process_list]
                            
                            prompt = (
                                f"You are an expert UK political analyst. The user only wants debates related to the '{selected_dept}'. "
                                "1. Group the genuinely relevant debates into 3 to 5 clear policy themes. "
                                "2. Group irrelevant/false positive debates into 'Irrelevant (Other Departments)'. "
                                "Return ONLY a pure JSON object where the keys are the 'Theme Names' and the values are arrays of the 'id' strings."
                            )
                            
                            model_path = get_working_model(GEMINI_API_KEY)
                            ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={GEMINI_API_KEY}"
                            payload = {
                                "contents": [{"parts": [{"text": prompt + "\n\nData: " + json.dumps(titles_only)}]}],
                                "generationConfig": {"responseMimeType": "application/json"}
                            }
                            ai_resp = requests.post(ai_url, json=payload, timeout=60)
                            
                            if ai_resp.status_code == 200:
                                ai_text = ai_resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '{}')
                                clean_text = ai_text.replace('```json', '').replace('```', '').strip()
                                theme_mapping = json.loads(clean_text)

                                mapped_ids = set()
                                for theme, ids in theme_mapping.items():
                                    if "Irrelevant" in theme: continue
                                        
                                    theme_debates = [d for d in process_list if d['id'] in ids]
                                    if theme_debates:
                                        grouped_debates[theme] = theme_debates
                                        mapped_ids.update(ids)
                                
                                unmapped = [d for d in process_list if d['id'] not in mapped_ids]
                                if unmapped: grouped_debates["General / Uncategorized"] = unmapped
                                if leftover_list: grouped_debates[f"Older / Extra Sessions ({len(leftover_list)} Uncategorized)"] = leftover_list

                            else:
                                grouped_debates[f"All Debates (AI Error {ai_resp.status_code})"] = debate_list
                        except Exception as e:
                            grouped_debates[f"All Debates (Failsafe Triggered)"] = debate_list
                    else:
                        grouped_debates["All Debates"] = debate_list
                else:
                    error_message = "No debates found for this period/department."

            except Exception as e:
                error_message = f"Internal Error: {str(e)}"

    return render_template('debate_scanner.html', 
                           grouped_debates=grouped_debates,
                           error_message=error_message,
                           start_date=start_date, end_date=end_date,
                           departments=DEPARTMENTS_TWFY, selected_dept=selected_dept,
                           selected_house=selected_house, content_type=content_type,
                           is_post=(request.method == 'POST'))


# ==========================================
# ROUTE 2: FETCH TRANSCRIPTS (NUCLEAR WEB SCRAPER OPTION)
# ==========================================
@debate_scanner_bp.route('/debates_analyze', methods=['POST'])
def analyze_selected():
    selected = request.form.getlist('selected_debates')
    if not selected:
        return "Please go back and select at least one debate.", 400

    full_transcript_text = ""
    
    try:
        for item in selected:
            parts = item.split('||')
            url = parts[0]
            title = parts[1] if len(parts) > 1 else "Unknown Title"
            date_val = parts[2] if len(parts) > 2 else ""

            if url and url.startswith('http'):
                # Check cache first — transcripts never change once published
                cached = CachedTranscript.get(url)
                if cached:
                    full_transcript_text += f"### DEBATE: {cached.title} ({cached.date})\n\n{cached.transcript_text}\n\n"
                    continue

                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                resp = requests.get(url, headers=headers, timeout=20)

                if resp.status_code == 200:
                    html = resp.text
                    html = re.sub(r'<head.*?>.*?</head>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<script.*?>.*?</script>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<style.*?>.*?</style>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<nav.*?>.*?</nav>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<header.*?>.*?</header>', '', html, flags=re.DOTALL|re.IGNORECASE)
                    html = re.sub(r'<footer.*?>.*?</footer>', '', html, flags=re.DOTALL|re.IGNORECASE)

                    clean_text = re.sub(r'<[^>]+>', ' ', html)
                    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                    clean_text = clean_text[:60000]

                    # Store in cache for next time
                    try:
                        CachedTranscript.store(url=url, title=title, date=date_val,
                                               house='', transcript_text=clean_text)
                    except Exception:
                        pass

                    session_text = f"### DEBATE: {title} ({date_val})\n\n{clean_text}\n\n"
                    full_transcript_text += session_text
            
        if not full_transcript_text:
            return "No content could be extracted from the URL. Please try another session.", 400

        if GEMINI_API_KEY:
            model_path = get_working_model(GEMINI_API_KEY)
            # THE FIX: Added Section 6 for Active Stakeholders
            prompt = (
                "You are a senior Parliamentary Researcher. Analyze the following scraped web text of a Hansard transcript "
                "on Education and provide a high-level briefing for a University Leadership Team.\n\n"
                "Ignore any remaining website navigation text (like 'Home', 'Members', etc.) and focus only on the debate content.\n"
                "Structure your response exactly as follows (use Markdown formatting):\n"
                "## 1. EXECUTIVE SUMMARY\n(A 3-sentence overview of the main policy direction or debate focus)\n\n"
                "## 2. MINISTERIAL COMMITMENTS\n(Bullet points of any concrete promises, funding, or deadlines mentioned by the government)\n\n"
                "## 3. OPPOSITION CRITIQUE\n(Key challenges raised by Shadow Ministers or other parties)\n\n"
                "## 4. MOOD OF THE HOUSE\n(Is the sentiment generally supportive, hostile, or concerned?)\n\n"
                "## 5. KEY QUESTIONS & RESPONSES\n(Extract 3 to 5 of the most pressing questions asked by Members, paired with the Minister's direct response.)\n\n"
                "## 6. ACTIVE STAKEHOLDERS (NON-MINISTERIAL)\n(Provide a bulleted list of the backbench MPs, Opposition leads, or Peers who spoke in these specific sessions. Exclude the responding Government Minister. Briefly summarize their stance or the specific angle they focused on.)\n\n"
                f"TRANSCRIPTS:\n{full_transcript_text}"
            )
            
            ai_url = f"https://generativelanguage.googleapis.com/v1beta/{model_path}:generateContent?key={GEMINI_API_KEY}"
            ai_resp = requests.post(ai_url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=60)
            
            if ai_resp.status_code == 200:
                briefing_content = ai_resp.json().get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '')
            else:
                briefing_content = f"AI Analysis failed. (Error {ai_resp.status_code})"

            return render_template('debate_briefing.html', briefing=briefing_content, selected_count=len(selected))

    except Exception as e:
        return f"Error during analysis: {str(e)}", 500


# ==========================================
# ROUTE 3: EXPORT BRIEFING TO WORD DOC
# ==========================================
@debate_scanner_bp.route('/download_debate_briefing', methods=['POST'])
def download_debate_briefing():
    if not Document:
        return "Word library missing.", 500

    briefing_text = request.form.get('briefing_text', '')
    if not briefing_text: return "No text found.", 400

    doc = Document()
    doc.add_heading('Parliamentary Intelligence Briefing', 0)
    
    for line in briefing_text.split('\n'):
        line = line.strip()
        if not line:
            doc.add_paragraph()
            continue
        if line.startswith('##'):
            doc.add_heading(line.replace('#', '').strip(), level=2)
        elif line.startswith('*') or line.startswith('-'):
            doc.add_paragraph(line.lstrip('*- ').strip(), style='List Bullet')
        else:
            doc.add_paragraph(line.replace('**', ''))

    mem_doc = io.BytesIO()
    doc.save(mem_doc)
    mem_doc.seek(0)
    
    return send_file(mem_doc, as_attachment=True, download_name=f"Briefing_{datetime.now().strftime('%Y%m%d')}.docx", mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')