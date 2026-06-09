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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

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

# ════════════════════════════════════════
# TELEGRAM HELPER
# ════════════════════════════════════════
def send_telegram(text):
    """Sendet eine Nachricht an Josefs Telegram. Gibt True/False zurück."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
        return r.ok
    except:
        return False

# ════════════════════════════════════════
# FOTO AGENT – Wetter & Empfehlung
# ════════════════════════════════════════

# Wien & St. Pölten (NÖ) Koordinaten
WETTER_ORTE = {
    "wien": {"lat": 48.2082, "lon": 16.3738},
    "noe":  {"lat": 48.2047, "lon": 15.6256}  # St. Pölten
}

WETTERCODE = {
    0: "klar", 1: "überwiegend klar", 2: "teils bewölkt", 3: "bewölkt",
    45: "Nebel", 48: "Reifnebel", 51: "leichter Niesel", 53: "Niesel",
    55: "starker Niesel", 61: "leichter Regen", 63: "Regen", 65: "starker Regen",
    71: "leichter Schnee", 73: "Schnee", 75: "starker Schnee", 77: "Schneegriesel",
    80: "Regenschauer", 81: "Regenschauer", 82: "heftige Schauer",
    85: "Schneeschauer", 86: "Schneeschauer", 95: "Gewitter", 96: "Gewitter mit Hagel"
}

def get_wetter(lat, lon):
    """Holt aktuelles Wetter von Open-Meteo (kostenlos, kein API Key)."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,cloud_cover",
                "timezone": "Europe/Vienna"
            },
            headers={"User-Agent": "HonigAgent/1.0"},
            timeout=10
        )
        if not r.ok:
            print(f"[WETTER] Open-Meteo HTTP {r.status_code}: {r.text[:200]}")
            return {"temp": None, "code": -1, "beschreibung": "Wetter nicht verfügbar", "cloud_cover": None, "ok": False}

        data = r.json().get("current", {})
        print(f"[WETTER] Open-Meteo current: {data}")

        temp_raw = data.get("temperature_2m")
        code = data.get("weather_code")
        cloud = data.get("cloud_cover")

        # None-sichere Verarbeitung
        temp = round(temp_raw) if temp_raw is not None else None
        if code is None:
            code = -1

        return {
            "temp": temp,
            "code": code,
            "beschreibung": WETTERCODE.get(code, "wechselhaft" if code >= 0 else "Wetter nicht verfügbar"),
            "cloud_cover": cloud,
            "ok": temp is not None
        }
    except Exception as e:
        print(f"[WETTER] Exception: {e}")
        return {"temp": None, "code": -1, "beschreibung": "Wetter nicht verfügbar", "cloud_cover": None, "ok": False, "error": str(e)}

def aktuelle_saison():
    """Gibt die aktuelle Jahreszeit + saisonalen Honig-Kontext zurück."""
    monat = datetime.now().month
    if monat in (3, 4, 5):
        return {
            "name": "Frühling",
            "honig": "Frühlingsblüte, Löwenzahn, Obstblüte",
            "stimmung": "frisches Licht, Blüten, Neubeginn",
            "produkt_fokus": "Wacholdergold (Gin) – passt zur frischen, klaren Jahreszeit"
        }
    elif monat in (6, 7, 8):
        return {
            "name": "Sommer",
            "honig": "Linde, Sonnenblume, Waldhonig",
            "stimmung": "warmes Abendlicht, goldene Stunde, Terrasse",
            "produkt_fokus": "Inselgold (Rum) – sommerlich, entspannt, für laue Abende"
        }
    elif monat in (9, 10, 11):
        return {
            "name": "Herbst",
            "honig": "Edelkastanie, Heidehonig, Wald",
            "stimmung": "warme Töne, gemütlich, bernsteinfarbenes Licht",
            "produkt_fokus": "Fassgold (Whisky) – warm, tief, perfekt für kühlere Tage"
        }
    else:
        return {
            "name": "Winter",
            "honig": "dunkler Waldhonig, kräftige Sorten",
            "stimmung": "Kerzenlicht, Kaminstimmung, Innenaufnahmen",
            "produkt_fokus": "Fassgold (Whisky) – Wärme im Glas, Festtagsstimmung"
        }

