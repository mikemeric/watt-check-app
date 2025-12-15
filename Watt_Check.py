import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import pytz
import hashlib
import secrets
import time
import os
import sqlite3
import logging
import threading
import json
from decimal import Decimal, getcontext
from contextlib import contextmanager

# --- 0. CONFIGURATION SYSTÈME & BRANDING ---
VERSION = "v1.0"
APP_NAME = "WATT-CHECK"
COMPANY_NAME = "DI-SOLUTIONS"

st.set_page_config(
    page_title=f"{APP_NAME} {VERSION} | {COMPANY_NAME}", 
    page_icon="⚡", 
    layout="wide",
    initial_sidebar_state="expanded"
)

logging.getLogger('streamlit').setLevel(logging.ERROR)
getcontext().prec = 28
FUSEAU = pytz.timezone('Africa/Douala')
DB_FILE = 'watt_check_saas.db'
SALT_FILE = ".watt_salt"
DB_LOCK = threading.Lock()

# --- 1. DESIGN SYSTEM (CSS AVANCÉ) ---
@st.cache_resource
def load_css():
    return """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700&family=Roboto:wght@300;400;700&display=swap');
        h1 { font-family: 'Orbitron', sans-serif; color: #FFD700; text-shadow: 0 0 10px rgba(255, 215, 0, 0.3); }
        h2, h3 { font-family: 'Roboto', sans-serif; color: #E2E8F0; }
        .stMetric, .history-card, .oracle-box, .result-box {
            background-color: #1E293B; border: 1px solid #334155; border-radius: 12px; padding: 15px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3); transition: transform 0.2s;
        }
        .stMetric:hover { transform: translateY(-2px); border-color: #FFD700; }
        .big-font { font-family: 'Orbitron', sans-serif; font-size: 42px !important; color: #FFD700; }
        .oracle-box { background: linear-gradient(135deg, #0F172A 0%, #1E3A8A 100%); border: 1px solid #3B82F6; }
        .stButton button { background-color: #2563EB; color: white; border-radius: 8px; font-weight: bold; border: none; }
        .stButton button:hover { background-color: #1D4ED8; }
        .branding-footer { text-align: center; color: #64748B; font-size: 12px; margin-top: 50px; border-top: 1px solid #334155; padding-top: 20px; }
        .company-name { color: #FFD700; font-weight: bold; letter-spacing: 1px; }
    </style>
    """
st.markdown(load_css(), unsafe_allow_html=True)

# --- 2. FONCTIONS SYSTÈME (SECU & DB) ---
def get_salt():
    if not os.path.exists(SALT_FILE):
        with open(SALT_FILE, "w") as f: f.write(secrets.token_hex(32))
    with open(SALT_FILE, "r") as f: return f.read().strip()
SALT = get_salt()

def hash_pass(password): return hashlib.sha256(f"{SALT}{password}".encode()).hexdigest()

