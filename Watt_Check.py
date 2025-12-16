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
VERSION = "v1.1" # Version Sauvegarde
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

# --- 1. DESIGN SYSTEM (CORRECTIF LISIBILITÉ HYBRIDE) ---
@st.cache_resource
def load_css():
    return """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700&family=Roboto:wght@300;400;700&display=swap');

        /* 1. TEXTE GÉNÉRAL : BLEU FONCÉ (Pour fond blanc) */
        html, body, [class*="css"], .stMarkdown, p, div, label, li, span, h1, h2, h3, h4, h5, h6 {
            color: #0F172A !important; 
            font-family: 'Roboto', sans-serif;
        }

        /* 2. TITRES SPÉCIFIQUES */
        h1 { 
            font-family: 'Orbitron', sans-serif !important; 
            color: #D97706 !important; /* Or foncé */
            text-shadow: none !important;
        }

        /* 3. CARTES & BOITES (FOND SOMBRE) */
        .stMetric, .history-card, .oracle-box, .result-box, [data-testid="stMetric"] {
            background-color: #1E293B !important; /* Fond bleu nuit */
            border: 1px solid #334155 !important;
            border-radius: 12px !important;
            padding: 15px !important;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2) !important;
        }

        /* Texte BLANC à l'intérieur des cartes sombres */
        .stMetric label, .stMetric div, .stMetric span, .stMetric p,
        .oracle-box span, .oracle-box div, .oracle-box p,
        .result-box span, .result-box div {
             color: #F8FAFC !important;
        }

        /* 4. CHIFFRES CLÉS DANS LES CARTES */
        [data-testid="stMetricLabel"] { color: #CBD5E1 !important; font-size: 14px !important; }
        [data-testid="stMetricValue"] { color: #FFD700 !important; font-family: 'Orbitron', sans-serif !important; font-size: 32px !important; }

        /* 5. BOUTONS & INPUTS */
        .stTextInput label, .stNumberInput label, .stSelectbox label {
            color: #334155 !important; font-weight: bold !important;
        }
        .stButton button {
            background-color: #2563EB !important; color: white !important;
            border: none !important; font-weight: bold !important;
        }
        .stButton button:hover { background-color: #1D4ED8 !important; }
        
        /* 6. PIED DE PAGE */
        .branding-footer {
            text-align: center; color: #64748B !important; font-size: 12px; margin-top: 50px; 
            border-top: 1px solid #E2E8F0; padding-top: 20px;
        }
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
        st.markdown(f"<div style='text-align: center; color: #64748B; margin-bottom: 20px;'>Powered by <b>{COMPANY_NAME}</b></div>", unsafe_allow_html=True)
        
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
    st.session_state.user = dict(user_fresh) if user_fresh else None
    if st.session_state.user is None: st.rerun() # Securité si user supprimé
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
        with st.expander("💎 PASSER PRO (OFFRE)", expanded=True):
            st.info("""
            **🚀 DÉBLOQUEZ TOUT :**
            ✅ Historique Illimité
            ✅ Export Excel
            ✅ Création d'appareils sur mesure
            """)
            st.markdown("### 🏷️ Tarif : 5 000 FCFA / an")
            st.warning("""
            **COMMENT ACTIVER ?**
            1️⃣ Dépôt OM/MOMO au :
            **671 89 40 95** (Emeric T.)
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
    st.markdown(f"<div style='text-align: center; color: #64748B; font-size: 12px; margin-top: 20px;'>Développé par<br><b style='color: #D97706'>{COMPANY_NAME}</b></div>", unsafe_allow_html=True)

# HEADER
st.markdown(f"<h1>Tableau de Bord Énergétique</h1>", unsafe_allow_html=True)

tabs_titles = ["🔮 ORACLE", "📜 HISTORIQUE", "⚙️ AUDIT & CONFIG", "👤 PROFIL"]
if IS_ADMIN: tabs_titles.append("🛠️ ADMIN")
tabs = st.tabs(tabs_titles)