def generate_foto_empfehlung():
    """Erzeugt eine Foto-Empfehlung basierend auf Wetter + Saison via Claude."""
    wetter_wien = get_wetter(WETTER_ORTE["wien"]["lat"], WETTER_ORTE["wien"]["lon"])
    wetter_noe  = get_wetter(WETTER_ORTE["noe"]["lat"], WETTER_ORTE["noe"]["lon"])
    saison = aktuelle_saison()

    wetter_ok = wetter_wien.get("ok", False)

    # Outdoor möglich? Nur bewerten wenn Wetterdaten da sind
    schlechtwetter_codes = {51,53,55,61,63,65,71,73,75,77,80,81,82,85,86,95,96}
    if wetter_ok:
        outdoor_moeglich = wetter_wien["code"] not in schlechtwetter_codes
    else:
        outdoor_moeglich = None  # unbekannt

    # Anzeige-Strings (None-sicher)
    temp_str = f"{wetter_wien['temp']}°C" if wetter_wien.get("temp") is not None else "k.A."
    cloud_str = f", Bewölkung {wetter_wien['cloud_cover']}%" if wetter_wien.get("cloud_cover") is not None else ""
    noe_temp_str = f", {wetter_noe['temp']}°C" if wetter_noe.get("temp") is not None else ""

    if wetter_ok:
        wetter_zeile = f"Wetter heute in Wien: {wetter_wien['beschreibung']}, {temp_str}{cloud_str}."
        outdoor_zeile = f"Outdoor-Fotos heute {'sinnvoll' if outdoor_moeglich else 'eher nicht (Wetter)'}."
    else:
        wetter_zeile = "Wetterdaten heute nicht verfügbar – gib eine wetterunabhängige Empfehlung."
        outdoor_zeile = "Outdoor-Eignung unbekannt – gib sowohl eine Outdoor- als auch eine Indoor-Idee."

    system_prompt = """Du bist der Foto-Berater von Honigspirituosen Josef Mayer in Wien.
Josef ist Berufsimker und stellt Premium-Spirituosen her: Wacholdergold (Gin), Fassgold (Whisky), Inselgold (Rum) – alle mit eigenem Honig veredelt.
Claim: "Du erwartest Süße – du bekommst Charakter."
Du gibst kurze, konkrete, umsetzbare Foto-Empfehlungen für Social Media. Kein Geschwafel, direkt und praktisch."""

    user_msg = f"""{wetter_zeile}
Jahreszeit: {saison['name']}. Saisonale Honige: {saison['honig']}. Stimmung: {saison['stimmung']}. Produkt-Fokus: {saison['produkt_fokus']}.
{outdoor_zeile}

Gib mir eine Foto-Empfehlung für heute. Antworte NUR mit JSON, kein anderer Text:
{{"outdoor_tip": "ein konkreter Tipp für Outdoor-Fotos heute (1 Satz)", "light_tip": "Tipp zum Licht heute (1 Satz)", "season_idea": "eine konkrete saisonale Foto-Idee mit einem der Produkte (1-2 Sätze)", "indoor_scene": "eine Indoor-Szene als Alternative (1 Satz)"}}"""

    antwort = call_claude(system_prompt, user_msg)
    
    # JSON aus Antwort extrahieren
    try:
        json_match = re.search(r'\{.*\}', antwort, re.DOTALL)
        ideen = json.loads(json_match.group()) if json_match else {}
    except:
        ideen = {}

    # Anzeige-Werte für Frontend
    weather_wien_display = wetter_wien["beschreibung"] if wetter_ok else "nicht verfügbar"
    weather_noe_display = (f"{wetter_noe['beschreibung']}{noe_temp_str}" if wetter_noe.get("ok") else "nicht verfügbar")

    recommendation = {
        "weather_wien": weather_wien_display,
        "temp": wetter_wien["temp"] if wetter_wien.get("temp") is not None else "–",
        "weather_noe": weather_noe_display,
        "outdoor_tip": ideen.get("outdoor_tip", "Heute flexibel bleiben."),
        "light_tip": ideen.get("light_tip", "Weiches Tageslicht am Fenster nutzen."),
        "season_idea": ideen.get("season_idea", f"{saison['produkt_fokus']}"),
        "indoor_scene": ideen.get("indoor_scene", "Produkt auf Holztisch mit Tageslicht."),
        "outdoor_moeglich": bool(outdoor_moeglich) if outdoor_moeglich is not None else False,
        "wetter_ok": wetter_ok
    }
    return recommendation, saison

