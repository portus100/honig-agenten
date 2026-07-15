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
# service_role umgeht RLS (liegt geheim in Render). Fallback auf anon, falls nicht gesetzt.
SUPABASE_KEY     = os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_ANON_KEY", "")
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

def sb_delete(table, filter_str):
    try:
        r = requests.delete(
            f"{SUPABASE_URL}/rest/v1/{table}?{filter_str}",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}"
            },
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

def call_claude_websearch(system_prompt, user_message, max_searches=5):
    """Claude mit aktivierter Web-Suche. Für Recherche-Aufgaben (Märkte etc).
    Kostet ~1 Cent pro Suche zusätzlich. Gibt den finalen Text zurück."""
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
                "max_tokens": 4000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
                "tools": [{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": max_searches
                }]
            },
            timeout=120  # Web-Suche dauert deutlich länger
        )
        data = r.json()
        if not r.ok:
            return f"Claude Websearch Fehler: {data}"
        # Antwort kann mehrere Text-Blöcke enthalten (zwischen Suchen) – alle zusammenfügen
        texte = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                texte.append(block.get("text", ""))
        return "\n".join(texte)
    except Exception as e:
        return f"Claude Websearch Fehler: {str(e)}"

# ── GPT-4o (E-Mail schreiben) ──
def call_gpt4(prompt, max_tokens=400):
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
                "max_tokens": max_tokens
            },
            timeout=30
        )
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        return f"GPT Fehler: {str(e)}"


# ════════════════════════════════════════
# BRAND MEMORY – zentrale Markenregeln für alle Textagenten
# ════════════════════════════════════════

# Startwerte – Melli ergänzt/ändert das später im Dashboard
BRAND_DEFAULTS = {
    "positioning": (
        "Premium-Honigspirituosen aus Wien, handwerklich vom Berufsimker Josef Mayer. "
        "Drei Produkte: Wacholdergold (Gin), Fassgold (Whisky), Inselgold (Rum) – alle mit eigenem Honig veredelt. "
        "Honig ist VEREDELUNG, kein Süßungsversprechen. KEIN Likör. "
        "Positionierung: hochwertig aber nahbar, echtes Handwerk statt Marketing-Glanz. "
        "Claim: 'Du erwartest Süße – du bekommst Charakter.'"
    ),
    "preferred_language": (
        "Bevorzugte Begriffe: Charakter, Handwerk, Veredelung, Berufsimker, eigener Honig, "
        "Vom Stock ins Glas, komplex, fein, Wiener Handwerk, Blütenhonig, Charakter statt Kompromiss. "
        "Ton: authentisch, persönlich (Josef spricht als Imker in der Ich-Form), hochwertig aber nicht abgehoben. "
        "Keine Werbefloskeln, kein übertriebenes Marketing-Deutsch."
    ),
    "forbidden_phrases": (
        "VERBOTEN (Recht/EU): bekömmlich, gesund, tut gut, wohltuend, heilsam, therapeutisch, "
        "entspannt, gegen Stress, stärkend, belebend – keine gesundheitsbezogenen oder Wirkungs-Aussagen. "
        "VERBOTEN (Spirituosenkodex): Aufforderung zum Trinken, übermäßiger Konsum, Alkohol als Problemlöser, "
        "Alkohol + Autofahren/Maschinen, Erfolg/Leistung durch Alkohol, Ansprache von unter 18-Jährigen. "
        "VERBOTEN (Marke): Likör, süß als Verkaufsargument, billig, Schnaps (abwertend)."
    ),
    "compliance_rules": (
        "Österreich Spirituosenwerbung (Werberat + Spirituosenkodex), gilt auch online/Social Media: "
        "1) Keine gesundheitsbezogenen Angaben (EU-Recht ab 1,2% Alkohol). "
        "2) Keine therapeutische/stimulierende/konfliktlösende Wirkung suggerieren. "
        "3) Nicht zu übermäßigem Konsum ermutigen. "
        "4) Nicht an Kinder/Jugendliche richten – Zielgruppe immer ab 18 (Ads besser ab 25). "
        "5) Kein Alkohol-Kontext mit Fahrzeuglenken/Maschinen. "
        "6) Alkohol nicht als Erfolgs-/Leistungssteigerung darstellen. "
        "7) Keine verharmlosenden Darstellungen."
    ),
    "proof_requirements": (
        "Belegbar bleiben: 'eigener Honig' und 'Berufsimker' sind echt – darauf darf man sich stützen. "
        "Keine erfundenen Auszeichnungen, keine erfundenen Bewertungen, keine erfundenen Mengen/Zahlen. "
        "Preise nur nennen wenn aktuell bekannt. Alkoholgehalt nur wenn korrekt."
    ),
    "channel_preferences": (
        "Instagram Feed: emotional, bildstark, 2-4 Sätze, Story-Charakter. "
        "Instagram Story: sehr kurz, 1 Satz + Call-to-Action. "
        "Facebook: erzählend, persönlich, etwas ausführlicher. "
        "LinkedIn: professionell, Fokus Handwerk/Qualität/Unternehmertum, B2B-tauglich, weniger Hashtags. "
        "B2B-E-Mail (Wien Scanner): sachlich, respektvoll, kurz, konkretes Terminangebot. "
        "Prospekt: hochwertig, informativ, Imker-Geschichte im Zentrum."
    )
}

BRAND_FIELDS = ["positioning", "preferred_language", "forbidden_phrases",
                "compliance_rules", "proof_requirements", "channel_preferences"]

def lade_brand_rules():
    """Lädt die Markenregeln aus Supabase. Fällt auf Defaults zurück wenn leer."""
    rows = sb_get("brand_rules", "select=*&id=eq.1")
    if rows and isinstance(rows, list) and len(rows) > 0:
        row = rows[0]
        # Fehlende Felder mit Defaults auffüllen
        return {f: (row.get(f) or BRAND_DEFAULTS[f]) for f in BRAND_FIELDS}
    return dict(BRAND_DEFAULTS)

def brand_kontext(kanal=None):
    """Baut einen Text-Block mit den Markenregeln für den System-Prompt eines Agenten.
    Optional kann ein Kanal hervorgehoben werden (z.B. 'Instagram Feed')."""
    r = lade_brand_rules()
    block = (
        "\n\n═══ MARKENREGELN (verbindlich für alle Texte) ═══\n"
        f"POSITIONIERUNG: {r['positioning']}\n\n"
        f"BEVORZUGTE SPRACHE: {r['preferred_language']}\n\n"
        f"VERBOTENE BEGRIFFE/AUSSAGEN: {r['forbidden_phrases']}\n\n"
        f"RECHTLICHE REGELN (UNBEDINGT EINHALTEN): {r['compliance_rules']}\n\n"
        f"BELEGBARKEIT: {r['proof_requirements']}\n\n"
        f"KANAL-TONALITÄT: {r['channel_preferences']}\n"
        "════════════════════════════════════════\n"
    )
    return block

@app.route("/brand", methods=["GET"])
def brand_get():
    """Gibt die aktuellen Markenregeln zurück (für den Brand-Tab)."""
    return jsonify({"success": True, "rules": lade_brand_rules()})

@app.route("/brand", methods=["POST"])
def brand_save():
    """Speichert die Markenregeln. Upsert auf Zeile id=1."""
    data = request.json or {}
    payload = {"id": 1}
    for f in BRAND_FIELDS:
        if f in data:
            payload[f] = data[f]

    # Existiert Zeile 1 schon?
    existing = sb_get("brand_rules", "select=id&id=eq.1")
    if existing and len(existing) > 0:
        ok = sb_update("brand_rules", "id=eq.1", payload)
    else:
        ok = sb_insert("brand_rules", payload)

    return jsonify({"success": bool(ok), "rules": lade_brand_rules()})

@app.route("/brand/reset", methods=["POST"])
def brand_reset():
    """Setzt die Markenregeln auf die Startwerte zurück."""
    payload = {"id": 1}
    payload.update(BRAND_DEFAULTS)
    existing = sb_get("brand_rules", "select=id&id=eq.1")
    if existing and len(existing) > 0:
        ok = sb_update("brand_rules", "id=eq.1", payload)
    else:
        ok = sb_insert("brand_rules", payload)
    return jsonify({"success": bool(ok), "rules": lade_brand_rules()})


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

def search_places(query, location, max_results=10, pages=1):
    """Google Places Textsuche. pages=1..3 holt bis zu 3 Seiten (max ~60 Treffer).
    Details (Website/Telefon/Öffnungszeiten) werden pro Treffer nachgeladen."""
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
    params = {"query": f"{query} {location}", "key": GOOGLE_API_KEY, "language": "de", "region": "at"}
    results = []
    seen = set()
    try:
        for seite in range(max(1, min(pages, 3))):
            r = requests.get(url, params=params, timeout=10)
            data = r.json()
            for place in data.get("results", []):
                if len(results) >= max_results and pages == 1:
                    break
                pid = place.get("place_id", "")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                details = get_place_details(pid)
                geo = place.get("geometry", {}).get("location", {})
                results.append({
                    "name": place.get("name", ""),
                    "address": place.get("formatted_address", ""),
                    "rating": place.get("rating", 0),
                    "place_id": pid,
                    "lat": geo.get("lat"),
                    "lng": geo.get("lng"),
                    "website": details.get("website", ""),
                    "phone": details.get("phone", ""),
                    "opening_hours": details.get("opening_hours", [])
                })
            # nächste Seite vorbereiten
            token = data.get("next_page_token")
            if not token or seite >= pages - 1:
                break
            import time as _t
            _t.sleep(2)  # Google braucht kurz, bis der Token gültig ist
            params = {"pagetoken": token, "key": GOOGLE_API_KEY}
        if pages == 1:
            results = results[:max_results]
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

def parse_opening_hours(weekday_text):
    """Wandelt Google's weekday_text in {wochentag_index: [(open_min, close_min), ...]} um.
    weekday_text z.B.: ['Montag: 17:00–02:00', 'Dienstag: Geschlossen', ...]"""
    day_map = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
               "freitag": 4, "samstag": 5, "sonntag": 6}
    result = {}
    for line in (weekday_text or []):
        if ":" not in line:
            continue
        tag_teil, _, zeit_teil = line.partition(":")
        tag_idx = day_map.get(tag_teil.strip().lower())
        if tag_idx is None:
            continue
        zeit_teil = zeit_teil.strip()
        if "geschlossen" in zeit_teil.lower() or "closed" in zeit_teil.lower():
            result[tag_idx] = []
            continue
        spans = []
        # Mehrere Zeitspannen mit Komma getrennt möglich
        for span in zeit_teil.split(","):
            # Verschiedene Bindestrich-Varianten normalisieren
            s = span.replace("–", "-").replace("—", "-").strip()
            if "-" not in s:
                continue
            a, _, b = s.partition("-")
            try:
                ah, am = [int(x) for x in a.strip().split(":")[:2]]
                bh, bm = [int(x) for x in b.strip().split(":")[:2]]
                open_min = ah * 60 + am
                close_min = bh * 60 + bm
                if close_min <= open_min:  # über Mitternacht
                    close_min += 24 * 60
                spans.append((open_min, close_min))
            except (ValueError, IndexError):
                continue
        result[tag_idx] = spans
    return result