@contextmanager
def db_connection():
    with DB_LOCK:
        conn = sqlite3.connect(DB_FILE, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try: yield conn
        except Exception as e: conn.rollback(); raise e
        finally: conn.close()

@st.cache_resource
def init_schema():
    with db_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      username TEXT UNIQUE, password TEXT, 
                      first_name TEXT, last_name TEXT, phone TEXT, meter_number TEXT,
                      is_pro BOOLEAN DEFAULT 0, is_admin BOOLEAN DEFAULT 0, 
                      pro_expiration_date TEXT, created_at TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS profils (user_id INTEGER PRIMARY KEY, budget REAL, conso_jour REAL, label TEXT, config_json TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS etats_mensuels (user_id INTEGER, mois TEXT, cumul REAL, PRIMARY KEY (user_id, mois))''')
        conn.execute('''CREATE TABLE IF NOT EXISTS historique (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, date TEXT, montant REAL, kwh REAL, token_ref TEXT, cumul_apres REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS licences (code TEXT PRIMARY KEY, created_by INTEGER, used_by INTEGER, created_at TEXT, used_at TEXT, duree_jours INTEGER DEFAULT 365)''')

def create_admin():
    h_pass = hash_pass("admin123")
    with db_connection() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
            conn.execute("INSERT INTO users (username, password, is_pro, is_admin, created_at) VALUES (?, ?, 1, 1, ?)", ("admin", h_pass, datetime.now().isoformat()))
            conn.commit()

init_schema(); create_admin()

# --- 3. LOGIQUE MÉTIER ---
def login_user(u, p):
    h = hash_pass(p)
    with db_connection() as conn: return conn.execute("SELECT * FROM users WHERE username=? AND password=?", (u, h)).fetchone()

def create_user(u, p):
    h = hash_pass(p)
    try:
        with db_connection() as conn:
            conn.execute("INSERT INTO users (username, password, created_at) VALUES (?, ?, ?)", (u, h, datetime.now().isoformat()))
            conn.commit(); return True
    except: return False

def update_profile(uid, fname, lname, phone, meter):
    with db_connection() as conn:
        conn.execute("UPDATE users SET first_name=?, last_name=?, phone=?, meter_number=? WHERE id=?", 
                  (fname, lname, phone, meter, uid))
        conn.commit()
    return True

def change_password(uid, old_p, new_p):
    h_old = hash_pass(old_p)
    with db_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=? AND password=?", (uid, h_old)).fetchone()
        if user:
            h_new = hash_pass(new_p)
            conn.execute("UPDATE users SET password=? WHERE id=?", (h_new, uid))
            conn.commit()
            return True
    return False

def check_pro_status(user):
    if not user['is_pro']: return False, None
    if user['is_admin'] or not user['pro_expiration_date']: return True, "Illimité"
    exp = datetime.fromisoformat(user['pro_expiration_date'])
    if datetime.now(FUSEAU).replace(tzinfo=None) > exp.replace(tzinfo=None): return False, "Expiré"
    return True, exp.strftime("%d/%m/%Y")

def act_licence(uid, code):
    with db_connection() as conn:
        row = conn.execute("SELECT * FROM licences WHERE code=? AND used_by IS NULL", (code,)).fetchone()
        if row:
            fin = datetime.now(FUSEAU) + timedelta(days=row['duree_jours'])
            conn.execute("UPDATE licences SET used_by=?, used_at=? WHERE code=?", (uid, datetime.now().isoformat(), code))
            conn.execute("UPDATE users SET is_pro=1, pro_expiration_date=? WHERE id=?", (fin.isoformat(), uid))
            conn.commit(); return True, fin.strftime("%d/%m/%Y")
        return False, None

def gen_licence(admin_id, j=365):
    code = f"PRO-{datetime.now().year}-{secrets.token_hex(4).upper()}"
    with db_connection() as conn:
        conn.execute("INSERT INTO licences (code, created_by, created_at, duree_jours) VALUES (?, ?, ?, ?)", (code, admin_id, datetime.now().isoformat(), j))
        conn.commit(); return code

@st.cache_data
def get_catalogue_pareto():
    return {
        "ECLAIRAGE": {"Ampoule LED": 9, "Ampoule Jaune": 75, "Tube Néon": 36, "Projecteur": 100},
        "FROID & CLIM": {"Ventilateur": 70, "Frigo Standard": 150, "Congélateur": 200, "Clim 1 CV": 900, "Clim 1.5 CV": 1400},
        "MULTIMEDIA": {"Chargeur Tél": 10, "Laptop": 65, "TV LED 32": 50, "TV 50+": 120, "Wifi": 15},
        "MAISON": {"Fer à repasser": 1200, "Mixeur": 350, "Micro-ondes": 1000, "Chauffe-eau": 2000, "Machine laver": 500}
    }

def get_coeff_simultaneite(p):
    if p < 3000: return 0.8
    if p < 6000: return 0.7
    if p < 12000: return 0.6
    return 0.5

@st.cache_data
def get_tranches_decimal():
    return {
        "0-110": [(Decimal('0'), Decimal('110'), Decimal('50'), False), (Decimal('110'), Decimal('220'), Decimal('94'), False), (Decimal('220'), Decimal('Infinity'), Decimal('94'), True)],
        "111-220": [(Decimal('0'), Decimal('220'), Decimal('79'), False), (Decimal('220'), Decimal('400'), Decimal('79'), True), (Decimal('400'), Decimal('Infinity'), Decimal('99'), True)],
        "221-400": [(Decimal('0'), Decimal('220'), Decimal('79'), True), (Decimal('220'), Decimal('400'), Decimal('79'), True), (Decimal('400'), Decimal('Infinity'), Decimal('99'), True)],
        "401+": [(Decimal('0'), Decimal('220'), Decimal('94'), True), (Decimal('220'), Decimal('800'), Decimal('94'), True), (Decimal('800'), Decimal('Infinity'), Decimal('99'), True)]
    }

def determiner_cat(cj):
    k = cj * 30
    if k <= 110: return "0-110"
    elif k <= 220: return "111-220"
    elif k <= 400: return "221-400"
    else: return "401+"

def calcul_kwh(m, c, cat):
    if m <= 0: return Decimal('0'), False, Decimal('0')
    arg = Decimal(str(m)); k_tot = Decimal('0'); tva = False; curs = Decimal(str(c)); p_u = Decimal('0')
    for bi, bs, px, at in get_tranches_decimal().get(cat):
        if arg < 0.1: break
        if curs >= bs: continue
        esp = bs - max(curs, bi); tx = Decimal('1.1925') if at else Decimal('1')
        ck = px * tx; p_u = ck; 
        if at: tva = True
        ct = esp * ck
        if arg >= ct: k_tot += esp; arg -= ct; curs += esp
        else: k_tot += arg / ck; arg = 0; break
    return k_tot, tva, p_u

# --- 4. INTERFACE GRAPHIQUE ---

if 'user' not in st.session_state: st.session_state.user = None

# A. LOGIN SCREEN
if not st.session_state.user:
    c1, c2, c3 = st.columns([1,2,1])
    with c2:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown(f"<h1 style='text-align: center;'>⚡ {APP_NAME}</h1>", unsafe_allow_html=True)
        st.markdown(f"<div style='text-align: center; color: #94a3b8; margin-bottom: 20px;'>Powered by <b>{COMPANY_NAME}</b></div>", unsafe_allow_html=True)
        
        tab1, tab2 = st.tabs(["🔐 Connexion", "📝 Inscription"])
        with tab1:
            with st.form("log"):
                u = st.text_input("Identifiant")
                p = st.text_input("Mot de passe", type="password")
                if st.form_submit_button("Se Connecter", use_container_width=True):
                    usr = login_user(u, p)
                    if usr: st.session_state.user = dict(usr); st.rerun()
                    else: st.error("Identifiants incorrects")
        with tab2:
            st.markdown("##### Créer un compte")
            with st.form("sign"):
                nu = st.text_input("Choisir un Identifiant")
                np = st.text_input("Choisir un Mot de passe", type="password")
                npc = st.text_input("Confirmer le Mot de passe", type="password")
                
                st.markdown("---")
                with st.expander("📜 Lire les Conditions Générales (CGU)"):
                    st.markdown("""**CGU SIMPLIFIÉES** : WATT-CHECK fournit des estimations. L'usage est sous votre responsabilité. Données sécurisées. Licence PRO valable 1 an.""")
                accept_cgu = st.checkbox("J'accepte les CGU", value=False)
                
                if st.form_submit_button("Créer mon Compte", use_container_width=True): 
                    if not accept_cgu: st.error("🛑 Acceptez les CGU.")
                    elif np != npc: st.error("🛑 Les mots de passe ne correspondent pas.")
                    elif len(nu) < 3: st.error("Identifiant trop court.")
                    elif create_user(nu, np): 
                        st.success("Compte créé !"); time.sleep(1)
                        st.info("Allez dans l'onglet Connexion.")
                    else: st.error("Identifiant déjà pris.")
    st.stop()

# B. DASHBOARD PRINCIPAL
user = st.session_state.user; IS_ADMIN = user['is_admin']; USER_ID = user['id']
est_pro, date_fin = check_pro_status(user)

with db_connection() as conn:
    user_fresh = conn.execute("SELECT * FROM users WHERE id=?", (USER_ID,)).fetchone()
    st.session_state.user = dict(user_fresh)
    prof = dict(conn.execute("SELECT * FROM profils WHERE user_id=?", (USER_ID,)).fetchone() or {})
    inv = json.loads(prof.get('config_json', '[]')) if prof else []
    mois = datetime.now(FUSEAU).strftime("%Y-%m")
    cumul = conn.execute("SELECT cumul FROM etats_mensuels WHERE user_id=? AND mois=?", (USER_ID, mois)).fetchone()
    cumul_val = cumul['cumul'] if cumul else 0.0

# SIDEBAR
with st.sidebar:
    st.markdown(f"### ⚡ {APP_NAME} <span style='font-size:10px'>{VERSION}</span>", unsafe_allow_html=True)
    st.write(f"Bonjour, **{st.session_state.user['username']}**")
    
    if IS_ADMIN: 
        st.error("🛡️ ADMIN")
    elif est_pro: 
        st.success("💎 PRO ACTIVE")
        st.caption(f"Expiration : {date_fin}")
    else:
        st.info("👤 VERSION GRATUITE")
        st.markdown("---")
        with st.expander("💎 PASSER PRO (OFFRE)", expanded=False):
            st.markdown("""
            <div style='background-color: #f0fdf4; padding: 10px; border-radius: 5px; border: 1px solid #bbf7d0; color: #166534; font-size: 13px; margin-bottom: 10px;'>
            <b>🚀 DÉBLOQUEZ TOUT :</b><br>
            ✅ Historique Illimité<br>
            ✅ Export Excel<br>
            ✅ Création d'appareils sur mesure
            </div>
            """, unsafe_allow_html=True)
            
            st.markdown("### 🏷️ Tarif : 5 000 FCFA / an")
            st.caption("Investissez une fois, économisez toute l'année.")
            
            st.warning("""
            **COMMENT ACTIVER ?**
            1️⃣ Dépôt OM/MOMO au :
            **671 89 40 95** (Emeric Tchamdjio Nkouetcha)
            2️⃣ Envoyez la capture sur WhatsApp.
            3️⃣ Entrez votre code ci-dessous :
            """)
            
            k = st.text_input("Saisir le Code Licence", placeholder="Ex: PRO-2026-...")
            
            if st.button("ACTIVER LA LICENCE", type="primary", use_container_width=True):
                ok, d = act_licence(USER_ID, k.strip())
                if ok: st.balloons(); st.rerun()
                else: st.error("Code invalide ou déjà utilisé.")
    
    st.markdown("---")
    if st.button("Déconnexion", use_container_width=True): st.session_state.user = None; st.rerun()
    st.markdown(f"<div style='text-align: center; color: #475569; font-size: 11px; margin-top: 20px;'>Développé par<br><b style