@app.route("/wetter-test", methods=["GET"])
def wetter_test():
    """Diagnose: zeigt rohe Wetterdaten von Open-Meteo."""
    w = get_wetter(WETTER_ORTE["wien"]["lat"], WETTER_ORTE["wien"]["lon"])
    return jsonify({"wien": w})

@app.route("/foto-empfehlung", methods=["GET"])
def foto_empfehlung():
    """Generiert die heutige Foto-Empfehlung, speichert sie und schickt Telegram."""
    recommendation, saison = generate_foto_empfehlung()

    # Wetter-Anzeige-String (None-sicher)
    if recommendation.get("wetter_ok"):
        wetter_wien_str = f"{recommendation['weather_wien']}, {recommendation['temp']}°C"
    else:
        wetter_wien_str = "Wetter nicht verfügbar"

    # In Supabase speichern
    sb_insert("foto_empfehlungen", {
        "datum": datetime.now().isoformat(),
        "wetter_wien": wetter_wien_str,
        "wetter_noe": recommendation["weather_noe"],
        "saison_idee": recommendation["season_idea"],
        "indoor_szene": recommendation["indoor_scene"],
        "outdoor_moeglich": recommendation["outdoor_moeglich"],
        "erledigt": False,
        "notiz": ""
    })

    # Telegram Push
    telegram_text = (
        f"📸 <b>Foto-Empfehlung heute</b>\n\n"
        f"🌤 Wien: {wetter_wien_str}\n\n"
        f"💡 <b>Idee:</b> {recommendation['season_idea']}\n\n"
        f"☀️ Licht: {recommendation['light_tip']}\n"
        f"🏠 Indoor: {recommendation['indoor_scene']}"
    )
    telegram_sent = send_telegram(telegram_text)

    return jsonify({
        "success": True,
        "recommendation": recommendation,
        "telegram_sent": telegram_sent
    })

@app.route("/telegram-test", methods=["GET"])
def telegram_test():
    """Testet ob Telegram funktioniert."""
    ok = send_telegram("✅ Test von Honigspirituosen Agent – Telegram funktioniert!")
    return jsonify({"success": ok, "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)})

# ════════════════════════════════════════
# CONTENT CREATOR AGENT
# Foto/Bild → Captions für IG Feed, IG Story, Facebook, LinkedIn
# ════════════════════════════════════════

# Claude kann Bilder analysieren – diese Funktion nimmt base64 Bild
def call_claude_vision(system_prompt, user_text, image_base64, media_type="image/jpeg"):
    """Claude mit Bild-Input. image_base64 ohne data:... Prefix."""
    if not ANTHROPIC_API_KEY:
        return "ANTHROPIC_API_KEY fehlt"
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
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_base64}},
                        {"type": "text", "text": user_text}
                    ]
                }]
            },
            timeout=30
        )
        data = r.json()
        if not r.ok:
            return f"Claude Vision Fehler: {data}"
        return data.get("content", [{}])[0].get("text", "")
    except Exception as e:
        return f"Claude Vision Fehler: {str(e)}"