def ist_offen(opening_map, weekday_idx, hour, minute):
    """Prüft ob an weekday_idx um hour:minute offen ist. Leere Map = unbekannt = erlauben."""
    if not opening_map or weekday_idx not in opening_map:
        return True  # keine Info -> nicht blockieren
    t = hour * 60 + minute
    for open_min, close_min in opening_map[weekday_idx]:
        if open_min <= t <= close_min:
            return True
    return False

def _overlap(a_start, a_end, b_start, b_end):
    """Schnittmenge zweier Zeitintervalle (in Minuten). None wenn keine Überschneidung."""
    s = max(a_start, b_start)
    e = min(a_end, b_end)
    return (s, e) if s < e else None

def calculate_appointments(verfuegbarkeit, opening_hours=None, belegte_slots=None,
                           wochen=3, termin_dauer=60, vorschlaege_pro_fenster=3):
    """Erzeugt Terminvorschläge: Verfügbarkeitsfenster ∩ Öffnungszeiten, gleichmäßig verteilt.

    - verfuegbarkeit: Liste von {datum:'YYYY-MM-DD', von:'HH:MM', bis:'HH:MM'}
      (autonome Tagesfenster des Partners, mehrere pro Tag möglich)
    - opening_hours: Google weekday_text des Leads (Termin nur wenn offen)
    - belegte_slots: bereits vergebene 'YYYY-MM-DD HH:MM'
    Fällt verfuegbarkeit leer aus, wird auf Mo–Fr Standardfenster zurückgegriffen.
    """
    opening_map = parse_opening_hours(opening_hours) if opening_hours else {}
    belegt = set(belegte_slots or [])

    def hhmm_to_min(s):
        try:
            h, m = [int(x) for x in s.split(":")[:2]]
            return h * 60 + m
        except Exception:
            return None

    # Rückwärtskompatibel: wenn eine Liste von Wochentags-NAMEN kommt
    # (altes Haupt-Dashboard), in Standardfenster 11–19 für die nächsten 3 Wochen wandeln.
    if verfuegbarkeit and isinstance(verfuegbarkeit[0], str):
        day_map = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
                   "freitag": 4, "samstag": 5, "sonntag": 6}
        gewuenscht = set(day_map[d.lower()] for d in verfuegbarkeit if d.lower() in day_map)
        umgewandelt = []
        cur = datetime.now() + timedelta(days=1)
        for _ in range(wochen * 7):
            if cur.weekday() in gewuenscht:
                umgewandelt.append({"datum": cur.strftime("%Y-%m-%d"), "von": "11:00", "bis": "19:00"})
            cur += timedelta(days=1)
        verfuegbarkeit = umgewandelt

    # Fallback: keine Verfügbarkeit eingetragen -> Mo–Fr 11–19 Uhr für die nächsten 3 Wochen
    if not verfuegbarkeit:
        verfuegbarkeit = []
        cur = datetime.now() + timedelta(days=1)
        for _ in range(wochen * 7):
            if cur.weekday() < 5:
                verfuegbarkeit.append({
                    "datum": cur.strftime("%Y-%m-%d"), "von": "11:00", "bis": "19:00"
                })
            cur += timedelta(days=1)

    slots = []
    for fenster in verfuegbarkeit:
        datum = fenster.get("datum", "")
        von = hhmm_to_min(fenster.get("von", ""))
        bis = hhmm_to_min(fenster.get("bis", ""))
        if not datum or von is None or bis is None or bis <= von:
            continue
        try:
            d = datetime.strptime(datum, "%Y-%m-%d")
        except ValueError:
            continue
        wd = d.weekday()

        # Öffnungszeiten dieses Wochentags
        offen_spans = opening_map.get(wd, None)
        # Keine Info -> ganzes Fenster gilt als offen
        if offen_spans is None:
            nutzbare = [(von, bis)]
        elif len(offen_spans) == 0:
            nutzbare = []  # geschlossen
        else:
            nutzbare = []
            for (os_, oe_) in offen_spans:
                ov = _overlap(von, bis, os_, oe_)
                if ov:
                    nutzbare.append(ov)

        for (start, ende) in nutzbare:
            spanne = ende - start
            if spanne < termin_dauer:
                continue
            # Gleichmäßig verteilen: n Vorschläge im nutzbaren Fenster
            n = min(vorschlaege_pro_fenster, max(1, spanne // termin_dauer))
            if n == 1:
                punkte = [start]
            else:
                schritt = (spanne - termin_dauer) / (n - 1) if n > 1 else 0
                punkte = [int(start + i * schritt) for i in range(n)]
            for p in punkte:
                # auf 5 Minuten runden
                p = int(round(p / 5) * 5)
                hh, mm = divmod(p, 60)
                key = f"{datum} {hh:02d}:{mm:02d}"
                if key in belegt:
                    continue
                slots.append(d.strftime(f"%A, %d.%m.%Y") + f" um {hh:02d}:{mm:02d} Uhr")

    return slots[:15]

import math

def distanz_meter(lat1, lng1, lat2, lng2):
    """Luftlinie zwischen zwei Punkten in Metern (Haversine). Gratis, ohne API."""
    if None in (lat1, lng1, lat2, lng2):
        return 999999
    R = 6371000  # Erdradius in Metern
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def cluster_leads(leads, radius_m=800):
    """Gruppiert Leads nach Nähe (Luftlinie). Greedy: nimm einen Lead, sammle alle
    im Radius, bilde Cluster, weiter mit dem nächsten ungenutzten. Sortiert so, dass
    dichte Cluster (viele nahe Leads) zuerst kommen."""
    rest = [l for l in leads if l.get("lat") is not None and l.get("lng") is not None]
    ohne_geo = [l for l in leads if l.get("lat") is None or l.get("lng") is None]
    cluster = []
    benutzt = set()
    for i, l in enumerate(rest):
        if i in benutzt:
            continue
        gruppe = [l]
        benutzt.add(i)
        for j, m in enumerate(rest):
            if j in benutzt:
                continue
            if distanz_meter(l["lat"], l["lng"], m["lat"], m["lng"]) <= radius_m:
                gruppe.append(m)
                benutzt.add(j)
        cluster.append(gruppe)
    # Dichteste Cluster zuerst
    cluster.sort(key=len, reverse=True)
    # Flach zurückgeben: erst dichte Cluster, dann Geo-lose Leads ans Ende
    sortiert = []
    for g in cluster:
        sortiert.extend(g)
    sortiert.extend(ohne_geo)
    return sortiert

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
[E-Mail Text]""" + brand_kontext()
    
    return call_gpt4(prompt)

def write_gift_email(business, zielgruppe):
    """Individuelle B2B-Weihnachtsgeschenk-Mail pro Firma."""
    contact = business.get("contact_person") or ""
    if contact:
        salutation = f"Sehr geehrte/r {contact},"
        weiterleit = ""
    else:
        salutation = "Sehr geehrte Damen und Herren,"
        weiterleit = ("- Falls du nicht die richtige Ansprechperson für Firmengeschenke bist, "
                      "bitte höflich um Weiterleitung an die zuständige Person.")
    prompt = f"""Schreibe eine kurze, hochwertige B2B-E-Mail für Josef Mayer von "Honigspirituosen Josef Mayer".
Thema: personalisierte Weihnachtsgeschenke für Kunden oder Mitarbeiter.

Empfänger: {business.get('name','')} – Branche: {zielgruppe}
Anrede: {salutation}

Angebot:
- Honigveredelte Premium-Spirituosen (Gin, Whisky, Rum) als Firmengeschenk
- Personalisierbar mit dem LOGO DES KUNDEN vorne auf dem Etikett (die Marke des Empfängers steht im Vordergrund, nicht meine)
- Handgegossene Bienenwachskerze aus eigener Imkerei als Beilage – regional, handgemacht, einzigartig
- Zwei Linien: Einzelflasche (als Werbegeschenk bis 40€ netto ggf. steuerlich absetzbar) und hochwertiges Geschenk-Set
- Frühe Bestellung nötig, da alles handgefertigt wird

Anforderungen:
- Max 6 Sätze, edel und persönlich, kein Werbe-Blabla
- Betonung: regional, handgemacht, personalisiert mit IHREM Logo
- Hinweis auf frühen Bestellschluss (Kerzen handgefertigt)
- Bitte um kurzes Gespräch / Musteranfrage
{weiterleit}
- Signatur: Mit freundlichen Grüßen\\nJosef Mayer\\nHonigspirituosen Josef Mayer\\nwww.honigspirituosen.at

Format:
BETREFF: [Betreff]
---
[E-Mail Text]""" + brand_kontext()
    return call_gpt4(prompt)

@app.route("/geschenk/scan", methods=["POST"])
def geschenk_scan():
    """Scannt B2B-Geschenk-Zielgruppen OHNE Limit, speichert dauerhaft in geschenk_leads."""
    data = request.json or {}
    zielgruppen = data.get("zielgruppen") or list(B2B_GESCHENK_ZIELGRUPPEN.keys())
    bezirk = data.get("bezirk", "Wien")

    bestehend = set()
    for row in (sb_get("geschenk_leads", "select=place_id") or []):
        if row.get("place_id"):
            bestehend.add(row["place_id"])

    neu = 0
    seen = set()
    for zg in zielgruppen:
        begriffe = B2B_GESCHENK_ZIELGRUPPEN.get(zg, [zg])
        for q in begriffe:
            for p in search_places(q, bezirk, max_results=60, pages=3):
                pid = p.get("place_id", "")
                if not pid or pid in seen or pid in bestehend:
                    continue
                seen.add(pid)
                analysis = analyze_website(p.get("website", ""))
                betreff, text = "", ""
                lead = {
                    "name": p["name"],
                    "address": p["address"],
                    "place_id": pid,
                    "website": p.get("website", ""),
                    "phone": p.get("phone", ""),
                    "contact_email": analysis.get("contact_email", ""),
                    "contact_person": analysis.get("contact_person", ""),
                    "zielgruppe": zg,
                    "bezirk": bezirk,
                    "rating": p.get("rating", 0),
                    "email_betreff": "",
                    "email_draft": "",
                    "mail_status": "entwurf",
                    "created_at": datetime.now().isoformat()
                }
                sb_insert("geschenk_leads", lead)
                neu += 1
    return jsonify({"success": True, "neu": neu, "zielgruppen": zielgruppen, "bezirk": bezirk})

@app.route("/geschenk/import", methods=["POST"])
def geschenk_import():
    """Nimmt kopierten WKO-Text, zerlegt via GPT in Firmen UND erkennt je Firma die Branche.
    Ordnet automatisch der passenden Zielgruppe zu. Keine Vorab-Auswahl nötig."""
    data = request.json or {}
    roh = (data.get("text") or "").strip()
    bezirk = data.get("bezirk", "Wien")
    # Optionale manuelle Vorgabe (überschreibt die Auto-Erkennung, falls gesetzt)
    fixe_zielgruppe = (data.get("zielgruppe") or "").strip()
    if len(roh) < 20:
        return jsonify({"success": False, "error": "Zu wenig Text"})

    # Nur WKO- und Maps-Links entfernen — echte Firmen-Websites bleiben erhalten.
    import re as _re
    text = roh
    text = _re.sub(r'\[([^\]]*)\]\((?:tel:|mailto:)?[^)]*\)', r'\1', text)     # Markdown-Links -> Text
    text = _re.sub(r'https?://(?:firmen\.wko\.at|maps\.google\.com)\S+', '', text)  # nur WKO/Maps-URLs
    text = _re.sub(r'Route planen', '', text)
    text = _re.sub(r'\n{2,}', '\n', text)
    roh = text.strip()

    kategorien = list(B2B_GESCHENK_ZIELGRUPPEN.keys())
    kat_liste = ", ".join(kategorien)
    prompt = f"""Aus dem folgenden Text eines Firmenverzeichnisses (WKO Firmen A-Z) extrahiere ALLE Firmen.
Ordne jede Firma EINER dieser Kategorien zu (nutze exakt diese Schreibweise):
{kat_liste}, Sonstige

Bestimme außerdem den Wiener Bezirk aus der Postleitzahl in der Adresse:
- Wiener PLZ haben das Format 1XX0, wobei XX der Bezirk ist (z.B. 1010=1010 Wien, 1090=1090 Wien, 1230=1230 Wien).
- Schreibe ins Feld "bezirk" z.B. "1090 Wien". Ist keine Wiener PLZ erkennbar, schreibe den Ort oder leer.

Gib NUR ein JSON-Array zurück, kein anderer Text. Format je Firma:
{{"name":"", "address":"", "phone":"", "website":"", "contact_email":"", "contact_person":"", "kategorie":"", "bezirk":""}}
- "kategorie" = eine der oben genannten. Passt keine, nimm "Sonstige".
- Fehlende Felder als leerer String. Keine erfundenen Daten.

TEXT:
{roh[:12000]}"""
    antwort = call_gpt4(prompt, max_tokens=4000)
    firmen = []
    try:
        s = antwort.find("["); e = antwort.rfind("]")
        if s >= 0 and e > s:
            firmen = json.loads(antwort[s:e+1])
    except Exception:
        try:
            for m in _re.finditer(r'\{[^{}]*\}', antwort):
                try:
                    firmen.append(json.loads(m.group(0)))
                except Exception:
                    pass
        except Exception:
            firmen = []
    if not firmen:
        return jsonify({"success": False, "error": "Konnte Text nicht zerlegen — bitte kleinere Portion einfuegen"})

    bestehend = set()
    for row in (sb_get("geschenk_leads", "select=name,address") or []):
        schluessel = (row.get("name") or "").lower().strip() + "|" + (row.get("address") or "").lower().replace(" ", "")
        bestehend.add(schluessel)

    neu = 0
    pro_kategorie = {}
    gesehen = set()  # innerhalb dieses Imports
    for f in firmen:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        # Dublette nur, wenn Name UND Adresse identisch (echte Mehrfacheinträge derselben Firma)
        schluessel = name.lower() + "|" + (f.get("address") or "").lower().replace(" ", "")
        if schluessel in bestehend or schluessel in gesehen:
            continue
        if fixe_zielgruppe:
            kategorie = fixe_zielgruppe  # vom Nutzer bewusst gesetzt (auch eigene Branche erlaubt)
        else:
            kategorie = (f.get("kategorie") or "Sonstige").strip()
            if kategorie not in kategorien and kategorie != "Sonstige":
                kategorie = "Sonstige"
        # KEIN Website-Scan hier (zu langsam bei Masse) — nur WKO-Daten übernehmen.
        # Der Ansprechpartner-Scan passiert später bei der Mail-Generierung.
        lead = {
            "name": name,
            "address": f.get("address", ""),
            "place_id": "",
            "website": f.get("website", ""),
            "phone": f.get("phone", ""),
            "contact_email": f.get("contact_email", ""),
            "contact_person": f.get("contact_person", ""),
            "zielgruppe": kategorie,
            "bezirk": (f.get("bezirk") or "").strip() or bezirk,
            "rating": 0,
            "email_betreff": "",
            "email_draft": "",
            "mail_status": "entwurf",
            "created_at": datetime.now().isoformat()
        }
        sb_insert("geschenk_leads", lead)
        bestehend.add(schluessel)
        gesehen.add(schluessel)
        pro_kategorie[kategorie] = pro_kategorie.get(kategorie, 0) + 1
        neu += 1
    return jsonify({"success": True, "erkannt": len(firmen), "neu": neu, "pro_kategorie": pro_kategorie})

@app.route("/geschenk/leads", methods=["GET"])
def geschenk_leads_liste():
    status = request.args.get("status", "")
    zielgruppe = request.args.get("zielgruppe", "")
    bezirk = request.args.get("bezirk", "")
    filt = ""
    if status:     filt += f"&mail_status=eq.{status}"
    if zielgruppe: filt += f"&zielgruppe=eq.{zielgruppe}"
    if bezirk:     filt += f"&bezirk=eq.{bezirk}"
    rows = sb_get("geschenk_leads", f"select=*{filt}&order=zielgruppe.asc,created_at.desc")
    return jsonify({"success": True, "leads": rows or []})

@app.route("/geschenk/mails-generieren", methods=["POST"])
def geschenk_mails_generieren():
    """Erzeugt für Entwurfs-Leads individuelle Mails. Scannt dabei die Website
    nach Ansprechpartner (nur hier, nicht beim Massen-Import)."""
    rows = sb_get("geschenk_leads", "select=*&email_draft=eq.&order=created_at.asc") or []
    erzeugt = 0
    for lead in rows[:15]:  # kleinerer Batch, da jetzt Website-Scan dazukommt
        # Ansprechpartner/E-Mail von Website holen, falls noch nicht vorhanden
        if lead.get("website") and not lead.get("contact_person"):
            analysis = analyze_website(lead["website"])
            neu_person = analysis.get("contact_person", "")
            neu_mail = lead.get("contact_email") or analysis.get("contact_email", "")
            if neu_person or neu_mail:
                sb_update("geschenk_leads", f"id=eq.{lead['id']}",
                          {"contact_person": neu_person, "contact_email": neu_mail})
                lead["contact_person"] = neu_person
                lead["contact_email"] = neu_mail
        roh = write_gift_email(lead, lead.get("zielgruppe", ""))
        betreff, text = "", roh
        if "BETREFF:" in roh and "---" in roh:
            try:
                betreff = roh.split("BETREFF:")[1].split("---")[0].strip()
                text = roh.split("---", 1)[1].strip()
            except Exception:
                pass
        sb_update("geschenk_leads", f"id=eq.{lead['id']}",
                  {"email_betreff": betreff, "email_draft": text})
        erzeugt += 1
    return jsonify({"success": True, "erzeugt": erzeugt, "verbleibend": max(0, len(rows) - erzeugt)})

@app.route("/geschenk/freigeben", methods=["POST"])
def geschenk_freigeben():
    """Gibt Leads frei (einzeln per id, oder alle einer Zielgruppe)."""
    data = request.json or {}
    if data.get("id"):
        sb_update("geschenk_leads", f"id=eq.{data['id']}", {"mail_status": "freigegeben"})
    elif data.get("zielgruppe"):
        sb_update("geschenk_leads",
                  f"zielgruppe=eq.{data['zielgruppe']}&mail_status=eq.entwurf",
                  {"mail_status": "freigegeben"})
    elif data.get("alle"):
        sb_update("geschenk_leads", "mail_status=eq.entwurf", {"mail_status": "freigegeben"})
    return jsonify({"success": True})

@app.route("/geschenk/senden", methods=["POST"])
def geschenk_senden():
    """Sammelversand: alle freigegebenen Leads mit E-Mail-Adresse via Make raus."""
    rows = sb_get("geschenk_leads",
        "select=*&mail_status=eq.freigegeben&contact_email=neq.&order=created_at.asc") or []
    gesendet = 0
    for lead in rows:
        if MAKE_WEBHOOK_URL:
            try:
                requests.post(MAKE_WEBHOOK_URL, json={
                    "action": "send_email",
                    "betreff": lead.get("email_betreff", ""),
                    "email": lead.get("email_draft", ""),
                    "recipient": lead.get("contact_email", ""),
                    "timestamp": datetime.now().isoformat()
                }, timeout=10)
            except Exception:
                continue
        sb_update("geschenk_leads", f"id=eq.{lead['id']}",
                  {"mail_status": "gesendet", "gesendet_am": datetime.now().isoformat()})
        gesendet += 1
    return jsonify({"success": True, "gesendet": gesendet})

@app.route("/geschenk/zielgruppen", methods=["GET"])
def geschenk_zielgruppen():
    return jsonify({"success": True, "zielgruppen": list(B2B_GESCHENK_ZIELGRUPPEN.keys())})

# ════════════════════════════════════════
# B2B-GESCHENK ZIELGRUPPEN (zentral, mandantenfähig-freundlich)
# Jede Zielgruppe: Suchbegriffe für Google Places.
# Neue Branche = neue Zeile, kein Code-Umbau.
# ════════════════════════════════════════
B2B_GESCHENK_ZIELGRUPPEN = {
    "Kanzleien & Notare":      ["Rechtsanwalt", "Notar", "Anwaltskanzlei"],
    "Steuerberatung":          ["Steuerberater", "Wirtschaftsprüfer", "Steuerkanzlei"],
    "Vermögensberatung":       ["Vermögensberatung", "Finanzberatung", "Private Banking", "Vermögensberater"],
    "Versicherungen":          ["Versicherungsbüro", "Versicherungsmakler", "Versicherungsagentur"],
    "Immobilien":              ["Immobilienmakler", "Immobilienbüro"],
    "Architektur & Planung":   ["Architekturbüro", "Planungsbüro", "Ingenieurbüro"],
    "Agenturen":               ["Werbeagentur", "PR-Agentur", "Marketingagentur", "IT-Agentur"],
    "Banken (Filialen)":       ["Bankfiliale", "Sparkasse", "Raiffeisenbank"],
    "Hotels & Wellness":       ["Boutique Hotel", "Wellnesshotel", "Spa Hotel"],
    "Weingüter":               ["Weingut", "Winzer", "Vinothek"],
    "Autohäuser (Premium)":    ["Autohaus Premium", "BMW Händler", "Mercedes Händler", "Audi Händler"],
    "Bau & Handwerk (gehoben)":["Bauträger", "Baufirma", "Tischlerei Meisterbetrieb"],
    "Ärzte & Apotheken":       ["Arztpraxis", "Zahnarzt", "Apotheke", "Privatordination"],
    "Kammern & Verbände":      ["Wirtschaftskammer", "Berufsverband", "Innung"],
    "Konzern-Standorte":       ["Siemens", "OMV", "Erste Bank", "Magna", "A1 Telekom", "Rewe", "Spar Zentrale"],
    "Mittelstand":             ["GmbH Produktion", "Handelsunternehmen", "mittelständisches Unternehmen"],
}

@app.route("/scan/import", methods=["POST"])
def scan_import():
    """WKO-Text zerlegen und als Leads ins Haupt-Dashboard (leads-Tabelle) übernehmen."""
    data = request.json or {}
    roh = (data.get("text") or "").strip()
    target_type = data.get("target_type", "Feinkostgeschäft")
    if len(roh) < 20:
        return jsonify({"success": False, "error": "Zu wenig Text"})

    prompt = f"""Aus dem folgenden kopierten Text eines Firmenverzeichnisses (WKO Firmen A-Z) extrahiere alle Firmen.
Gib NUR ein JSON-Array zurück, kein anderer Text. Format je Firma:
{{"name":"", "address":"", "phone":"", "website":"", "contact_email":"", "contact_person":""}}
Fehlende Felder als leerer String. Keine erfundenen Daten.

TEXT:
{roh[:6000]}"""
    antwort = call_gpt4(prompt, max_tokens=4000)
    firmen = []
    try:
        s = antwort.find("["); e = antwort.rfind("]")
        if s >= 0 and e > s:
            firmen = json.loads(antwort[s:e+1])
    except Exception:
        return jsonify({"success": False, "error": "Konnte Text nicht zerlegen, bitte erneut versuchen"})

    appointments = calculate_appointments(["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"])
    bestehend = set()
    for row in (sb_get("leads", "select=name") or []):
        bestehend.add((row.get("name") or "").lower())

    neu = 0
    for f in firmen:
        name = (f.get("name") or "").strip()
        if not name or name.lower() in bestehend:
            continue
        website = f.get("website", "")
        analysis = analyze_website(website) if website else {"score": 0, "signals": [], "contact_email": "", "contact_person": ""}
        business = {"name": name, "address": f.get("address", ""),
                    "contact_person": f.get("contact_person") or analysis.get("contact_person", "")}
        email = write_email(business, analysis, appointments[:3])
        lead = {
            "name": name,
            "address": f.get("address", ""),
            "place_id": "",
            "website": website,
            "phone": f.get("phone", ""),
            "contact_email": f.get("contact_email") or analysis.get("contact_email", ""),
            "contact_person": business["contact_person"],
            "score": analysis.get("score", 0),
            "signals": json.dumps(analysis.get("signals", [])),
            "rating": 0,
            "email_draft": email,
            "appointment_slots": json.dumps(appointments[:3]),
            "target_type": target_type,
            "status": "pending_approval",
            "created_at": datetime.now().isoformat()
        }
        sb_insert("leads", lead)
        bestehend.add(name.lower())
        neu += 1
    return jsonify({"success": True, "erkannt": len(firmen), "neu": neu})

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
        for p in search_places(q, bezirk, max_results=60, pages=3):
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

# Tages-Cache: { "lat,lon": {"datum": "2026-06-11", "daten": {...}} }
# Spart Open-Meteo-Aufrufe – einmal pro Tag pro Ort reicht für einen Imker.
_WETTER_CACHE = {}

def get_wetter(lat, lon):
    """Holt Wetter von Open-Meteo, mit Tages-Cache. Fragt pro Ort nur 1x täglich neu."""
    cache_key = f"{lat},{lon}"
    heute = datetime.now().strftime("%Y-%m-%d")

    # Cache-Treffer? Dann gar nicht erst Open-Meteo fragen.
    cached = _WETTER_CACHE.get(cache_key)
    if cached and cached.get("datum") == heute and cached.get("daten", {}).get("ok"):
        print(f"[WETTER] Cache-Treffer für {cache_key} ({heute})")
        return cached["daten"]

    daten = _wetter_von_api(lat, lon)

    # Nur erfolgreiche Abrufe cachen (Fehler nicht, damit später nochmal versucht wird)
    if daten.get("ok"):
        _WETTER_CACHE[cache_key] = {"datum": heute, "daten": daten}

    return daten

def _wetter_von_api(lat, lon):
    """Roher Open-Meteo-Abruf. KEIN Retry bei 429 (würde nur die Quota verbrennen)."""
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,cloud_cover",
                "timezone": "Europe/Vienna"
            },
            headers={"User-Agent": "Mozilla/5.0 (HonigAgent)"},
            timeout=25
        )
        if not r.ok:
            grund = r.text[:150]
            print(f"[WETTER] HTTP {r.status_code}: {grund}")
            # Bei 429 (Limit) ehrlichen Hinweis mitgeben
            hinweis = "Tageslimit erreicht" if r.status_code == 429 else f"HTTP {r.status_code}"
            return {"temp": None, "code": -1, "beschreibung": "Wetter nicht verfügbar",
                    "cloud_cover": None, "ok": False, "fehler": hinweis}

        data = r.json().get("current", {})
        print(f"[WETTER] OK: {data}")
        temp_raw = data.get("temperature_2m")
        code = data.get("weather_code")
        cloud = data.get("cloud_cover")
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
        fehler = f"{type(e).__name__}: {str(e)[:120]}"
        print(f"[WETTER] Exception: {fehler}")
        return {"temp": None, "code": -1, "beschreibung": "Wetter nicht verfügbar",
                "cloud_cover": None, "ok": False, "fehler": fehler}

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
    """Diagnose: zeigt rohe Wetterdaten + nackten Direkt-Test von Open-Meteo."""
    w = get_wetter(WETTER_ORTE["wien"]["lat"], WETTER_ORTE["wien"]["lon"])

    # Nackter Direkt-Test ohne Verarbeitung – zeigt was Open-Meteo wirklich antwortet
    roh = {}
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast?latitude=48.2082&longitude=16.3738&current=temperature_2m",
            headers={"User-Agent": "Mozilla/5.0 (HonigAgent)"},
            timeout=25
        )
        roh["status_code"] = r.status_code
        roh["antwort"] = r.text[:300]
    except Exception as e:
        roh["exception"] = f"{type(e).__name__}: {str(e)[:200]}"

    return jsonify({"verarbeitet": w, "roh_direkt": roh})

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
# AI VISIBILITY MONITOR (Phase 1 — nur Gemini + Grounding)
# ════════════════════════════════════════
def _iso_woche():
    jahr, kw, _ = datetime.now().isocalendar()
    return f"{jahr}-KW{kw:02d}"

@app.route("/ai-visibility/scan", methods=["POST"])
def ai_visibility_scan():
    """Jede aktive Frage 3x mit Grounding, jede Antwort bewerten, alles speichern.
    Änderung ggü. Vorwoche → Telegram."""
    woche = _iso_woche()
    alle_fragen = sb_get("ai_visibility_queries", "select=*&aktiv=eq.true&order=id.asc") or []
    if not alle_fragen:
        return jsonify({"success": False, "error": "Keine aktiven Fragen"})
    # Nur Fragen, die diese Woche noch KEIN Ergebnis haben (Häppchen gegen Timeout)
    schon_da = sb_get("ai_visibility_results", f"select=query_id&woche=eq.{woche}") or []
    erledigte_ids = set(r["query_id"] for r in schon_da)
    fragen = [f for f in alle_fragen if f["id"] not in erledigte_ids][:3]  # max 3 pro Aufruf
    if not fragen:
        return jsonify({"success": True, "fertig": True, "message": "Alle Fragen dieser Woche erledigt",
                        "woche": woche, "geprueft": 0, "zusammenfassung": [], "aenderungen": []})
    aenderungen = []
    zusammenfassung = []
    for f in fragen:
        nennungen = 0; scores = []; halluzinationen = 0
        for run in (1, 2, 3):
            rohantwort, quellen = call_gemini_grounded(f["frage_text"])
            bew = bewerte_antwort(f["frage_text"], f.get("kategorie", ""), rohantwort, len(quellen))
            if bew.get("marke_genannt"): nennungen += 1
            scores.append(bew.get("score", 0))
            if bew.get("halluzination_flag"): halluzinationen += 1
            sb_insert("ai_visibility_results", {
                "query_id": f["id"], "frage_text": f["frage_text"],
                "kategorie": f.get("kategorie", ""), "prompt_version": f.get("prompt_version", "v1"),
                "run_timestamp": datetime.now().isoformat(), "run_nummer": run, "woche": woche,
                "rohantwort": rohantwort[:4000], "quellen": json.dumps(quellen),
                "quellen_anzahl": len(quellen), "marke_genannt": bool(bew.get("marke_genannt")),
                "score": bew.get("score", 0), "confidence": bew.get("confidence", 0),
                "halluzination_flag": bool(bew.get("halluzination_flag")),
                "halluzination_detail": bew.get("halluzination_detail", "")
            })
        avg = round(sum(scores) / len(scores)) if scores else 0
        zusammenfassung.append({"frage": f["frage_text"], "kategorie": f.get("kategorie", ""),
                                "stabilitaet": f"{nennungen}/3", "score": avg, "halluzinationen": halluzinationen})
        vorwoche = sb_get("ai_visibility_results",
            f"select=marke_genannt&query_id=eq.{f['id']}&woche=neq.{woche}&order=run_timestamp.desc&limit=3")
        if vorwoche:
            vorher = any(r.get("marke_genannt") for r in vorwoche)
            jetzt = nennungen > 0
            if vorher != jetzt:
                aenderungen.append((f"✅ NEU erwähnt bei: {f['frage_text']}" if jetzt
                                    else f"❌ NICHT mehr erwähnt bei: {f['frage_text']}"))
        if halluzinationen > 0:
            aenderungen.append(f"⚠️ Halluzination bei: {f['frage_text']} ({halluzinationen}/3)")
    if aenderungen:
        send_telegram("🔍 AI-Sichtbarkeit – Änderungen diese Woche:\n\n" + "\n".join(aenderungen[:15]))
    return jsonify({"success": True, "woche": woche, "geprueft": len(fragen),
                    "zusammenfassung": zusammenfassung, "aenderungen": aenderungen})

@app.route("/ai-visibility/ergebnisse", methods=["GET"])
def ai_visibility_ergebnisse():
    woche = request.args.get("woche", "") or _iso_woche()
    rows = sb_get("ai_visibility_results",
        f"select=*&woche=eq.{woche}&order=query_id.asc,run_nummer.asc") or []
    proFrage = {}
    for r in rows:
        qid = r["query_id"]
        if qid not in proFrage:
            proFrage[qid] = {"frage": r["frage_text"], "kategorie": r["kategorie"],
                             "runs": [], "nennungen": 0, "scores": [], "halluzinationen": 0, "quellen": []}
        g = proFrage[qid]; g["runs"].append(r)
        if r["marke_genannt"]: g["nennungen"] += 1
        g["scores"].append(r["score"])
        if r["halluzination_flag"]: g["halluzinationen"] += 1
        try: g["quellen"].extend(json.loads(r.get("quellen") or "[]"))
        except: pass
    ergebnis = []
    for qid, g in proFrage.items():
        ergebnis.append({"frage": g["frage"], "kategorie": g["kategorie"],
            "stabilitaet": f"{g['nennungen']}/{len(g['runs'])}",
            "score": round(sum(g["scores"])/len(g["scores"])) if g["scores"] else 0,
            "halluzinationen": g["halluzinationen"], "quellen": g["quellen"][:5],
            "rohantwort": g["runs"][0]["rohantwort"] if g["runs"] else ""})
    gesamt = round(sum(e["score"] for e in ergebnis)/len(ergebnis)) if ergebnis else 0
    return jsonify({"success": True, "woche": woche, "gesamt_score": gesamt, "ergebnisse": ergebnis})

@app.route("/ai-visibility/massnahme", methods=["POST"])
def ai_visibility_massnahme_add():
    data = request.json or {}
    massnahme = (data.get("massnahme") or "").strip()
    if not massnahme:
        return jsonify({"success": False, "error": "Maßnahme fehlt"})
    sb_insert("ai_visibility_massnahmen", {
        "datum": data.get("datum") or datetime.now().strftime("%Y-%m-%d"),
        "massnahme": massnahme,
        "erwartung": (data.get("erwartung") or "").strip(),
        "titel": (data.get("titel") or "").strip(),
        "status": data.get("status") or "idee",
        "prioritaet": data.get("prioritaet") or "",
        "url": (data.get("url") or "").strip()
    })
    return jsonify({"success": True})

@app.route("/ai-visibility/massnahmen", methods=["GET"])
def ai_visibility_massnahmen_liste():
    rows = sb_get("ai_visibility_massnahmen", "select=*&order=datum.desc") or []
    return jsonify({"success": True, "massnahmen": rows})

@app.route("/ai-visibility/massnahme-loeschen", methods=["POST"])
def ai_visibility_massnahme_del():
    data = request.json or {}
    if data.get("id"):
        sb_delete("ai_visibility_massnahmen", f"id=eq.{data['id']}")
    return jsonify({"success": True})

@app.route("/ai-visibility/massnahme-status", methods=["POST"])
def ai_visibility_massnahme_status():
    """Status einer Content-Pipeline-Maßnahme ändern (idee → in_arbeit → live), optional URL nachtragen."""
    data = request.json or {}
    mid = data.get("id")
    if not mid:
        return jsonify({"success": False, "error": "id fehlt"})
    update = {}
    if data.get("status"): update["status"] = data["status"]
    if data.get("url") is not None: update["url"] = (data.get("url") or "").strip()
    if not update:
        return jsonify({"success": False, "error": "nichts zu ändern"})
    sb_update("ai_visibility_massnahmen", f"id=eq.{mid}", update)
    return jsonify({"success": True})
@app.route("/ai-visibility/score-verlauf", methods=["GET"])
def ai_visibility_score_verlauf():
    """Gesamt-Score pro Woche, kompakt für den GEO/Content-Tab."""
    rows = sb_get("ai_visibility_results", "select=woche,score&order=woche.asc") or []
    proWoche = {}
    for r in rows:
        proWoche.setdefault(r["woche"], []).append(r.get("score", 0))
    verlauf = [{"woche": w, "score": round(sum(s)/len(s)) if s else 0}
               for w, s in sorted(proWoche.items())]
    return jsonify({"success": True, "verlauf": verlauf[-20:]})

@app.route("/ai-visibility/wochen", methods=["GET"])
def ai_visibility_wochen():
    rows = sb_get("ai_visibility_results", "select=woche&order=run_timestamp.desc") or []
    wochen = []
    for r in rows:
        if r["woche"] not in wochen: wochen.append(r["woche"])
    return jsonify({"success": True, "wochen": wochen[:20]})

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

# ════════════════════════════════════════
# GEMINI (Bild + Text) für den Content Creator
# Modellname hier oben leicht änderbar — echten Namen aus Google AI Studio eintragen.
# ════════════════════════════════════════
GEMINI_MODEL = "gemini-3.5-flash"  # ← ggf. aktuellen Namen aus AI Studio eintragen
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

def call_gemini_vision(system_prompt, user_text, image_base64, media_type="image/jpeg"):
    """Gemini mit Bild-Input. image_base64 ohne data:... Prefix.
    Liefert reinen Text zurück (gleiche Signatur wie call_claude_vision)."""
    if not GEMINI_API_KEY:
        return "GEMINI_API_KEY fehlt"
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": system_prompt}]},
                "contents": [{
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": media_type, "data": image_base64}},
                        {"text": user_text}
                    ]
                }],
                "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.7}
            },
            timeout=30
        )
        data = r.json()
        if not r.ok:
            return f"Gemini Vision Fehler: {data}"
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"Gemini Vision Fehler: {str(e)}"

# Bildgenerierung via Nano Banana (Gemini Image). Modellname leicht änderbar.
# gemini-3.1-flash-image = Nano Banana 2 (gutes Preis-Leistungs-Verhältnis)
# gemini-3-pro-image     = Nano Banana Pro (höchste Qualität, teurer)
# gemini-2.5-flash-image = Nano Banana (Klassiker, günstig, stabil)
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"  # ← ggf. neueren Namen aus AI Studio eintragen

# ════════════════════════════════════════
# FAKTEN-REFERENZ für den AI-Visibility Bewertungs-Call
# Einzige Wahrheitsquelle. Josef-bestätigt.
# ════════════════════════════════════════
FAKTEN_REFERENZ = """FAKTEN über "Honigspirituosen Josef Mayer" (einzige Wahrheitsquelle, geprüft an honigspirituosen.at):
- Firma: Honigspirituosen Josef Mayer
- Standort: Wien, Österreich
- Website: honigspirituosen.at (bzw. www.honigspirituosen.at). ACHTUNG: Domain endet auf .at, NIEMALS .de. honigspirituosen.de ist FALSCH und eine Halluzination.
- Slogan: "Du erwartest Süße? – Du bekommst Charakter!", "Kein Likör – Keine dominierende Süße!", "Wo Bienen flüstern, entsteht Genuss". Alle KORREKT.
- Josef Mayer ist BERUFSIMKER. Er betreibt KEINE eigene Brennerei und brennt/destilliert NICHT selbst.
- Konzept: Die fertigen Spirituosen stammen aus traditionellen Herkunftsregionen. Josef VEREDELT sie mit eigenem Honig (aus Wien und Niederösterreich, eigene Imkerei plus ausgewählte Imkerkollegen aus Österreich). Es heißt "veredelt", NICHT "aromatisiert". Eigene Brennerei/Destillerie = FALSCH.
- Drei Produktlinien:
  * Wacholdergold (Gin): 0,2l = 21,90 €, 0,5l = 44,00 €
  * Fassgold (Whisky, ein Scotch Single Malt): 0,35l = 34,90 €, 0,5l = 44,00 €
  * Inselgold (Rum): 0,2l = 21,90 €, 0,5l = 44,00 €
- Alkoholgehalt: 31,5 % Vol. (KORREKT, nicht als Halluzination werten)
- Jede Produktlinie gibt es in mehreren HONIGSORTEN: Blütenhonig, Edelkastanienhonig, Lindenhonig, Sonnenblumenhonig, Waldhonig. Diese Sorten sind KORREKT und keine Halluzination.
- Zur Anzahl der Bienenvölker gibt es KEINE offizielle Angabe. Eine konkrete Völkerzahl ist als unbestätigt/potenziell halluziniert zu werten."""

def call_gemini_grounded(frage):
    """Stellt eine Frage an Gemini MIT Google-Search-Grounding.
    Gibt (rohantwort, quellen_liste) zurück. quellen_liste = Liste von URLs/Titeln."""
    if not GEMINI_API_KEY:
        return "GEMINI_API_KEY fehlt", []
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"role": "user", "parts": [{"text": frage}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024}
            },
            timeout=40
        )
        data = r.json()
        if not r.ok:
            return f"Grounding Fehler: {data}", []
        cand = data["candidates"][0]
        # Text zusammensetzen
        text = ""
        for part in cand.get("content", {}).get("parts", []):
            if part.get("text"):
                text += part["text"]
        # Quellen aus grounding_metadata ziehen (Struktur defensiv behandeln)
        quellen = []
        gm = cand.get("groundingMetadata") or cand.get("grounding_metadata") or {}
        chunks = gm.get("groundingChunks") or gm.get("grounding_chunks") or []
        for ch in chunks:
            web = ch.get("web") or {}
            uri = web.get("uri") or web.get("url") or ""
            titel = web.get("title") or ""
            if uri or titel:
                quellen.append({"url": uri, "titel": titel})
        return text.strip(), quellen
    except Exception as e:
        return f"Grounding Fehler: {str(e)}", []

def bewerte_antwort(frage, kategorie, rohantwort, quellen_anzahl):
    """Zweiter, günstiger Call: bewertet die Antwort gegen die Fakten-Referenz.
    Gibt dict mit score, marke_genannt, halluzination_flag, halluzination_detail."""
    prompt = f"""Du bist ein strenger Fakten-Prüfer. Bewerte die folgende KI-Antwort auf eine Suchanfrage.

{FAKTEN_REFERENZ}

FRAGE (Kategorie {kategorie}): {frage}

ZU BEWERTENDE KI-ANTWORT:
{rohantwort[:2000]}

Bewerte streng und gib NUR ein JSON zurück, kein anderer Text:
{{
  "marke_genannt": true/false,  // Wird "Honigspirituosen", "Josef Mayer" oder ein Produkt (Wacholdergold/Fassgold/Inselgold) genannt?
  "score": 0-100,               // Sichtbarkeit: 0 = gar nicht erwähnt, 100 = prominent mit korrekten Fakten + Website + Kaufmöglichkeit
  "halluzination_flag": true/false,  // Enthält die Antwort FALSCHE Fakten über die Marke (falscher Preis, "aromatisiert", eigene Brennerei, erfundene Völkerzahl, falscher Ort)?
  "halluzination_detail": "kurze Begründung falls halluzination_flag true, sonst leer"
}}"""
    roh = call_gpt4(prompt, max_tokens=500)
    try:
        s = roh.find("{"); e = roh.rfind("}")
        if s >= 0 and e > s:
            d = json.loads(roh[s:e+1])
            # Confidence aus Quellenzahl ableiten (0 Quellen = 20, viele = bis 100)
            conf = min(100, 20 + quellen_anzahl * 20)
            d["confidence"] = conf
            return d
    except Exception:
        pass
    return {"marke_genannt": False, "score": 0, "halluzination_flag": False,
            "halluzination_detail": "", "confidence": min(100, 20 + quellen_anzahl * 20)}

def generate_image_gemini(prompt):
    """Erzeugt ein Bild via Nano Banana. Gibt (base64, media_type) oder (None, fehler) zurück.
    Hinweis: Alle Bilder enthalten ein unsichtbares SynthID-Wasserzeichen."""
    if not GEMINI_API_KEY:
        return None, "GEMINI_API_KEY fehlt"
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent?key={GEMINI_API_KEY}"
        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"contents": [{"role": "user", "parts": [{"text": prompt}]}]},
            timeout=60
        )
        data = r.json()
        if not r.ok:
            return None, f"Nano Banana Fehler: {data}"
        # Bild aus den parts fischen
        for part in data["candidates"][0]["content"]["parts"]:
            inline = part.get("inline_data") or part.get("inlineData")
            if inline and inline.get("data"):
                mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
                return inline["data"], mime
        return None, "Kein Bild in der Antwort"
    except Exception as e:
        return None, f"Nano Banana Fehler: {str(e)}"

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

    system_mit_brand = CONTENT_SYSTEM + brand_kontext()
    # Content Creator läuft jetzt über Gemini. Zum Zurückschalten auf Claude:
    # antwort = call_claude_vision(system_mit_brand, user_text, image_base64, media_type)
    antwort = call_gemini_vision(system_mit_brand, user_text, image_base64, media_type)

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

    # Bildgenerierung läuft jetzt über Nano Banana (Gemini). Zum Zurückschalten auf DALL-E:
    # den DALL-E-Block unten entkommentieren und diesen Aufruf entfernen.
    b64, mime = generate_image_gemini(prompt)
    if b64:
        return jsonify({"success": True, "image_base64": b64, "media_type": mime})
    else:
        return jsonify({"success": False, "error": mime})

    # ── ALTER DALL-E-WEG (Rückfalllösung) ──
    # try:
    #     r = requests.post(
    #         "https://api.openai.com/v1/images/generations",
    #         headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
    #         json={"model": "dall-e-3", "prompt": prompt, "n": 1,
    #               "size": "1024x1024", "quality": "standard", "response_format": "b64_json"},
    #         timeout=60
    #     )
    #     data_r = r.json()
    #     if not r.ok:
    #         return jsonify({"success": False, "error": f"DALL-E Fehler: {data_r}"})
    #     b64 = data_r["data"][0]["b64_json"]
    #     return jsonify({"success": True, "image_base64": b64, "media_type": "image/png"})
    # except Exception as e:
    #     return jsonify({"success": False, "error": f"DALL-E Fehler: {str(e)}"})

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


# ════════════════════════════════════════
# VERANSTALTER-RADAR
# Statt Events zu suchen: Veranstalter als dauerhafte Datenbank führen,
# auf Ausschreibungen überwachen, mit Josef-Fit-Score bewerten.
# Architektur-Idee: Melli
# ════════════════════════════════════════

# Startbestand – von Melli vorgeschlagene Veranstalter Wien/NÖ
VERANSTALTER_SEED = [
    {"name": "Wintermarkt Prater", "website": "https://www.wintermarkt.at/aussteller",
     "marktart": "Adventmarkt", "region": "Wien"},
    {"name": "Weihnachtsmarkt am Hof", "website": "https://www.weihnachtsmarkt-hof.at/",
     "marktart": "Adventmarkt", "region": "Wien"},
    {"name": "Weihnachtsquartier Wien", "website": "https://weihnachtsquartier.at/",
     "marktart": "Adventmarkt", "region": "Wien"},
    {"name": "Design Depot Wien", "website": "https://www.design-depot.at/",
     "marktart": "Design-/Manufakturmarkt", "region": "Wien"},
    {"name": "Weihnachtsmarkt Schloss Schönbrunn", "website": "https://www.weihnachtsmarkt-schoenbrunn.at/",
     "marktart": "Adventmarkt", "region": "Wien"},
    {"name": "MAX.CENTER Weihnachtsmarkt", "website": "https://www.maxcenter.at/de/news/aussteller-gesucht-weihnachtsmarkt/",
     "marktart": "Adventmarkt", "region": "Niederösterreich"},
]

# Saisonale Gewichtung – welche Marktart ist je nach Monat gerade aktuell für BEWERBUNGEN
def saison_gewichtung():
    monat = datetime.now().month
    # Bewerbungsfristen laufen meist Monate vor dem Event
    if monat in (6, 7, 8):       # Sommer → Advent-Bewerbungen laufen
        return {"fokus": "Adventmärkte", "hinweis": "Jetzt laufen die Bewerbungsfristen für Advent-/Weihnachtsmärkte (Saison Nov/Dez)."}
    elif monat in (1, 2, 3):     # Winter → Frühling/Ostern
        return {"fokus": "Frühlings-/Ostermärkte", "hinweis": "Jetzt laufen Bewerbungen für Frühlings-, Oster- und Genussmärkte."}
    elif monat in (4, 5):        # Frühling → Sommerfeste
        return {"fokus": "Sommerfeste/Genussmärkte", "hinweis": "Jetzt laufen Bewerbungen für Sommerfeste und Genussmärkte."}
    else:                        # 9,10,11,12 → Herbst/Genuss + nächstes Frühjahr
        return {"fokus": "Herbst-/Genussmärkte", "hinweis": "Jetzt laufen Bewerbungen für Herbst-/Genussmärkte und teils schon nächstes Frühjahr."}

RADAR_SYSTEM = """Du bist das Veranstalter-Radar von Honigspirituosen Josef Mayer aus Wien.
Josef ist Berufsimker mit Premium-Spirituosen (Gin, Whisky, Rum mit eigenem Honig, hochpreisig).
Er sucht NICHT einzelne Events, sondern VERANSTALTER von Märkten/Messen in Wien + Niederösterreich,
bei denen er sich als Aussteller bewerben kann – mit Fokus auf Premium, Verkostung, Genuss.

DEINE AUFGABE: Finde Veranstalter und ihre aktuellen Aussteller-Ausschreibungen. Suche gezielt nach
Ausschreibungs-Begriffen ("Aussteller gesucht", "Standanmeldung", "jetzt bewerben", "Marktstand",
"Teilnahme", "exhibitor"), NICHT nach Event-Berichten.

JOSEF-FIT-SCORE (0-100) für jeden Veranstalter berechnen:
- Spirituosen/Alkohol als Aussteller erlaubt: +30
- Verkostung am Stand erlaubt: +20
- Erwartete Besucher über 5.000: +15
- Standgebühr unter 1.000 €: +10
- Premium-/hochwertige Positionierung des Marktes: +15
- Wiederbewerbung / jährlich wiederkehrend: +10
Wenn eine Info nicht auffindbar ist, schätze konservativ und vermerke es."""


def veranstalter_seed_anlegen():
    """Legt die Seed-Veranstalter an, falls die Tabelle noch leer ist."""
    bestehende = sb_get("veranstalter", "select=name")
    if bestehende and len(bestehende) > 0:
        return 0  # schon befüllt
    count = 0
    for v in VERANSTALTER_SEED:
        sb_insert("veranstalter", {
            "name": v["name"],
            "website": v["website"],
            "marktart": v["marktart"],
            "region": v["region"],
            "kontakt": "",
            "fristfenster": "",
            "besucher": "",
            "fit_score": 0,
            "status": "neu",
            "notiz": "Startbestand (Melli)",
            "geprueft_am": ""
        })
        count += 1
    return count


@app.route("/veranstalter", methods=["GET"])
def veranstalter_liste():
    """Alle Veranstalter, nach Fit-Score sortiert (beste zuerst)."""
    # Beim ersten Aufruf Seed anlegen
    veranstalter_seed_anlegen()
    rows = sb_get("veranstalter", "select=*&order=fit_score.desc")
    saison = saison_gewichtung()
    return jsonify({"success": True, "veranstalter": rows or [], "saison": saison})


@app.route("/veranstalter/hinzufuegen", methods=["POST"])
def veranstalter_hinzufuegen():
    """Manuell einen Veranstalter eintragen (Josef/Melli)."""
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "Kein Name"})
    sb_insert("veranstalter", {
        "name": name,
        "website": (data.get("website") or "").strip(),
        "marktart": (data.get("marktart") or "").strip(),
        "region": (data.get("region") or "").strip(),
        "kontakt": (data.get("kontakt") or "").strip(),
        "fristfenster": "",
        "besucher": "",
        "fit_score": 0,
        "status": "neu",
        "notiz": "Manuell hinzugefügt",
        "geprueft_am": ""
    })
    return jsonify({"success": True})


@app.route("/veranstalter/radar", methods=["POST"])
def veranstalter_radar():
    """Radar-Lauf: prüft bekannte Veranstalter auf Ausschreibungen + sucht neue.
    Bewertet alles mit Josef-Fit-Score. Auf Knopfdruck (kostet ein paar Cent)."""
    veranstalter_seed_anlegen()
    heute = datetime.now().strftime("%d.%m.%Y")
    saison = saison_gewichtung()

    # Bekannte Veranstalter für den Kontext
    bekannte = sb_get("veranstalter", "select=name,website,marktart")
    bekannte_namen = ", ".join(b.get("name", "") for b in (bekannte or []))

    user_msg = f"""Heute ist der {heute}. SAISON-FOKUS: {saison['hinweis']}

Prüfe per Web-Suche, welche dieser bekannten Veranstalter gerade eine offene Aussteller-Ausschreibung haben (Bewerbungsfrist, Anmeldeformular, "jetzt bewerben"):
{bekannte_namen}

Suche ZUSÄTZLICH nach 2-3 NEUEN Veranstaltern in Wien/NÖ mit aktueller Aussteller-Ausschreibung, passend zum Saison-Fokus ({saison['fokus']}). Premium/Genuss bevorzugt.

Gib NUR ein JSON-Array zurück, kein anderer Text, keine Markdown-Blöcke:
[{{"name":"Veranstalter","website":"https://...","marktart":"Adventmarkt/Genussmarkt/Design/Hochzeit","region":"Wien/Niederösterreich","fristfenster":"z.B. Bewerbung bis 31.08.2026 oder leer","besucher":"z.B. 10000 oder leer","fit_score":0-100,"fit_begruendung":"kurz warum dieser Score"}}]

Den fit_score nach den genannten Kriterien berechnen. Nur echte, im Web gefundene Veranstalter."""

    antwort = call_claude_websearch(RADAR_SYSTEM, user_msg, max_searches=6)

    import re as _re
    try:
        json_match = _re.search(r'\[.*\]', antwort, _re.DOTALL)
        gefunden = json.loads(json_match.group()) if json_match else []
    except:
        gefunden = []

    if not gefunden:
        return jsonify({"success": False, "error": "Keine Ergebnisse oder Antwort nicht lesbar",
                        "raw": antwort[:500]})

    # Bestehende holen für Update/Insert-Entscheidung
    bestehende = sb_get("veranstalter", "select=id,name")
    name_zu_id = {b.get("name", "").lower().strip(): b.get("id") for b in (bestehende or [])}

    neu_count = 0
    update_count = 0
    for g in gefunden:
        name = (g.get("name") or "").strip()
        if not name:
            continue
        payload = {
            "website": (g.get("website") or "").strip(),
            "marktart": (g.get("marktart") or "").strip(),
            "region": (g.get("region") or "").strip(),
            "fristfenster": (g.get("fristfenster") or "").strip(),
            "besucher": str(g.get("besucher") or "").strip(),
            "fit_score": int(g.get("fit_score") or 0),
            "notiz": (g.get("fit_begruendung") or "").strip(),
            "geprueft_am": datetime.now().isoformat()
        }
        existing_id = name_zu_id.get(name.lower())
        if existing_id:
            sb_update("veranstalter", f"id=eq.{existing_id}", payload)
            update_count += 1
        else:
            payload["name"] = name
            payload["kontakt"] = ""
            payload["status"] = "neu"
            sb_insert("veranstalter", payload)
            neu_count += 1

    return jsonify({"success": True, "neu": neu_count, "aktualisiert": update_count,
                    "gesamt": len(gefunden)})


@app.route("/veranstalter/status", methods=["POST"])
def veranstalter_status():
    """Status ändern (neu/interessant/beworben/erledigt)."""
    data = request.json or {}
    vid = data.get("id")
    status = data.get("status", "")
    if not vid:
        return jsonify({"success": False, "error": "Keine ID"})
    ok = sb_update("veranstalter", f"id=eq.{vid}", {"status": status})
    return jsonify({"success": bool(ok)})


@app.route("/veranstalter/loeschen", methods=["POST"])
def veranstalter_loeschen():
    """Veranstalter löschen."""
    data = request.json or {}
    vid = data.get("id")
    if not vid:
        return jsonify({"success": False, "error": "Keine ID"})
    try:
        r = requests.delete(
            f"{SUPABASE_URL}/rest/v1/veranstalter?id=eq.{vid}",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10
        )
        return jsonify({"success": r.ok})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/veranstalter/test", methods=["GET"])
def veranstalter_test():
    """Diagnose: rohe Claude-Antwort des Radar-Laufs, im Browser aufrufbar."""
    heute = datetime.now().strftime("%d.%m.%Y")
    saison = saison_gewichtung()
    user_msg = f"""Heute ist der {heute}. Saison-Fokus: {saison['fokus']}.
Suche 2 Veranstalter in Wien/NÖ mit aktueller Aussteller-Ausschreibung (Premium/Genuss/Advent).
Gib NUR ein JSON-Array zurück, kein anderer Text:
[{{"name":"...","website":"...","marktart":"...","region":"...","fristfenster":"...","besucher":"...","fit_score":0,"fit_begruendung":"..."}}]"""
    antwort = call_claude_websearch(RADAR_SYSTEM, user_msg, max_searches=3)
    import re as _re
    extrahiert = None
    fehler = None
    try:
        m = _re.search(r'\[.*\]', antwort, _re.DOTALL)
        if m:
            extrahiert = json.loads(m.group())
        else:
            fehler = "Kein [...] Block gefunden"
    except Exception as e:
        fehler = f"JSON-Fehler: {str(e)}"
    return jsonify({
        "rohe_antwort": antwort,
        "extrahiert_erfolgreich": extrahiert is not None,
        "anzahl": len(extrahiert) if extrahiert else 0,
        "fehler": fehler,
        "saison": saison
    })


# ════════════════════════════════════════
# PARTNER-SYSTEM (Franchise-Test)
# Komplett getrennt vom Haupt-Dashboard.
# Testverkäufer scannt Bars/Cocktailbars, eigene Leads, eigener Login.
# Tabellen: partner (Logins), partner_leads (seine Leads), partner_scans (Tageslimit)
# ════════════════════════════════════════

import hashlib
import secrets as _secrets

# Aktive Sessions: token -> {partner_id, name, expires}
_PARTNER_SESSIONS = {}

# Admin Sessions (Josef): token -> {name, expires}
_ADMIN_SESSIONS = {}

PARTNER_SCAN_LIMIT = 5  # Scans pro Tag pro Partner

def _hash_pw(passwort, salt):
    """Passwort sicher hashen (PBKDF2)."""
    return hashlib.pbkdf2_hmac("sha256", passwort.encode(), salt.encode(), 100000).hex()

def partner_aus_token(token):
    """Gibt die Partner-Session zurück, wenn Token gültig, sonst None."""
    sess = _PARTNER_SESSIONS.get(token)
    if not sess:
        return None
    if datetime.now().timestamp() > sess["expires"]:
        _PARTNER_SESSIONS.pop(token, None)
        return None
    return sess

def _token_aus_request():
    """Holt das Partner-Token aus dem Authorization-Header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None

@app.route("/partner/login", methods=["POST"])
def partner_login():
    """Partner-Login. Gibt ein Session-Token zurück."""
    data = request.json or {}
    name = (data.get("name") or "").strip().lower()
    passwort = data.get("passwort") or ""
    if not name or not passwort:
        return jsonify({"success": False, "error": "Name und Passwort nötig"})

    rows = sb_get("partner", f"select=*&name=eq.{name}")
    if not rows or len(rows) == 0:
        return jsonify({"success": False, "error": "Login fehlgeschlagen"})

    p = rows[0]
    if _hash_pw(passwort, p.get("salt", "")) != p.get("pw_hash", ""):
        return jsonify({"success": False, "error": "Login fehlgeschlagen"})

    # Session erstellen (24h gültig)
    token = _secrets.token_urlsafe(32)
    _PARTNER_SESSIONS[token] = {
        "partner_id": p["id"],
        "name": p["name"],
        "expires": datetime.now().timestamp() + 86400
    }
    return jsonify({"success": True, "token": token, "name": p["name"], "stadt": p.get("stadt", "Wien")})

@app.route("/partner/me", methods=["GET"])
def partner_me():
    """Prüft, ob das Token gültig ist (für Auto-Login beim Seitenaufruf)."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False})
    return jsonify({"success": True, "name": sess["name"]})

def _scans_heute(partner_id):
    """Zählt die heutigen Scans eines Partners."""
    heute = datetime.now().strftime("%Y-%m-%d")
    rows = sb_get("partner_scans", f"select=id&partner_id=eq.{partner_id}&tag=eq.{heute}")
    return len(rows) if rows else 0

def _ist_admin(partner_id):
    """Prüft ob der Account ein Admin ist (kein Limit)."""
    rows = sb_get("partner", f"select=rolle&id=eq.{partner_id}")
    return bool(rows) and rows[0].get("rolle") == "admin"

def _offene_leads_ohne_termin(partner_id):
    """Zählt Leads die noch keinen Termin haben und nicht abgelehnt sind."""
    rows = sb_get("partner_leads",
        f"select=id&partner_id=eq.{partner_id}&status=in.(neu,angeschrieben)")
    return len(rows) if rows else 0

def _belegte_slots(partner_id):
    """Liste der vergebenen Slots im 3-Wochen-Fenster als 'YYYY-MM-DD HH:MM'."""
    heute = datetime.now().strftime("%Y-%m-%d")
    ende = (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d")
    rows = sb_get("partner_termine",
        f"select=datum,uhrzeit&partner_id=eq.{partner_id}"
        f"&datum=gte.{heute}&datum=lte.{ende}&status=neq.abgesagt")
    return [f"{r['datum']} {r['uhrzeit']}" for r in (rows or [])]

# Grenze: solange so viele Leads offen sind, kein neuer Scan (Partner)
OFFENE_LEADS_GRENZE = 20

@app.route("/partner/scan", methods=["POST"])
def partner_scan():
    """Partner scannt Bars/Cocktailbars. Limit über offene Leads, nicht pro Tag."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401

    partner_id = sess["partner_id"]
    admin = _ist_admin(partner_id)

    # Limit: Partner darf nicht nachladen, solange zu viele Leads unbearbeitet sind.
    # Admin (Josef) hat kein Limit.
    if not admin:
        offen = _offene_leads_ohne_termin(partner_id)
        if offen >= OFFENE_LEADS_GRENZE:
            return jsonify({"success": False, "error":
                f"Du hast noch {offen} offene Leads ohne Termin. "
                f"Arbeite die erst ab (Termin oder abgelehnt), dann kannst du neu scannen."})

    data = request.json or {}
    # Feste Auswahl: nur Bar oder Cocktailbar
    target_type = data.get("target_type", "Cocktailbar")
    if target_type not in ("Bar", "Cocktailbar"):
        target_type = "Cocktailbar"
    bezirk = data.get("bezirk", "Wien")
    max_results = min(data.get("max_results", 10), 20)

    queries = {
        "Bar": ["Bar Wien", "Weinbar"],
        "Cocktailbar": ["Cocktailbar", "Cocktail Lounge"]
    }.get(target_type, ["Cocktailbar"])

    # Bereits von DIESEM Partner gescannte place_ids
    bestehende = sb_get("partner_leads", f"select=place_id&partner_id=eq.{partner_id}")
    schon_da = set(b.get("place_id", "") for b in (bestehende or []) if b.get("place_id"))

    all_places = []
    seen = set()
    for q in queries[:2]:
        for p in search_places(q, bezirk, max_results=60, pages=3):
            pid = p["place_id"]
            if pid not in seen and pid not in schon_da:
                seen.add(pid)
                all_places.append(p)

    # Verfügbarkeit des Partners aus der DB (Tagesfenster, 3-Wochen-Fenster)
    heute = datetime.now().strftime("%Y-%m-%d")
    ende = (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d")
    verf_rows = sb_get("partner_verfuegbarkeit",
        f"select=datum,von,bis&partner_id=eq.{partner_id}"
        f"&datum=gte.{heute}&datum=lte.{ende}&order=datum.asc")
    verfuegbarkeit = verf_rows or []

    # Bereits belegte Slots im 3-Wochen-Fenster (einmal laden)
    belegt = _belegte_slots(partner_id)

    # Geo-Clustering: nahe Leads zuerst (kompakte Touren), 800m-Radius
    all_places = cluster_leads(all_places, radius_m=800)

    qualified = []
    for place in all_places[:max_results]:
        analysis = analyze_website(place.get("website", ""))
        if analysis["score"] >= 1 or place.get("rating", 0) >= 4.0:
            # Terminvorschläge: Verfügbarkeit ∩ Öffnungszeiten dieser Location + freie Slots
            oeffnung = place.get("opening_hours", [])
            appointments = calculate_appointments(
                verfuegbarkeit,
                opening_hours=oeffnung,
                belegte_slots=belegt,
                wochen=3
            )
            email = write_email(place, analysis, appointments[:3])
            lead = {
                "partner_id": partner_id,
                "name": place["name"],
                "address": place["address"],
                "place_id": place.get("place_id", ""),
                "lat": place.get("lat"),
                "lng": place.get("lng"),
                "website": place.get("website", ""),
                "phone": place.get("phone", ""),
                "contact_email": analysis.get("contact_email", ""),
                "score": analysis["score"],
                "rating": place.get("rating", 0),
                "email_draft": email,
                "target_type": target_type,
                "status": "neu",
                "notiz": "",
                "created_at": datetime.now().isoformat()
            }
            qualified.append(lead)
            sb_insert("partner_leads", lead)

    # Scan protokollieren (fürs Tageslimit)
    sb_insert("partner_scans", {
        "partner_id": partner_id,
        "tag": datetime.now().strftime("%Y-%m-%d"),
        "target_type": target_type,
        "bezirk": bezirk,
        "anzahl": len(qualified),
        "created_at": datetime.now().isoformat()
    })

    return jsonify({
        "success": True,
        "gefunden": len(all_places),
        "qualifiziert": len(qualified),
        "scans_genutzt": genutzt + 1,
        "scans_limit": PARTNER_SCAN_LIMIT
    })

@app.route("/partner/leads", methods=["GET"])
def partner_leads_liste():
    """Gibt die Leads des eingeloggten Partners zurück."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401

    rows = sb_get("partner_leads", f"select=*&partner_id=eq.{sess['partner_id']}&order=created_at.desc")
    genutzt = _scans_heute(sess["partner_id"])
    return jsonify({
        "success": True,
        "leads": rows or [],
        "scans_genutzt": genutzt,
        "scans_limit": PARTNER_SCAN_LIMIT
    })

@app.route("/partner/lead/status", methods=["POST"])
def partner_lead_status():
    """Partner setzt den Status eines seiner Leads."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401

    data = request.json or {}
    lead_id = data.get("id")
    status = data.get("status", "")
    notiz = data.get("notiz")
    if not lead_id:
        return jsonify({"success": False, "error": "Keine ID"})

    # Sicherstellen, dass der Lead diesem Partner gehört
    rows = sb_get("partner_leads", f"select=partner_id&id=eq.{lead_id}")
    if not rows or rows[0].get("partner_id") != sess["partner_id"]:
        return jsonify({"success": False, "error": "Nicht erlaubt"}), 403

    update = {}
    if status:
        update["status"] = status
    if notiz is not None:
        update["notiz"] = notiz
    ok = sb_update("partner_leads", f"id=eq.{lead_id}", update)
    return jsonify({"success": bool(ok)})

@app.route("/partner/followups", methods=["GET"])
def partner_followups():
    """Gibt Leads zurück, die seit 3+ Tagen keinen Status-Update haben (noch 'neu' oder 'angeschrieben')."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401
    grenze = (datetime.now() - timedelta(days=3)).isoformat()
    rows = sb_get("partner_leads",
        f"select=*&partner_id=eq.{sess['partner_id']}"
        f"&status=in.(neu,angeschrieben)"
        f"&created_at=lte.{grenze}"
        f"&order=created_at.asc")
    return jsonify({"success": True, "followups": rows or []})

# ── ADMIN LOGIN (Josef) ──

def _admin_aus_token(token):
    sess = _ADMIN_SESSIONS.get(token)
    if not sess:
        return None
    if datetime.now().timestamp() > sess["expires"]:
        _ADMIN_SESSIONS.pop(token, None)
        return None
    return sess

def _admin_token_aus_request():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None

@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.json or {}
    name = (data.get("name") or "").strip().lower()
    passwort = data.get("passwort") or ""
    if not name or not passwort:
        return jsonify({"success": False, "error": "Name und Passwort nötig"})
    rows = sb_get("partner", f"select=*&name=eq.{name}")
    if not rows or len(rows) == 0:
        return jsonify({"success": False, "error": "Login fehlgeschlagen"})
    p = rows[0]
    if _hash_pw(passwort, p.get("salt", "")) != p.get("pw_hash", ""):
        return jsonify({"success": False, "error": "Login fehlgeschlagen"})
    token = _secrets.token_urlsafe(32)
    _ADMIN_SESSIONS[token] = {
        "name": p["name"],
        "expires": datetime.now().timestamp() + 86400
    }
    return jsonify({"success": True, "token": token, "name": p["name"]})

@app.route("/admin/me", methods=["GET"])
def admin_me():
    token = _admin_token_aus_request()
    sess = _admin_aus_token(token)
    if not sess:
        return jsonify({"success": False}), 401
    return jsonify({"success": True, "name": sess["name"]})

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    token = _admin_token_aus_request()
    if token:
        _ADMIN_SESSIONS.pop(token, None)
    return jsonify({"success": True})

@app.route("/admin/anlegen", methods=["POST"])
def admin_anlegen():
    """Legt den Admin-Account (Josef) an. Einmalig, geschützt durch PARTNER_SETUP_KEY."""
    data = request.json or {}
    setup_key = data.get("setup_key", "")
    erwartet = os.environ.get("PARTNER_SETUP_KEY", "")
    if not erwartet or setup_key != erwartet:
        return jsonify({"success": False, "error": "Falscher Setup-Key"}), 403
    name = (data.get("name") or "").strip().lower()
    passwort = data.get("passwort") or ""
    if not name or not passwort:
        return jsonify({"success": False, "error": "Name und Passwort nötig"})
    bestehend = sb_get("partner", f"select=id&name=eq.{name}")
    if bestehend and len(bestehend) > 0:
        return jsonify({"success": False, "error": "Name existiert bereits"})
    salt = _secrets.token_hex(16)
    pw_hash = _hash_pw(passwort, salt)
    sb_insert("partner", {
        "name": name,
        "pw_hash": pw_hash,
        "salt": salt,
        "stadt": "Wien",
        "rolle": "admin",
        "angelegt": datetime.now().isoformat()
    })
    return jsonify({"success": True, "name": name})

# ── TERMIN-KALENDER (Partner + Admin) ──

def _termin_fenster_tage():
    """3-Wochen-Fenster ab heute."""
    return 21

@app.route("/partner/verfuegbarkeit", methods=["GET"])
def partner_verfuegbarkeit_liste():
    """Verfügbarkeitsfenster des Partners im 3-Wochen-Fenster."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401
    heute = datetime.now().strftime("%Y-%m-%d")
    ende = (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d")
    rows = sb_get("partner_verfuegbarkeit",
        f"select=*&partner_id=eq.{sess['partner_id']}"
        f"&datum=gte.{heute}&datum=lte.{ende}&order=datum.asc,von.asc")
    return jsonify({"success": True, "fenster": rows or []})

@app.route("/partner/verfuegbarkeit/anlegen", methods=["POST"])
def partner_verfuegbarkeit_anlegen():
    """Partner trägt ein Zeitfenster für einen Tag ein (mehrere pro Tag möglich)."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401
    data = request.json or {}
    datum = (data.get("datum") or "").strip()
    von = (data.get("von") or "").strip()
    bis = (data.get("bis") or "").strip()
    if not datum or not von or not bis:
        return jsonify({"success": False, "error": "Datum, von und bis nötig"})
    if bis <= von:
        return jsonify({"success": False, "error": "'bis' muss nach 'von' liegen"})
    sb_insert("partner_verfuegbarkeit", {
        "partner_id": sess["partner_id"],
        "datum": datum, "von": von, "bis": bis,
        "created_at": datetime.now().isoformat()
    })
    return jsonify({"success": True})

@app.route("/partner/verfuegbarkeit/loeschen", methods=["POST"])
def partner_verfuegbarkeit_loeschen():
    """Ein Zeitfenster wieder entfernen."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401
    data = request.json or {}
    fid = data.get("id")
    if not fid:
        return jsonify({"success": False, "error": "Keine ID"})
    rows = sb_get("partner_verfuegbarkeit", f"select=partner_id&id=eq.{fid}")
    if not rows or rows[0].get("partner_id") != sess["partner_id"]:
        return jsonify({"success": False, "error": "Nicht erlaubt"}), 403
    sb_delete("partner_verfuegbarkeit", f"id=eq.{fid}")
    return jsonify({"success": True})

@app.route("/partner/termine", methods=["GET"])
def partner_termine_liste():
    """Alle Termine des eingeloggten Partners im 3-Wochen-Fenster."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401
    heute = datetime.now().strftime("%Y-%m-%d")
    ende = (datetime.now() + timedelta(days=_termin_fenster_tage())).strftime("%Y-%m-%d")
    rows = sb_get("partner_termine",
        f"select=*&partner_id=eq.{sess['partner_id']}"
        f"&datum=gte.{heute}&datum=lte.{ende}"
        f"&order=datum.asc,uhrzeit.asc")
    return jsonify({"success": True, "termine": rows or []})

@app.route("/partner/termin/anlegen", methods=["POST"])
def partner_termin_anlegen():
    """Partner trägt einen Termin ein (nach dem Anruf)."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401
    data = request.json or {}
    datum = (data.get("datum") or "").strip()
    uhrzeit = (data.get("uhrzeit") or "").strip()
    if not datum or not uhrzeit:
        return jsonify({"success": False, "error": "Datum und Uhrzeit nötig"})
    # Slot schon belegt?
    bestehend = sb_get("partner_termine",
        f"select=id&partner_id=eq.{sess['partner_id']}"
        f"&datum=eq.{datum}&uhrzeit=eq.{uhrzeit}&status=neq.abgesagt")
    if bestehend and len(bestehend) > 0:
        return jsonify({"success": False, "error": "Slot bereits belegt"})
    termin = {
        "partner_id": sess["partner_id"],
        "lead_id": data.get("lead_id"),
        "lead_name": data.get("lead_name", ""),
        "datum": datum,
        "uhrzeit": uhrzeit,
        "status": "geplant",
        "notiz": data.get("notiz", ""),
        "created_at": datetime.now().isoformat()
    }
    sb_insert("partner_termine", termin)
    # Wenn aus einem Lead: Lead-Status auf 'termin' setzen
    if data.get("lead_id"):
        sb_update("partner_leads", f"id=eq.{data.get('lead_id')}", {"status": "termin"})
    return jsonify({"success": True})

@app.route("/partner/termin/status", methods=["POST"])
def partner_termin_status():
    """Termin auf erledigt/abgesagt setzen."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401
    data = request.json or {}
    tid = data.get("id")
    status = data.get("status", "")
    if not tid or status not in ("geplant", "erledigt", "abgesagt"):
        return jsonify({"success": False, "error": "Ungültig"})
    rows = sb_get("partner_termine", f"select=partner_id&id=eq.{tid}")
    if not rows or rows[0].get("partner_id") != sess["partner_id"]:
        return jsonify({"success": False, "error": "Nicht erlaubt"}), 403
    sb_update("partner_termine", f"id=eq.{tid}", {"status": status})
    return jsonify({"success": True})

@app.route("/partner/passwort", methods=["POST"])
def partner_passwort_aendern():
    """Partner ändert sein eigenes Passwort."""
    token = _token_aus_request()
    sess = partner_aus_token(token)
    if not sess:
        return jsonify({"success": False, "error": "Nicht eingeloggt"}), 401
    data = request.json or {}
    alt = data.get("alt") or ""
    neu = data.get("neu") or ""
    if len(neu) < 6:
        return jsonify({"success": False, "error": "Neues Passwort min. 6 Zeichen"})
    rows = sb_get("partner", f"select=*&id=eq.{sess['partner_id']}")
    if not rows:
        return jsonify({"success": False, "error": "Nicht gefunden"})
    p = rows[0]
    if _hash_pw(alt, p.get("salt", "")) != p.get("pw_hash", ""):
        return jsonify({"success": False, "error": "Altes Passwort falsch"})
    neuer_salt = _secrets.token_hex(16)
    neuer_hash = _hash_pw(neu, neuer_salt)
    sb_update("partner", f"id=eq.{sess['partner_id']}", {"pw_hash": neuer_hash, "salt": neuer_salt})
    return jsonify({"success": True})

@app.route("/partner/seite", methods=["GET"])
def partner_seite_alias():
    return send_file("partner.html")

@app.route("/partner", methods=["GET"])
def partner_seite():
    """Liefert die Partner-Oberfläche (getrennte HTML-Seite)."""
    return send_file("partner.html")

@app.route("/partner/anlegen", methods=["POST"])
def partner_anlegen():
    """Legt einen neuen Partner an. Geschützt durch einen Setup-Schlüssel.
    Aufruf NUR durch Josef, z.B. per Tool. Setup-Key als ENV PARTNER_SETUP_KEY."""
    data = request.json or {}
    setup_key = data.get("setup_key", "")
    erwartet = os.environ.get("PARTNER_SETUP_KEY", "")
    if not erwartet or setup_key != erwartet:
        return jsonify({"success": False, "error": "Falscher Setup-Key"}), 403

    name = (data.get("name") or "").strip().lower()
    passwort = data.get("passwort") or ""
    stadt = (data.get("stadt") or "Wien").strip()
    if not name or not passwort:
        return jsonify({"success": False, "error": "Name und Passwort nötig"})

    # Existiert der Name schon?
    bestehend = sb_get("partner", f"select=id&name=eq.{name}")
    if bestehend and len(bestehend) > 0:
        return jsonify({"success": False, "error": "Name existiert bereits"})

    salt = _secrets.token_hex(16)
    pw_hash = _hash_pw(passwort, salt)
    sb_insert("partner", {
        "name": name,
        "pw_hash": pw_hash,
        "salt": salt,
        "stadt": stadt,
        "angelegt": datetime.now().isoformat()
    })
    return jsonify({"success": True, "name": name})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
