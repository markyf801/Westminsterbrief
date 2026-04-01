import requests, io, docx, csv
from flask import Blueprint, render_template, request, make_response
from docx import Document
from docx.oxml.shared import OxmlElement, qn
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from cache_models import CachedQuestion, CachedMember

hansard_bp = Blueprint('hansard', __name__)

DEPARTMENTS = {
    "All Departments": "", "Department for Education": "60", "Department of Health and Social Care": "17",
    "HM Treasury": "14", "Home Office": "1", "Ministry of Defence": "11", "Ministry of Justice": "54",
    "Department for Science, Innovation and Technology": "216", "Cabinet Office": "53"
}
MEMBER_CACHE = {}

def prefetch_members(member_ids):
    """Parallel-fetch member details for all IDs not already cached."""
    unknown = [mid for mid in member_ids if mid and mid not in MEMBER_CACHE and not CachedMember.get(mid)]
    if not unknown:
        return
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(get_member_details, mid, 'Commons'): mid for mid in unknown}
        for f in as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

def get_member_details(member_id, fallback_house):
    if not member_id: return "Unknown", "No Party", "Unknown", fallback_house
    if member_id in MEMBER_CACHE: return MEMBER_CACHE[member_id]

    # Check DB cache first
    cached = CachedMember.get(member_id)
    if cached:
        result = (cached.name, cached.party, cached.constituency, cached.house)
        MEMBER_CACHE[member_id] = result
        return result

    try:
        url = f"https://members-api.parliament.uk/api/Members/{member_id}"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            data = resp.json().get('value') or {}
            membership = data.get('latestHouseMembership') or {}
            name = data.get('nameDisplayAs') or 'Unknown'
            party = (data.get('latestParty') or {}).get('name') or 'No Party'
            constituency = membership.get('membershipFrom') or 'Unknown'
            house = "Lords" if membership.get('house') == 2 else "Commons"
            image_url = data.get('thumbnailUrl') or ''
            CachedMember.store(member_id, name, party, constituency, house, image_url)
            MEMBER_CACHE[member_id] = (name, party, constituency, house)
            return MEMBER_CACHE[member_id]
    except: pass
    return "Member", "Party Info Pending", "Unknown", fallback_house

def add_hyperlink(paragraph, url, text):
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink", is_external=True)
    hyperlink = OxmlElement('w:hyperlink'); hyperlink.set(qn('r:id'), r_id)
    new_run = OxmlElement('w:r'); rPr = OxmlElement('w:rPr')
    c = OxmlElement('w:color'); c.set(qn('w:val'), '0000FF'); rPr.append(c)
    u = OxmlElement('w:u'); u.set(qn('w:val'), 'single'); rPr.append(u)
    new_run.append(rPr); new_run.text = text; hyperlink.append(new_run)
    paragraph._p.append(hyperlink)