CONTENT_SYSTEM = """Du bist der Social-Media-Texter von Honigspirituosen Josef Mayer aus Wien.
Josef ist Berufsimker und stellt Premium-Spirituosen her – alle mit eigenem Honig veredelt:
- Wacholdergold (Gin, €39,90) – frisch, klar, feine Honigrunde
- Fassgold (Whisky, €44,00) – warm, tief, komplex, in 4 Honigsorten
- Inselgold (Rum, €34,90) – warm, weich, der Honig macht ihn runder, nicht süßer
Claim: "Du erwartest Süße – du bekommst Charakter."
Kein Likör. Echtes Handwerk aus Wien.

TON: authentisch, hochwertig, nahbar. Josef spricht persönlich als Imker. Keine Werbefloskeln, kein übertriebenes Marketing-Deutsch. Charakter statt Kitsch.

Du schreibst Captions für 4 Plattformen mit unterschiedlichem Stil:
- Instagram Feed: emotional, bildstark, 2-4 Sätze, Story-Charakter
- Instagram Story: sehr kurz, knackig, 1 Satz + Call-to-Action
- Facebook: etwas ausführlicher, erzählend, darf persönlicher sein
- LinkedIn: professionell, Fokus auf Handwerk/Qualität/Unternehmertum, B2B-tauglich"""

@app.route("/content/generate", methods=["POST"])
def content_generate():
    """Erzeugt Captions für alle 4 Plattformen aus Bild + optionalem Anlass."""
    data = request.json or {}
    image_base64 = data.get("image_base64", "")
    media_type = data.get("media_type", "image/jpeg")
    anlass = (data.get("anlass") or "").strip()
    image_source = data.get("image_source", "upload")  # upload oder dalle

    anlass_text = f"\n\nBesonderer Anlass / Kontext für diesen Post: {anlass}" if anlass else ""

    user_text = f"""Schau dir das Bild an und schreibe Captions für alle 4 Plattformen.{anlass_text}

Antworte NUR mit JSON, kein anderer Text, genau in diesem Format:
{{
  "bild_beschreibung": "kurz was auf dem Bild zu sehen ist (1 Satz)",
  "instagram_feed": {{"caption": "...", "hashtags": "#... #... #..."}},
  "instagram_story": {{"caption": "...", "hashtags": "#... #..."}},
  "facebook": {{"caption": "...", "hashtags": "#... #..."}},
  "linkedin": {{"caption": "...", "hashtags": "#... #..."}}
}}

Hashtags: 5-10 relevante pro Plattform (Mix aus Marke, Region Wien, Produktkategorie). LinkedIn weniger Hashtags (3-5), professioneller."""

    if not image_base64:
        return jsonify({"success": False, "error": "Kein Bild übergeben"})

    antwort = call_claude_vision(CONTENT_SYSTEM, user_text, image_base64, media_type)

    # JSON extrahieren
    try:
        json_match = re.search(r'\{.*\}', antwort, re.DOTALL)
        content = json.loads(json_match.group()) if json_match else None
    except:
        content = None

    if not content:
        return jsonify({"success": False, "error": "Konnte Captions nicht erzeugen", "raw": antwort[:500]})

    return jsonify({"success": True, "content": content, "image_source": image_source})

@app.route("/content/dalle", methods=["POST"])
def content_dalle():
    """Generiert ein Werbebild via DALL-E 3. Auf Knopfdruck (kostet ~$0,04-0,08)."""
    if not OPENAI_API_KEY:
        return jsonify({"success": False, "error": "OPENAI_API_KEY fehlt"})

    data = request.json or {}
    motiv = (data.get("motiv") or "").strip()
    if not motiv:
        return jsonify({"success": False, "error": "Kein Motiv angegeben"})

    saison = aktuelle_saison()
    prompt = f"""Professional advertising photograph for a premium Austrian honey-infused spirits brand.
{motiv}.
Style: high-end product photography, warm amber and golden tones, natural light, artisanal and authentic mood, Viennese craftsmanship aesthetic. Season: {saison['name']}. No text, no logos, no labels with readable words. Elegant, not kitschy. Editorial quality."""

    try:
        r = requests.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "dall-e-3",
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024",
                "quality": "standard",
                "response_format": "b64_json"
            },
            timeout=60
        )
        data_r = r.json()
        if not r.ok:
            return jsonify({"success": False, "error": f"DALL-E Fehler: {data_r}"})
        b64 = data_r["data"][0]["b64_json"]
        return jsonify({"success": True, "image_base64": b64, "media_type": "image/png"})
    except Exception as e:
        return jsonify({"success": False, "error": f"DALL-E Fehler: {str(e)}"})

