
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
    return send_file("index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


# ════════════════════════════════════════
# FOTO/CONTENT AGENT
# Jeden Morgen Wetter + Shooting-Empfehlung via Telegram
# ════════════════════════════════════════

def get_weather(city="Wien"):
    """Holt Wetterdaten via Open-Meteo (kostenlos, kein API Key)"""
    try:
        # Koordinaten für Wien und NÖ
        coords = {
            "Wien": (48.2082, 16.3738),
            "Niederösterreich": (48.1, 15.8)
        }
        lat, lon = coords.get(city, coords["Wien"])
        
        url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weathercode,windspeed_10m&daily=weathercode,temperature_2m_max,precipitation_sum,sunrise,sunset&timezone=Europe/Vienna&forecast_days=1"
        r = requests.get(url, timeout=10)
        data = r.json()
        
        current = data.get("current", {})
        daily = data.get("daily", {})
        
        weather_codes = {
            0: "Sonnig ☀️", 1: "Überwiegend sonnig 🌤️", 2: "Teilweise bewölkt ⛅",
            3: "Bewölkt ☁️", 45: "Nebelig 🌫️", 48: "Reif-Nebel 🌫️",
            51: "Leichter Nieselregen 🌦️", 53: "Nieselregen 🌦️", 55: "Starker Nieselregen 🌧️",
            61: "Leichter Regen 🌧️", 63: "Regen 🌧️", 65: "Starker Regen 🌧️",
            71: "Leichter Schnee ❄️", 73: "Schnee ❄️", 75: "Starker Schnee ❄️",
            80: "Regenschauer 🌦️", 81: "Schauer 🌧️", 82: "Starke Schauer ⛈️",
            95: "Gewitter ⛈️"
        }
        
        code = current.get("weathercode", 0)
        weather_desc = weather_codes.get(code, "Unbekannt")
        temp = current.get("temperature_2m", 0)
        wind = current.get("windspeed_10m", 0)
        sunrise = daily.get("sunrise", [""])[0].split("T")[1] if daily.get("sunrise") else ""
        sunset = daily.get("sunset", [""])[0].split("T")[1] if daily.get("sunset") else ""
        precip = daily.get("precipitation_sum", [0])[0]
        
        return {
            "description": weather_desc,
            "temperature": temp,
            "wind": wind,
            "sunrise": sunrise,
            "sunset": sunset,
            "precipitation": precip,
            "code": code,
            "good_for_outdoor": code <= 3 and precip < 1.0
        }
    except Exception as e:
        return {"description": "Wetter nicht verfügbar", "good_for_outdoor": True, "error": str(e)}

def send_telegram(message):
    """Sendet eine Nachricht via Telegram"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        return r.ok
    except:
        return False

def generate_photo_recommendation(weather_wien, weather_noe):
    """Generiert Foto-Empfehlung basierend auf Wetter und Saison"""
    
    month = datetime.now().month
    hour = datetime.now().hour
    
    # Saisonaler Content-Kalender
    seasonal_content = {
        1: ["Fassgold auf verschneitem Tisch", "Winterstimmung mit Kerzenlicht", "Jahreswechsel-Stimmung"],
        2: ["Valentinstag – Geschenkidee Spirituosen", "Winterende – erste Sonnenstrahlen"],
        3: ["Frühlingserwachen am Bienenstand", "Erste Blüten mit Wacholdergold"],
        4: ["Bienen kehren zurück", "Frühlingsblüten – Blütenhonig-Saison beginnt"],
        5: ["Rapsfeld mit Bienenstöcken", "Maiblüten – Blütenhonig Ernte"],
        6: ["Sommerabend mit Wacholdergold Tonic", "Linden blühen – Lindenhonig-Saison"],
        7: ["Sommer am Bienenstand", "Cocktail-Saison – Inselgold auf Eis"],
        8: ["Sonnenblumen – Sonnenblumenhonig", "Hochsommer Ernte"],
        9: ["Edelkastanie Saison beginnt", "Herbststimmung – Fassgold Edelkastanie"],
        10: ["Herbstlaub mit Fassgold", "Ernte abgeschlossen – Zeit für Whisky"],
        11: ["Adventstimmung", "Geschenksets für Weihnachten"],
        12: ["Weihnachts-Stimmung", "Jahresabschluss – Fassgold am Kamin"]
    }
    
    season_ideas = seasonal_content.get(month, ["Produktfoto am Bienenstand"])
    
    # Lichtempfehlung
    if weather_wien["good_for_outdoor"]:
        sunrise = weather_wien.get("sunrise", "06:00")
        sunset = weather_wien.get("sunset", "20:00")
        light_tip = f"🌅 Goldene Stunde morgens nach {sunrise} oder abends vor {sunset}"
        outdoor_tip = "✅ Perfekt für Außenaufnahmen am Bienenstand!"
    else:
        outdoor_tip = "🏠 Schlechtwetter → Drinnen fotografieren"
        light_tip = "💡 Fensterseite nutzen, weiches diffuses Licht ideal für Produktfotos"
    
    # Studio-Szene bei Schlechtwetter
    indoor_scenes = [
        "Flasche auf dunklem Holzbrett mit Honigwabe daneben",
        "Flat-Lay: alle drei Produkte von oben, goldener Hintergrund",
        "Flasche gegen Fenster – Gegenlicht zeigt die Farbe des Inhalts",
        "Nahaufnahme Etikett mit unscharfem Hintergrund",
        "Glas mit Inselgold und einer Zimtstange",
        "Fassgold mit Whisky-Glas und Eichenwürfel"
    ]
    
    import random
    indoor_scene = random.choice(indoor_scenes)
    season_idea = random.choice(season_ideas)
    
    return {
        "outdoor_possible": weather_wien["good_for_outdoor"],
        "outdoor_tip": outdoor_tip,
        "light_tip": light_tip,
        "season_idea": season_idea,
        "indoor_scene": indoor_scene,
        "weather_wien": weather_wien["description"],
        "weather_noe": weather_noe["description"],
        "temp": weather_wien.get("temperature", 0)
    }

@app.route("/foto-empfehlung", methods=["GET", "POST"])
def foto_empfehlung():
    """Generiert Foto-Empfehlung und sendet sie via Telegram"""
    
    weather_wien = get_weather("Wien")
    weather_noe = get_weather("Niederösterreich")
    rec = generate_photo_recommendation(weather_wien, weather_noe)
    
    today = datetime.now().strftime("%A, %d.%m.%Y")
    
    message = f"""🍯 <b>Honigspirituosen – Foto-Tipp für heute</b>
{today}

🌤️ <b>Wetter Wien:</b> {rec['weather_wien']} | {rec['temp']}°C
🌿 <b>Wetter NÖ:</b> {rec['weather_noe']}

{rec['outdoor_tip']}
{rec['light_tip']}

📸 <b>Saisonaler Content-Vorschlag:</b>
{rec['season_idea']}

🏠 <b>Alternative Drinnen-Szene:</b>
{rec['indoor_scene']}

<i>honigspirituosen.at</i>"""

    telegram_sent = send_telegram(message)
    
    return jsonify({
        "success": True,
        "recommendation": rec,
        "telegram_sent": telegram_sent,
        "message": message
    })

@app.route("/telegram-test", methods=["GET"])
def telegram_test():
    """Testet die Telegram-Verbindung"""
    sent = send_telegram("🍯 Test von Honigspirituosen Agenten – Telegram funktioniert!")
    return jsonify({"success": sent, "message": "Telegram Test" if sent else "Telegram Fehler – Token oder Chat-ID prüfen"})


