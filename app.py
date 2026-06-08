
"""
Honigspirituosen Josef Mayer – Agent Backend
Alle Agenten in einem Python Service auf Render
"""
 
import os
import re
import json
import time
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
 
app = Flask(__name__)
CORS(app)
 
# ── API KEYS ──
GOOGLE_API_KEY   = os.environ.get("GOOGLE_API_KEY", "")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL", "")
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY     = os.environ.get("SUPABASE_ANON_KEY", "")
 
# ── SUPABASE HELPERS ──
def sb_get(table, params=""):
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{table}?{params}",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10
        )
        return r.json() if r.ok else []
    except:
        return []
 
def sb_insert(table, data):
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal"
            },
            json=data,
            timeout=10
        )
        return r.ok
    except:
        return False
 
def sb_update(table, filter_str, data):
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/{table}?{filter_str}",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json"
            },
            json=data,
            timeout=10
        )
        return r.ok
    except:
        return False
 
# ── CLAUDE (Analyse & Logik) ──
def call_claude(system_prompt, user_message, history=None):
    if not ANTHROPIC_API_KEY:
        return "ANTHROPIC_API_KEY fehlt"
    
    messages = []
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_message})
    
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01"
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 1000,
                "system": system_prompt,
                "messages": messages
            },
            timeout=20
        )
        data = r.json()
        if not r.ok:
            return f"Claude Fehler: {data}"
        return data.get("content", [{}])[0].get("text", "")
    except Exception as e:
        return f"Claude Fehler: {str(e)}"
 
# ── GPT-4o (E-Mail schreiben) ──
def call_gpt4(prompt):
    if not OPENAI_API_KEY:
        return "OPENAI_API_KEY fehlt"
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 400
            },
            timeout=15
        )
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"GPT Fehler: {str(e)}"
 
 
def get_scanned_place_ids():
    """Holt bereits gescannte place_ids aus Supabase"""
    rows = sb_get("leads", "select=place_id&place_id=not.is.null")
    return set(r.get("place_id", "") for r in rows if r.get("place_id"))
 
# ════════════════════════════════════════
# WIEN SCANNER AGENT
# ════════════════════════════════════════
 
QUALITY_SIGNALS = [
    "premium", "feinkost", "delikatess", "regional", "handwerk", "artisan",
    "bio", "craft", "spirituosen", "whisky", "gin", "rum", "wein", "destillat",
    "gourmet", "spezialität", "manufaktur", "liebhaber", "kenner"
]
 
def get_place_details(place_id):
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {"place_id": place_id, "fields": "website,formatted_phone_number,opening_hours", "key": GOOGLE_API_KEY, "language": "de"}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("result", {})
        return {
            "website": data.get("website", ""),
            "phone": data.get("formatted_phone_number", ""),
            "opening_hours": data.get("opening_hours", {}).get("weekday_text", [])
        }
    except:
        return {"website": "", "phone": "", "opening_hours": []}
 
def search_places(query, location, max_results=10):
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": f"{query} {location}", "key": GOOGLE_API_KEY, "language": "de", "region": "at"}
    results = []
    seen = set()
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        for place in data.get("results", [])[:max_results]:
            pid = place.get("place_id", "")
            if pid in seen:
                continue
            seen.add(pid)
            details = get_place_details(pid) if pid else {}
            results.append({
                "name": place.get("name", ""),
                "address": place.get("formatted_address", ""),
                "rating": place.get("rating", 0),
                "place_id": pid,
                "website": details.get("website", ""),
                "phone": details.get("phone", ""),
                "opening_hours": details.get("opening_hours", [])
            })
    except Exception as e:
        print(f"Google Maps Fehler: {e}")
    return results
 
def analyze_website(url):
    if not url:
        return {"score": 0, "signals": [], "contact_email": "", "contact_person": ""}
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=8)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator=" ").lower()
        text = re.sub(r'\s+', ' ', text).strip()
        
        emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', r.text)
        contact_email = emails[0] if emails else ""
        
        signals = [s for s in QUALITY_SIGNALS if s in text]
        score = min(10, round(len(signals) * 1.8))
        
        return {"score": score, "signals": signals[:5], "contact_email": contact_email, "contact_person": ""}
    except Exception as e:
        return {"score": 1, "signals": [], "contact_email": "", "contact_person": "", "error": str(e)}
 