@hansard_bp.route('/questions', methods=['GET', 'POST'])
def index():
    results, error_message = [], None
    selected_dept_id = ""
    selected_house = "All"
    subject, start_date, end_date = "", "", ""
    
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        start_date = request.form.get('start_date', '').strip()
        end_date = request.form.get('end_date', '').strip()
        selected_dept_id = request.form.get('department', '').strip() 
        selected_house = request.form.get('house_filter', 'All')
        action = request.form.get('action', 'search')
        
        subjects = [s.strip() for s in subject.split(',') if s.strip()] or ['']
        all_raw_results = []
        seen_uins = set()

        try:
            for subj in subjects:
                # Step 1: The API Pull
                params = {'take': 400} 
                if subj: params['searchTerm'] = subj
                if start_date: params['tabledStartDate'] = start_date
                if end_date: params['tabledEndDate'] = end_date
                if selected_dept_id: params['answeringBodies'] = [int(selected_dept_id)]
                
                resp = requests.get("https://questions-statements-api.parliament.uk/api/writtenquestions/questions", params=params, timeout=30)
                if resp.status_code == 200:
                    batch = resp.json().get('results') or []
                    for item in batch:
                        uin = item.get('value', {}).get('uin')
                        if uin and uin not in seen_uins:
                            seen_uins.add(uin)
                            all_raw_results.append(item)

            # Step 2: Pre-warm member cache in parallel before processing
            unique_member_ids = list({
                item.get('value', {}).get('askingMemberId')
                for item in all_raw_results
                if item.get('value', {}).get('askingMemberId')
            })
            prefetch_members(unique_member_ids)

            # Step 3: THE STRICT PYTHON DATE & DEPT FILTER
            for item in all_raw_results:
                val = item.get('value') or {}
                
                # REJECT wrong departments instantly
                if selected_dept_id and str(val.get('answeringBodyId')) != selected_dept_id:
                    continue 
                
                # REJECT wrong dates manually
                raw_date_full = val.get('dateTabled') or ''
                raw_date_str = raw_date_full.split('T')[0] # Get YYYY-MM-DD format
                
                # Python hard-comparison: if date is before start or after end, delete it
                if start_date and raw_date_str < start_date: continue
                if end_date and raw_date_str > end_date: continue
                
                # If it survives the "bouncer", process for UI
                val_member_id = val.get('askingMemberId')
                house_raw = val.get('house', 'Commons')
                name, party, constituency, actual_house = get_member_details(val_member_id, house_raw)
                
                if selected_house != "All" and actual_house != selected_house: continue
                
                try:
                    date_obj = datetime.fromisoformat(raw_date_str)
                    f_date = f"{date_obj.day} {date_obj.strftime('%B %Y')}"
                except: f_date = "N/A"
                
                question_url = f"https://questions-statements.parliament.uk/written-questions/detail/{raw_date_str}/{val.get('uin')}"
                question_text = val.get('questionText', '').replace('<p>','').replace('</p>','')

                # Cache questions older than 7 days — they won't change
                uin = val.get('uin')
                if uin and CachedQuestion.is_cacheable(raw_date_str):
                    try:
                        if not CachedQuestion.get(uin):
                            CachedQuestion.store(
                                uin=uin, member_name=name, party=party,
                                department_id=val.get('answeringBodyId', ''),
                                department_name=val.get('answeringBodyName', ''),
                                question_text=question_text,
                                answer_text=val.get('answerText'),
                                date_tabled=raw_date_str, url=question_url
                            )
                    except Exception:
                        pass

                results.append({
                    'display_text': f"[{val.get('answeringBodyName')}] {name}: {val.get('questionText')}",
                    'url': question_url,
                    'dept': val.get('answeringBodyName'), 'name': name, 'party': party,
                    'role': "Life Peer" if actual_house == "Lords" else f"MP for {constituency}",
                    'date': f_date, 'text': question_text
                })
                        
            # Export Handlers...
            if action == 'word' and results:
                doc = Document(); doc.add_heading('Parliamentary Written Questions', 0)
                for r in results:
                    p = doc.add_paragraph()
                    p.add_run(f"[{r['dept']}] {r['name']} ({r['party']}, {r['role']})\n").bold = True
                    p.add_run(f"Date Asked: {r['date']}\nQuestion: {r['text']}\nLink: ")
                    add_hyperlink(p, r['url'], r['url'])
                b = io.BytesIO(); doc.save(b); b.seek(0)
                output = make_response(b.getvalue()); output.headers["Content-Disposition"] = "attachment; filename=Hansard_Export.docx"
                return output
            elif action == 'csv' and results:
                si = io.StringIO(); cw = csv.writer(si)
                cw.writerow(['Department', 'Member', 'Party', 'Constituency', 'Date', 'Question', 'URL'])
                for r in results: cw.writerow([r['dept'], r['name'], r['party'], r['role'], r['date'], r['text'], r['url']])
                output = make_response(si.getvalue()); output.headers["Content-Disposition"] = "attachment; filename=Hansard_Export.csv"; output.headers["Content-type"] = "text/csv"
                return output
                
        except Exception as e: error_message = f"Search error: {str(e)}"
            
    return render_template('index.html', results=results, error_message=error_message, departments=DEPARTMENTS, selected_dept=selected_dept_id, selected_house=selected_house, subject=subject, start_date=start_date, end_date=end_date)