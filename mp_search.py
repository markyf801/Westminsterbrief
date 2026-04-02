import requests, os, json, io
from flask import Blueprint, render_template, request, send_file, jsonify
from datetime import datetime, timedelta

try:
    import docx
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
except ImportError:
    Document = None

mp_search_bp = Blueprint('mp_search', __name__)

DEPARTMENTS = {
    "All Departments": "", "Department for Education": "60", "Department of Health and Social Care": "17",
    "HM Treasury": "14", "Home Office": "1", "Ministry of Defence": "11", "Ministry of Justice": "54",
    "Department for Science, Innovation and Technology": "216", "Cabinet Office": "53"
}

def add_hyperlink(paragraph, url, text):
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)
    new_run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    c = OxmlElement('w:color'); c.set(qn('w:val'), '0000FF'); rPr.append(c)
    u = OxmlElement('w:u'); u.set(qn('w:val'), 'single'); rPr.append(u)
    new_run.append(rPr)
    text_element = OxmlElement('w:t'); text_element.text = text; new_run.append(text_element)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)
    return hyperlink

@mp_search_bp.route('/mp_search', methods=['GET', 'POST'])
def search_mp_pqs():
    results = []
    error_message = None
    mp_details = {}
    
    # Defaults
    selected_dept = ""
    search_mp = ""
    start_date = ""
    end_date = ""

    if request.method == 'POST':
        search_mp = request.form.get('mp_name', '').strip()
        selected_dept = request.form.get('department', '').strip() # Matches HTML 'name="department"'
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()

        if not search_mp:
            error_message = "Please enter an MP or Peer name."
        else:
            try:
                # 1. Member Lookup
                search_url = f"https://members-api.parliament.uk/api/Members/Search?Name={search_mp}"
                s_resp = requests.get(search_url, timeout=5)
                
                if s_resp.status_code == 200 and s_resp.json().get('items'):
                    member_data = s_resp.json()['items'][0]['value']
                    member_id = member_data['id']
                    
                    name = member_data.get('nameDisplayAs', 'Unknown')
                    party = member_data.get('latestParty', {}).get('name', 'No Party')
                    membership = member_data.get('latestHouseMembership', {})
                    house = "Lords" if membership.get('house') == 2 else "Commons"
                    constituency = membership.get('membershipFrom', 'Life Peer')
                    role = "Life Peer" if house == "Lords" else f"MP for {constituency}"
                    
                    mp_details = {"name": name, "party": party, "role": role}

                    # 2. PQ Fetching with enforced filters
                    params = {'askingMemberId': int(member_id), 'take': 500}
                    
                    # CRITICAL: This line links the dropdown selection to the API call
                    if selected_dept: params['answeringBodies'] = [int(selected_dept)]
                    if start_date: params['tabledWhenFrom'] = start_date
                    if end_date: params['tabledWhenTo'] = end_date

                    q_url = "https://questions-statements-api.parliament.uk/api/writtenquestions/questions"
                    q_resp = requests.get(q_url, params=params, timeout=20)
                    
                    if q_resp.status_code == 200:
                        data = q_resp.json().get('results') or []
                        # Enforce department filter in Python — API parameter alone is unreliable
                        if selected_dept:
                            data = [item for item in data
                                    if str((item.get('value') or {}).get('answeringBodyId', '')) == selected_dept]
                        for item in data:
                            val = item.get('value', {})
                            raw_date = val.get('dateTabled', '')
                            date_str = raw_date.split('T')[0] if raw_date else ''
                            is_answered = bool(val.get('answerText') or val.get('dateAnswered'))
                            uin = str(val.get('uin'))
                            
                            results.append({
                                'uin': uin,
                                'dept': val.get('answeringBodyName', 'Unknown Dept'),
                                'text': val.get('questionText', '').replace('<p>','').replace('</p>',''),
                                'date': date_str,
                                'status': "ANSWERED" if is_answered else "UNANSWERED",
                                'link': f"https://questions-statements.parliament.uk/written-questions?SearchTerm={uin}"
                            })
                else:
                    error_message = f"Could not find an MP or Peer matching '{search_mp}'."
            except Exception as e:
                error_message = f"Error: {e}"

    return render_template('mp_search.html', 
                           results=results, mp_details=mp_details,
                           error_message=error_message, departments=DEPARTMENTS, 
                           selected_dept=selected_dept, search_mp=search_mp,
                           start_date=start_date, end_date=end_date,
                           is_post=(request.method == 'POST'))