def calculate_appointments(available_days):
    day_map = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5}
    weekdays = [day_map[d.lower()] for d in available_days if d.lower() in day_map]
    if not weekdays:
        weekdays = [0, 1, 2, 3, 4]
    
    slots = []
    current = datetime.now() + timedelta(days=1)
    while len(slots) < 15 and (current - datetime.now()).days < 28:
        if current.weekday() in weekdays:
            for hour, minute in [(11, 0), (13, 30), (16, 0)]:
                slots.append(current.strftime(f"%A, %d.%m.%Y") + f" um {hour:02d}:{minute:02d} Uhr")
        current += timedelta(days=1)
    return slots[:15]
 
def write_email(business, analysis, appointments):
    contact = business.get("contact_person") or ""
    salutation = f"Sehr geehrte/r {contact}," if contact else "Sehr geehrte Damen und Herren,"
    signals_text = ", ".join(analysis.get("signals", [])) or "hochwertiges Sortiment"
    appt_text = "\n".join(appointments[:3]) if appointments else "nächste Woche"
    
    prompt = f"""Schreibe eine kurze B2B-Akquise-E-Mail für Josef Mayer von Honigspirituosen Josef Mayer.
 
Empfänger: {business.get('name', '')} ({business.get('address', '')})
Anrede: {salutation}
Website-Signale: {signals_text}
 
Produkte: Wacholdergold (Gin), Fassgold (Whisky), Inselgold (Rum) – alle mit Honig veredelt, €34-44, kein Likör.
Claim: "Du erwartest Süße – du bekommst Charakter."
 
Terminvorschläge:
{appt_text}
 
Anforderungen:
- Max 5 Sätze, professionell und persönlich
- Termin für persönliche Vorstellung anfragen (Josef kommt vorbei, 3h Zeitfenster)
- Bei Termin nicht passend: sollen zurückschreiben
- Signatur: Mit freundlichen Grüßen\\nJosef Mayer\\nHonigspirituosen Josef Mayer\\nwww.honigspirituosen.at
 
Format:
BETREFF: [Betreff]
---
[E-Mail Text]"""
    
    return call_gpt4(prompt)
 