# ── FOTO ARCHIV API ──
@app.route("/api/foto-archiv", methods=["GET"])
def foto_archiv():
    rows = sb_get("foto_empfehlungen", "select=*&order=datum.desc&limit=30")
    return jsonify({"success": True, "empfehlungen": rows})

@app.route("/api/foto-erledigt", methods=["POST"])
def foto_erledigt():
    data = request.json or {}
    sb_update("foto_empfehlungen", f"id=eq.{data.get('id')}", {"erledigt": data.get("erledigt", False)})
    return jsonify({"success": True})

@app.route("/api/foto-notiz", methods=["POST"])
def foto_notiz():
    data = request.json or {}
    sb_update("foto_empfehlungen", f"id=eq.{data.get('id')}", {"notiz": data.get("notiz", "")})
    return jsonify({"success": True})


# ════════════════════════════════════════
# PROSPEKT AGENT
# Erstellt druckfertige PDFs für verschiedene Zielgruppen
# ════════════════════════════════════════

PROSPEKT_TEMPLATES = {
    "Feinkost": {
        "titel": "Honigspirituosen für Ihr Sortiment",
        "untertitel": "Handwerkliche Premium-Spirituosen aus Wien",
        "ansprache": "Sehr geehrte Damen und Herren",
        "intro": "Wir sind ein Wiener Familienbetrieb – Berufsimker trifft Destillateur. Unsere Spirituosen werden mit eigenem Honig veredelt und sprechen Kunden an, die Qualität zu schätzen wissen.",
        "cta": "Vereinbaren Sie einen unverbindlichen Verkostungstermin.",
        "farbe": "#1a1a1a"
    },
    "Bar": {
        "titel": "Neue Dimension für Ihre Cocktailkarte",
        "untertitel": "Honig-veredelte Spirituosen aus Wien",
        "ansprache": "Liebe Bartender & Gastronomen",
        "intro": "Wacholdergold, Fassgold und Inselgold – drei Spirituosen die Honig nicht süß machen, sondern runder. Perfekt für Signature Cocktails die in Erinnerung bleiben.",
        "cta": "Wir kommen gerne zur Verkostung in Ihre Bar.",
        "farbe": "#1a1a1a"
    },
    "Hotel": {
        "titel": "Exklusiv für Ihre Gäste",
        "untertitel": "Wiener Premium-Spirituosen als besonderes Erlebnis",
        "ansprache": "Sehr geehrte Damen und Herren",
        "intro": "Bieten Sie Ihren Gästen etwas Besonderes: authentische Wiener Handwerkskunst in der Flasche. Honig-veredelte Spirituosen mit einer Geschichte die man gerne weitererzählt.",
        "cta": "Sprechen Sie uns für Exklusivkonditionen an.",
        "farbe": "#1a1a1a"
    },
    "Markt": {
        "titel": "🍯 Honigspirituosen",
        "untertitel": "Direkt vom Imker – handgemacht in Wien",
        "ansprache": "Liebe Freunde guter Spirituosen",
        "intro": "Ich bin Josef, Berufsimker aus Wien. Meine Bienen liefern den Honig, ich veredle damit Gin, Whisky und Rum. Kein Likör – echte Spirituosen mit Charakter.",
        "cta": "Probieren Sie gerne vor Ort! Alle Produkte heute erhältlich.",
        "farbe": "#B8860B"
    }
}