# TAB 1: ORACLE
with tabs[0]:
    if not prof: st.warning("👋 Bienvenue ! Commencez par faire votre **Audit Énergétique** dans l'onglet ⚙️ AUDIT.")
    else:
        c1, c2 = st.columns([1, 1])
        with c1:
            st.markdown("### 🔌 Nouvelle Recharge")
            with st.form("oracle"):
                m = st.number_input("Montant (FCFA)", 500, 500000, 5000, step=500)
                t = st.text_input("Code Token", type="password")
                if st.form_submit_button("CALCULER", use_container_width=True):
                    cat = determiner_cat(prof['conso_jour'])
                    kwh, tva, prix = calcul_kwh(m, cumul_val, cat)
                    with db_connection() as conn:
                        new_c = cumul_val + float(kwh)
                        conn.execute("INSERT OR REPLACE INTO etats_mensuels VALUES (?, ?, ?)", (USER_ID, mois, new_c))
                        ht = "REF-" + hashlib.sha256(f"{SALT}{t}".encode()).hexdigest()[:8] if t else "N/A"
                        conn.execute("INSERT INTO historique (user_id, date, montant, kwh, token_ref, cumul_apres) VALUES (?, ?, ?, ?, ?, ?)",
                                  (USER_ID, datetime.now(FUSEAU).strftime("%d/%m %H:%M"), m, float(kwh), ht, new_c))
                        conn.commit()
                    st.success(f"✅ +{kwh:.1f} kWh"); time.sleep(1); st.rerun()
        with c2:
            st.markdown("### 📊 État Actuel")
            st.markdown(f"""<div class="oracle-box"><span style="color:#CBD5E1">CUMUL DU MOIS</span><br><span class="big-font">{cumul_val:.1f} kWh</span><br><span style="font-size:12px; color:#93C5FD">Tranche : {determiner_cat(prof['conso_jour'])}</span></div>""", unsafe_allow_html=True)
            if est_pro:
                jours = 5 
                st.info(f"🔮 **PRO :** Coupure estimée le **{(datetime.now(FUSEAU)+timedelta(days=jours)).strftime('%d/%m à %Hh')}**")
            else: st.warning("🔮 Autonomie env. **5 jours**")

# TAB 2: HISTORIQUE
with tabs[1]:
    lim = 100 if est_pro else 3
    with db_connection() as conn: 
        df = pd.read_sql("SELECT date as 'Date', montant as 'Montant', kwh as 'kWh', token_ref as 'Ref' FROM historique WHERE user_id=? ORDER BY id DESC LIMIT ?", conn, params=(USER_ID, lim))
    if not df.empty: st.dataframe(df, use_container_width=True, hide_index=True)
    else: st.info("Vide.")
    if not est_pro: st.warning("🔒 Historique limité. Passez PRO.")

# TAB 3: AUDIT & CONFIG
with tabs[2]:
    st.write("### 🏗️ Parc Électrique")
    cat_dict = get_catalogue_pareto()
    c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
    with c1: cat = st.selectbox("Catégorie", list(cat_dict.keys()))
    with c2: 
        opts = list(cat_dict[cat].keys()); 
        if est_pro: opts.append("➕ Créer")
        item = st.selectbox("Appareil", opts)
    nom=item; p_def=cat_dict[cat].get(item,0)
    if item=="➕ Créer": nom=st.text_input("Nom"); p_def=st.number_input("W",1,9999,100)
    with c3: pa = st.number_input("Puissance (Watts)", value=int(p_def), disabled=not est_pro)
    with c4: 
        q = st.number_input("Qté", 1, 20, 1)
        if st.button("Ajouter", use_container_width=True):
            inv.append({"nom": nom, "p": pa, "q": q, "h": 5.0})
            with db_connection() as conn: conn.execute("INSERT OR REPLACE INTO profils (user_id, budget, conso_jour, label, config_json) VALUES (?, 0, 0, 'Auto', ?)", (USER_ID, json.dumps(inv))); conn.commit()
            st.rerun()
    st.divider()
    if inv:
        tp=0; tk=0
        for i, it in enumerate(inv):
            cc1, cc2, cc3, cc4 = st.columns([3, 2, 2, 1])
            with cc1: st.write(f"**{it['nom']}** (x{it['q']})")
            with cc2: st.write(f"{it['p']} W")
            with cc3: it['h'] = st.slider(f"Heures", 0., 24., float(it['h']), 0.5, key=f"h_{i}", label_visibility="collapsed")
            with cc4: 
                if st.button("🗑️", key=f"d_{i}"):
                    del inv[i]; 
                    with db_connection() as conn: conn.execute("UPDATE profils SET config_json=? WHERE user_id=?", (json.dumps(inv), USER_ID)); conn.commit()
                    st.rerun()
            tp+=it['p']*it['q']; tk+=(it['p']*it['q']*it['h'])/1000
        if st.button("💾 Mettre à jour"):
             with db_connection() as conn: conn.execute("UPDATE profils SET config_json=?, conso_jour=? WHERE user_id=?", (json.dumps(inv), tk, USER_ID)); conn.commit()
             st.rerun()
        st.markdown("---")
        st.metric("Puissance Installée (kW)", f"{tp/1000:.2f} kW")