@app.route("/scan", methods=["POST"])
def scan():
    data = request.json or {}
    target_type = data.get("target_type", "Feinkostgeschäft")
    bezirk = data.get("bezirk", "Wien")
    available_days = data.get("available_days", ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"])
    max_results = min(data.get("max_results", 10), 20)
    
    appointments = calculate_appointments(available_days)
    
    queries = {
        "Feinkostgeschäft": ["Feinkost Delikatessen", "Feinkostladen"],
        "Bar": ["Cocktailbar", "Weinbar"],
        "Hotel": ["Boutique Hotel", "Design Hotel"],
        "Restaurant": ["Gourmet Restaurant", "Fine Dining"]
    }.get(target_type, [target_type])
    
    all_places = []
    already_scanned = get_scanned_place_ids()
    seen = set()
    for q in queries[:2]:
        for p in search_places(q, bezirk, max_results // 2):
            if p["place_id"] not in seen and p["place_id"] not in already_scanned:
                seen.add(p["place_id"])
                all_places.append(p)
    
    qualified = []
    for place in all_places[:max_results]:
        analysis = analyze_website(place.get("website", ""))
        if analysis["score"] >= 2 or place.get("rating", 0) >= 4.2:
            email = write_email(place, analysis, appointments[:3])
            lead = {
                "name": place["name"],
                "address": place["address"],
                "place_id": place.get("place_id", ""),
                "website": place.get("website", ""),
                "phone": place.get("phone", ""),
                "contact_email": analysis.get("contact_email", ""),
                "contact_person": analysis.get("contact_person", ""),
                "score": analysis["score"],
                "signals": json.dumps(analysis.get("signals", [])),
                "rating": place.get("rating", 0),
                "email_draft": email,
                "appointment_slots": json.dumps(appointments[:3]),
                "target_type": target_type,
                "status": "pending_approval",
                "created_at": datetime.now().isoformat()
            }
            qualified.append(lead)
            sb_insert("leads", lead)
    
    qualified.sort(key=lambda x: (x["score"], x["rating"]), reverse=True)
    
    return jsonify({
        "success": True,
        "total_found": len(all_places),
        "qualified": len(qualified),
        "leads": qualified,
        "appointments": appointments[:5]
    })
 
@app.route("/api/leads", methods=["GET"])
def get_leads():
    status = request.args.get("status", "")
    params = "select=*&order=created_at.desc"
    if status:
        params += f"&status=eq.{status}"
    leads = sb_get("leads", params)
    return jsonify({"success": True, "leads": leads, "count": len(leads)})
 
@app.route("/approve", methods=["POST"])
def approve():
    data = request.json or {}
    lead_id = data.get("lead_id", "")
    email_draft = data.get("email_draft", "")
    recipient = data.get("contact_email", "")
    
    if MAKE_WEBHOOK_URL:
        try:
            requests.post(MAKE_WEBHOOK_URL, json={
                "action": "send_email",
                "email": email_draft,
                "recipient": recipient,
                "timestamp": datetime.now().isoformat()
            }, timeout=10)
        except:
            pass
    
    if lead_id:
        sb_update("leads", f"id=eq.{lead_id}", {"status": "approved"})
    
    return jsonify({"success": True})
 
@app.route("/reject", methods=["POST"])
def reject():
    data = request.json or {}
    lead_id = data.get("lead_id", "")
    if lead_id:
        sb_update("leads", f"id=eq.{lead_id}", {"status": "rejected"})
    return jsonify({"success": True})
 
# ════════════════════════════════════════
# CHAT MIT AGENTEN (Claude)
# ════════════════════════════════════════
 
SYSTEM_PROMPT = """Du bist der KI-Assistent von Josef Mayer, Gründer von Honigspirituosen Josef Mayer (honigspirituosen.at).
 
Du steuerst Agenten und hilfst Josef seinen B2B-Vertrieb aufzubauen.
 
PRODUKTE: Wacholdergold (Gin), Fassgold (Whisky in 4 Honigsorten), Inselgold (Rum) – alle Honig-veredelt, €34-44, kein Likör.
CLAIM: "Du erwartest Süße – du bekommst Charakter."
 
VERFÜGBARE AGENTEN:
- Wien Scanner: Findet B2B-Leads (Feinkost, Bars, Hotels) → /scan
- E-Mail Agent: Schreibt personalisierte E-Mails → GPT-4o
- Weitere in Entwicklung
 
Wenn Josef einen Agenten starten will, antworte mit einem JSON-Befehl:
{"action": "start_scan", "params": {"target_type": "...", "bezirk": "...", "available_days": [...]}}
 
Sonst antworte normal auf Deutsch. Kurz, direkt, auf Du."""
 
@app.route("/chat", methods=["POST"])
def chat():
    data = request.json or {}
    message = (data.get("message") or "").strip()
    history = data.get("history", [])
    
    if not message:
        return jsonify({"success": True, "answer": "Schreib mir was du brauchst."})
    
    # Save to Supabase
    sb_insert("chat_history", {"role": "user", "content": message, "created_at": datetime.now().isoformat()})
    
    answer = call_claude(SYSTEM_PROMPT, message, history)
    
    # Check if Claude wants to trigger an action
    action_data = None
    try:
        json_match = re.search(r'\{[^{}]*"action"[^{}]*\}', answer)
        if json_match:
            action_data = json.loads(json_match.group())
    except:
        pass
    
    sb_insert("chat_history", {"role": "assistant", "content": answer, "created_at": datetime.now().isoformat()})
    
    return jsonify({"success": True, "answer": answer, "action": action_data})
 
@app.route("/api/chat-history", methods=["GET"])
def chat_history():
    rows = sb_get("chat_history", "select=*&order=created_at.asc&limit=50")
    return jsonify({"success": True, "history": rows})
 
@app.route("/api/load-memory", methods=["GET"])
def load_memory():
    rows = sb_get("lena_memory", "select=*&order=updated_at.desc")
    return jsonify({"success": True, "count": len(rows)})
 
# ════════════════════════════════════════
# STATISCHE DATEIEN & HEALTH
# ════════════════════════════════════════
 
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "online", "service": "Honigspirituosen Agents", "version": "1.0"})
 
@app.route("/", methods=["GET"])
def index():
    return send_file("index.html", mimetype="text/html")
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