PRODUKTE = [
    {
        "name": "Wacholdergold",
        "typ": "Gin",
        "beschreibung": "Frisch, klar, mit feiner Honigrunde. Perfekt für Gin Tonic oder pur.",
        "preis": "€ 39,90",
        "bild": "https://honigspirituosen.at/wp-content/uploads/2024/01/wacholdergold.jpg"
    },
    {
        "name": "Fassgold",
        "typ": "Whisky",
        "beschreibung": "Warm, tief, komplex. Erhältlich mit Blütenhonig, Edelkastanie, Linde oder Sonnenblume.",
        "preis": "€ 44,00",
        "bild": "https://honigspirituosen.at/wp-content/uploads/2024/01/fassgold.jpg"
    },
    {
        "name": "Inselgold",
        "typ": "Rum",
        "beschreibung": "Warm, entspannt, weich. Der Honig macht ihn runder – nicht süßer.",
        "preis": "€ 34,90",
        "bild": "https://honigspirituosen.at/wp-content/uploads/2024/01/inselgold.jpg"
    }
]

SPRACHEN = {
    "de": {
        "produkte": "Unsere Produkte",
        "claim": "Du erwartest Süße – du bekommst Charakter.",
        "kontakt": "Kontakt",
        "website": "Website",
        "kein_likoer": "Kein Likör. Keine dominante Süße.",
        "handgemacht": "Handgemacht in Wien"
    },
    "en": {
        "produkte": "Our Products",
        "claim": "You expect sweetness – you get character.",
        "kontakt": "Contact",
        "website": "Website",
        "kein_likoer": "Not a liqueur. No dominant sweetness.",
        "handgemacht": "Handcrafted in Vienna"
    }
}