# TAB 4: PROFIL
with tabs[3]:
    st.write("### 👤 Mes Informations")
    st.caption("Ces informations nous aident à sécuriser votre compte.")
    curr_u = st.session_state.user
    with st.form("profil_form"):
        col_p1, col_p2 = st.columns(2)
        with col_p1:
            new_fname = st.text_input("Nom", value=curr_u['first_name'] or "")
            new_phone = st.text_input("Téléphone", value=curr_u['phone'] or "")
        with col_p2:
            new_lname = st.text_input("Prénom", value=curr_u['last_name'] or "")
            new_meter = st.text_input("Numéro de Compteur", value=curr_u['meter_number'] or "")
        if st.form_submit_button("💾 ENREGISTRER MES INFOS", use_container_width=True):
            if update_profile(USER_ID, new_fname, new_lname, new_phone, new_meter):
                st.success("Profil mis à jour !"); time.sleep(1); st.rerun()
            else: st.error("Erreur.")
    st.markdown("---")
    with st.expander("🔒 Modifier mon Mot de passe"):
        with st.form("pwd_change"):
            old = st.text_input("Ancien mot de passe", type="password")
            n1 = st.text_input("Nouveau mot de passe", type="password")
            n2 = st.text_input("Confirmer le nouveau", type="password")
            if st.form_submit_button("Changer le mot de passe"):
                if n1 != n2: st.error("Les mots de passe ne correspondent pas.")
                elif len(n1) < 4: st.error("Trop court.")
                elif change_password(USER_ID, old, n1):
                    st.success("Mot de passe changé ! Reconnexion requise."); time.sleep(2)
                    st.session_state.user = None; st.rerun()
                else: st.error("Ancien mot de passe incorrect.")

# TAB 5: ADMIN (AVEC SYSTEME DE SAUVEGARDE)
if IS_ADMIN:
    with tabs[4]:
        st.header("🛠️ Cockpit de Pilotage")
        
        # --- NOUVEAU : SYSTEME DE SAUVEGARDE ---
        st.error("🚨 ZONE DE DANGER : SAUVEGARDE DES DONNÉES")
        st.caption("Le serveur Cloud Gratuit efface les données au redémarrage. SAUVEGARDEZ TOUS LES SOIRS.")
        
        col_save1, col_save2 = st.columns(2)
        with col_save1:
            # BOUTON DOWNLOAD
            with open(DB_FILE, "rb") as f:
                btn = st.download_button(
                    label="📥 TÉLÉCHARGER LA BASE DE DONNÉES (BACKUP)",
                    data=f,
                    file_name=f"backup_wattcheck_{datetime.now().strftime('%Y%m%d_%H%M')}.db",
                    mime="application/x-sqlite3",
                    type="primary"
                )
        with col_save2:
            # UPLOAD RESTAURATION
            uploaded_db = st.file_uploader("📤 RESTAURER UNE SAUVEGARDE", type=["db"])
            if uploaded_db is not None:
                if st.button("⚠️ CONFIRMER LA RESTAURATION"):
                    with open(DB_FILE, "wb") as f:
                        f.write(uploaded_db.getbuffer())
                    st.success("Base de données restaurée !"); time.sleep(1); st.rerun()
        
        st.divider()
        
        with db_connection() as conn:
            users_df = pd.read_sql("SELECT id, username, first_name, last_name, phone, is_pro, created_at FROM users WHERE username != 'admin'", conn)
            lic_df = pd.read_sql("SELECT * FROM licences", conn)
        
        total_inscrits = len(users_df)
        total_pro = len(users_df[users_df['is_pro'] == 1])
        ca_estime = total_pro * 5000
        
        k1, k2, k3 = st.columns(3)
        k1.metric("👥 Total Inscrits", total_inscrits)
        k2.metric("💎 Abonnés PRO", total_pro)
        k3.metric("💰 CA Estimé", f"{ca_estime:,} FCFA")
        
        st.divider()

        c1, c2 = st.columns([1, 2])
        with c1:
            st.subheader("🔑 Générer Licence")
            if st.button("✨ CRÉER UN CODE (1 AN)"):
                code = gen_licence(USER_ID)
                st.success(code)
                st.info("Copiez et envoyez au client.")
        
        with c2:
            st.subheader("📋 Base Utilisateurs")
            if not users_df.empty:
                st.dataframe(
                    users_df[['username', 'phone', 'is_pro', 'created_at']], 
                    use_container_width=True,
                    column_config={"is_pro": st.column_config.CheckboxColumn("PRO ?"), "created_at": "Date"}
                )
            else:
                st.info("Aucun utilisateur.")
        
        st.markdown("---")
        with st.expander("📂 Voir l'historique des Licences"):
            st.dataframe(lic_df)

st.markdown(f"<div class='branding-footer'>© 2026 <span class='company-name'>{COMPANY_NAME}</span> | {APP_NAME} {VERSION}</div>", unsafe_allow_html=True)