@mp_search_bp.route('/api/mp_pqs')
def api_mp_pqs():
    name = request.args.get('name', '').strip()
    topic = request.args.get('topic', '').strip()
    if not name:
        return jsonify({'error': 'Name required'}), 400
    try:
        s_resp = requests.get(
            f"https://members-api.parliament.uk/api/Members/Search?Name={name}&IsCurrentMember=true&take=5",
            timeout=5
        )
        if not (s_resp.status_code == 200 and s_resp.json().get('items')):
            return jsonify({'error': f'MP not found: {name}'}), 404

        member_data = s_resp.json()['items'][0]['value']
        member_id = member_data['id']
        display_name = member_data.get('nameDisplayAs', name)
        party = (member_data.get('latestParty') or {}).get('name', '')
        membership = member_data.get('latestHouseMembership') or {}
        house = "Lords" if membership.get('house') == 2 else "Commons"
        constituency = membership.get('membershipFrom', '')
        role = "Life Peer" if house == "Lords" else f"MP for {constituency}"

        # Fetch 6 months so topic filtering has enough to work with
        six_months_ago = (datetime.now() - timedelta(days=180)).strftime('%Y-%m-%d')
        q_resp = requests.get(
            "https://questions-statements-api.parliament.uk/api/writtenquestions/questions",
            params={'askingMemberId': member_id, 'tabledWhenFrom': six_months_ago, 'take': 50},
            timeout=10
        )
        all_pqs = []
        if q_resp.status_code == 200:
            for item in q_resp.json().get('results', []):
                val = item.get('value', {})
                raw_date = (val.get('dateTabled') or '').split('T')[0]
                is_answered = bool(val.get('answerText') or val.get('dateAnswered'))
                uin = str(val.get('uin', ''))
                all_pqs.append({
                    'uin': uin,
                    'dept': val.get('answeringBodyName', ''),
                    'text': val.get('questionText', '').replace('<p>', '').replace('</p>', ''),
                    'date': raw_date,
                    'status': 'ANSWERED' if is_answered else 'UNANSWERED',
                    'link': f"https://questions-statements.parliament.uk/written-questions/detail/{raw_date}/{uin}"
                })

        # Filter to topic-relevant PQs if a topic was supplied
        topic_filtered = False
        pqs = all_pqs
        if topic and all_pqs:
            # Build keyword list from topic (skip short stop words)
            stop_words = {'the', 'and', 'for', 'that', 'this', 'with', 'from', 'are', 'have'}
            keywords = [w.lower().strip('"\' ') for w in topic.split()
                        if len(w) > 3 and w.lower() not in stop_words]
            if keywords:
                filtered = [q for q in all_pqs
                            if any(kw in q['text'].lower() for kw in keywords)]
                if filtered:
                    pqs = filtered
                    topic_filtered = True
                # If nothing matched, fall back to all PQs with a note

        return jsonify({
            'name': display_name,
            'party': party,
            'role': role,
            'pqs': pqs[:15],  # cap display at 15
            'topic_filtered': topic_filtered,
            'total_pqs': len(all_pqs)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@mp_search_bp.route('/download_mp_word', methods=['POST'])
def download_mp_word():
    export_data_str = request.form.get('export_data')
    if not export_data_str: return "No data.", 400
    
    payload = json.loads(export_data_str)
    mp_info = payload.get('mp_details', {})
    questions = payload.get('questions', [])

    doc = Document()
    doc.add_heading(f"MP PQ Research: {mp_info.get('name')}", 0)
    
    p = doc.add_paragraph()
    p.add_run(f"Party: {mp_info.get('party')}\n").bold = True
    p.add_run(f"Role: {mp_info.get('role')}")
    
    for q in questions:
        qp = doc.add_paragraph()
        qp.add_run(f"[{q['status']}] {q['date']} | To: {q['dept']}\n").bold = True
        qp.add_run(f"\"{q['text']}\"\n").italic = True
        qp.add_run(f"Link: ")
        add_hyperlink(qp, q['link'], q['link'])

    mem = io.BytesIO(); doc.save(mem); mem.seek(0)
    return send_file(mem, as_attachment=True, download_name="MP_PQ_Research.docx")