def generate_prospekt_html(zielgruppe, sprache="de"):
    """Generiert HTML für das Prospekt"""
    tmpl = PROSPEKT_TEMPLATES.get(zielgruppe, PROSPEKT_TEMPLATES["Feinkost"])
    lang = SPRACHEN.get(sprache, SPRACHEN["de"])
    
    produkte_html = ""
    for p in PRODUKTE:
        produkte_html += f"""
        <div class="produkt">
            <div class="produkt-name">{p['name']}</div>
            <div class="produkt-typ">{p['typ']}</div>
            <div class="produkt-desc">{p['beschreibung']}</div>
            <div class="produkt-preis">{p['preis']}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,600;1,400&family=Lato:wght@300;400&display=swap');
  
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  
  body {{
    font-family: 'Lato', sans-serif;
    background: #fff;
    color: #1a1a1a;
    width: 210mm;
    min-height: 297mm;
    padding: 0;
  }}
  
  .header {{
    background: #1a1a1a;
    color: #fff;
    padding: 40px 50px;
    position: relative;
  }}
  
  .header-gold {{
    color: #B8860B;
    font-family: 'Cormorant Garamond', serif;
    font-size: 11px;
    letter-spacing: 3px;
    text-transform: uppercase;
    margin-bottom: 8px;
  }}
  
  .header h1 {{
    font-family: 'Cormorant Garamond', serif;
    font-size: 36px;
    font-weight: 300;
    line-height: 1.2;
    margin-bottom: 8px;
  }}
  
  .header h2 {{
    font-size: 13px;
    font-weight: 300;
    color: #aaa;
    letter-spacing: 1px;
  }}
  
  .honey-bar {{
    height: 4px;
    background: linear-gradient(90deg, #B8860B, #D4A843, #B8860B);
  }}
  
  .content {{
    padding: 40px 50px;
  }}
  
  .intro {{
    font-size: 14px;
    line-height: 1.8;
    color: #444;
    margin-bottom: 30px;
    border-left: 3px solid #B8860B;
    padding-left: 16px;
  }}
  
  .claim {{
    font-family: 'Cormorant Garamond', serif;
    font-size: 22px;
    font-style: italic;
    color: #B8860B;
    text-align: center;
    margin: 30px 0;
    padding: 20px;
    border-top: 1px solid #e0d8c8;
    border-bottom: 1px solid #e0d8c8;
  }}
  
  .produkte-titel {{
    font-family: 'Cormorant Garamond', serif;
    font-size: 20px;
    font-weight: 400;
    margin-bottom: 20px;
    color: #1a1a1a;
  }}
  
  .produkte-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 30px;
  }}
  
  .produkt {{
    border: 1px solid #e0d8c8;
    border-radius: 4px;
    padding: 16px;
    background: #faf8f4;
  }}
  
  .produkt-name {{
    font-family: 'Cormorant Garamond', serif;
    font-size: 18px;
    font-weight: 400;
    margin-bottom: 2px;
  }}
  
  .produkt-typ {{
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #B8860B;
    margin-bottom: 8px;
  }}
  
  .produkt-desc {{
    font-size: 12px;
    color: #666;
    line-height: 1.6;
    margin-bottom: 10px;
  }}
  
  .produkt-preis {{
    font-family: 'Cormorant Garamond', serif;
    font-size: 16px;
    color: #1a1a1a;
    font-weight: 600;
  }}
  
  .kein-likoer {{
    background: #1a1a1a;
    color: #B8860B;
    padding: 12px 20px;
    text-align: center;
    font-size: 12px;
    letter-spacing: 2px;
    text-transform: uppercase;
    margin-bottom: 30px;
    border-radius: 2px;
  }}
  
  .cta {{
    background: #faf8f4;
    border: 1px solid #e0d8c8;
    border-radius: 4px;
    padding: 20px;
    margin-bottom: 30px;
    font-size: 14px;
    color: #444;
    line-height: 1.7;
  }}
  
  .footer {{
    border-top: 1px solid #e0d8c8;
    padding: 20px 0 0;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
  }}
  
  .footer-brand {{
    font-family: 'Cormorant Garamond', serif;
    font-size: 18px;
    color: #1a1a1a;
  }}
  
  .footer-brand span {{
    color: #B8860B;
    font-style: italic;
  }}
  
  .footer-kontakt {{
    font-size: 11px;
    color: #888;
    text-align: right;
    line-height: 1.8;
  }}
  
  .handgemacht {{
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #B8860B;
    margin-top: 4px;
  }}
</style>
</head>
<body>

<div class="header">
  <div class="header-gold">🍯 Honigspirituosen Josef Mayer · Wien</div>
  <h1>{tmpl['titel']}</h1>
  <h2>{tmpl['untertitel']}</h2>
</div>

<div class="honey-bar"></div>

<div class="content">
  
  <div class="intro">
    {tmpl['ansprache']},<br><br>
    {tmpl['intro']}
  </div>
  
  <div class="claim">„{lang['claim']}"</div>
  
  <div class="produkte-titel">{lang['produkte']}</div>
  
  <div class="produkte-grid">{produkte_html}</div>
  
  <div class="kein-likoer">{lang['kein_likoer']} · {lang['handgemacht']}</div>
  
  <div class="cta">{tmpl['cta']}</div>
  
  <div class="footer">
    <div>
      <div class="footer-brand">Honigspirituosen <span>Josef Mayer</span></div>
      <div class="handgemacht">{lang['handgemacht']}</div>
    </div>
    <div class="footer-kontakt">
      info@honigspirituosen.at<br>
      www.honigspirituosen.at<br>
      Wien, Österreich
    </div>
  </div>

</div>

</body>
</html>"""
    return html

@app.route("/prospekt", methods=["POST"])
def create_prospekt():
    """Erstellt ein Prospekt als HTML (druckfertig)"""
    data = request.json or {}
    zielgruppe = data.get("zielgruppe", "Feinkost")
    sprache = data.get("sprache", "de")
    
    html = generate_prospekt_html(zielgruppe, sprache)
    
    return jsonify({
        "success": True,
        "html": html,
        "zielgruppe": zielgruppe,
        "sprache": sprache
    })

@app.route("/prospekt/preview/<zielgruppe>", methods=["GET"])
def prospekt_preview(zielgruppe):
    """Zeigt Prospekt direkt im Browser an"""
    sprache = request.args.get("lang", "de")
    html = generate_prospekt_html(zielgruppe, sprache)
    from flask import Response
    return Response(html, mimetype='text/html')


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
