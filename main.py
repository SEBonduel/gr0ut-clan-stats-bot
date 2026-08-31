#!/usr/bin/env python3
"""
GR0UT — Bot "Clan Stats".

Deux commandes :
  python main.py inactivity   -> liste les membres sans bataille depuis N jours
  python main.py leaderboard  -> top 3 des joueurs sur les dernières ~24h

Données : API publique Wargaming (EU). Poste dans Discord via webhook.
Le leaderboard compare les stats cumulées à un snapshot quotidien (snapshot.json)
que le workflow GitHub Actions committe d'un run à l'autre.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

APP_ID = os.environ.get("WG_APP_ID", "").strip()
CLAN_ID = int(os.environ.get("WG_CLAN_ID", "500165786"))
# Webhook par défaut, + webhooks dédiés optionnels (sinon on retombe sur le défaut).
STATS_WEBHOOK_URL = os.environ.get("STATS_WEBHOOK_URL", "").strip()
LEADERBOARD_WEBHOOK_URL = (
    os.environ.get("LEADERBOARD_WEBHOOK_URL", "").strip() or STATS_WEBHOOK_URL
)
INACTIVITY_WEBHOOK_URL = (
    os.environ.get("INACTIVITY_WEBHOOK_URL", "").strip() or STATS_WEBHOOK_URL
)
API_BASE = os.environ.get("WG_API_BASE", "https://api.worldoftanks.eu")
# Portail clan (API interne, non documentée) : seule source du nb de batailles
# Bastion/Incursions par membre. Publique (pas d'auth), mais spécifique région.
PORTAL_BASE = os.environ.get("WG_PORTAL_BASE", "https://eu.wargaming.net").rstrip("/")

INACTIVITY_DAYS = int(os.environ.get("INACTIVITY_DAYS", "28"))
MIN_BATTLES = int(os.environ.get("MIN_BATTLES", "5"))  # seuil pour le leaderboard
TOP_N = int(os.environ.get("TOP_N", "5"))  # taille du classement (podium)
# Radar d'inactivité, 2e section : jeu en équipe (Bastion + Incursions).
# Sous ce nombre de batailles Bastion/Incursions sur 28 j, le membre est
# signalé comme ne participant pas au jeu d'équipe (contribution ~nulle aux
# ressources industrielles, qui ne se gagnent que dans ces modes).
MIN_BASTION_BATTLES = int(os.environ.get("MIN_BASTION_BATTLES", "10"))
# Classement positif « top contributeurs Bastion » (Escarmouches + Incursions).
BASTION_TOP_DAYS = int(os.environ.get("BASTION_TOP_DAYS", "7"))  # 1, 7 ou 28
BASTION_TOP_N = int(os.environ.get("BASTION_TOP_N", "10"))
# Jour où le Top Bastion est ajouté au leaderboard (lun=0 … dim=6). Défaut dimanche.
BASTION_TOP_WEEKDAY = int(os.environ.get("BASTION_TOP_WEEKDAY", "6"))
SNAPSHOT_FILE = os.environ.get("SNAPSHOT_FILE", "snapshot.json")
WN8_EXP_FILE = os.environ.get("WN8_EXP_FILE", "wn8exp.json")  # valeurs attendues (XVM)
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

# --- Promotion auto recrue -> soldat (GR0UT uniquement) ----------------------
# Passe les recrues à « soldat » après PROMOTE_AFTER_DAYS jours dans le clan.
# Utilise l'API interne du portail clan (endpoint change_role), qui exige une
# SESSION authentifiée d'officier : le cookie complet est fourni via le secret
# WG_PORTAL_COOKIE (à recoller le jour où il expire ; le bot prévient sur Discord).
PROMOTE_CLAN_ID = int(os.environ.get("PROMOTE_CLAN_ID", "500165786"))  # GR0UT
PROMOTE_AFTER_DAYS = int(os.environ.get("PROMOTE_AFTER_DAYS", "30"))
PROMOTE_WEBHOOK_URL = (
    os.environ.get("PROMOTE_WEBHOOK_URL", "").strip() or INACTIVITY_WEBHOOK_URL
)
WG_PORTAL_COOKIE = os.environ.get("WG_PORTAL_COOKIE", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "gr0ut-clan-stats/1.0"})
# Erreurs WG transitoires : on retente au lieu de faire planter tout le run.
TRANSIENT = {"SOURCE_NOT_AVAILABLE", "REQUEST_LIMIT_EXCEEDED"}


# --- API ---------------------------------------------------------------------

def api_get(path, _retries=3, **params):
    params["application_id"] = APP_ID
    url = f"{API_BASE}/{path.strip('/')}/"
    last = None
    for attempt in range(_retries):
        try:
            r = SESSION.get(url, params=params, timeout=30)
            r.raise_for_status()
            payload = r.json()
            if payload.get("status") == "ok":
                return payload["data"]
            err = payload.get("error") or {}
            last = RuntimeError(f"API error on {path}: {err}")
            if err.get("message") not in TRANSIENT:
                raise last
        except requests.RequestException as exc:
            last = exc
        time.sleep(2 * (attempt + 1))
    raise last


def fetch_members(clan_id=CLAN_ID):
    """[{account_id, name}] des membres d'un clan."""
    data = api_get("wgn/clans/info", clan_id=clan_id,
                   fields="members.account_id,members.account_name", game="wot")
    members = (data.get(str(clan_id)) or {}).get("members") or []
    return [{"account_id": m["account_id"], "name": m["account_name"]}
            for m in members]


def fetch_accounts(ids):
    """account/info par lots de 100 -> {account_id: info}."""
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = api_get(
            "wot/account/info",
            account_id=",".join(map(str, chunk)),
            fields=("nickname,last_battle_time,statistics.all.battles,"
                    "statistics.all.wins,statistics.all.damage_dealt,"
                    "statistics.all.xp"),
        )
        out.update({int(k): v for k, v in data.items() if v})
    return out


def fetch_last_battle_times(ids):
    """{account_id: last_battle_time} — pour ne re-fetcher que les joueurs actifs."""
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = api_get("wot/account/info",
                       account_id=",".join(map(str, chunk)),
                       fields="last_battle_time")
        out.update({int(k): (v or {}).get("last_battle_time")
                    for k, v in data.items() if v})
    return out


BASTION_DAYS = 28  # seule fenêtre proposée par le portail (1 / 7 / 28 j)
BASTION_BATTLE_TYPES = ("sortie", "incursion")  # Escarmouches (Bastion) + Incursions


def fetch_bastion_activity(clan_id, days=BASTION_DAYS):
    """{account_id: nb batailles Bastion+Incursions sur `days` j} via le portail.

    Source : endpoint interne `/clans/wot/<id>/api/players/` (public, sans auth,
    mais exige l'en-tête AJAX). Le portail n'accepte que 1 / 7 / 28 j. Renvoie None
    si les stats du clan sont masquées ou si l'endpoint est indisponible.
    """
    timeframe = min((1, 7, 28), key=lambda w: abs(w - days))  # borne aux valeurs offertes
    totals = {}
    for bt in BASTION_BATTLE_TYPES:
        url = f"{PORTAL_BASE}/clans/wot/{clan_id}/api/players/"
        try:
            r = SESSION.get(
                url,
                params={"offset": 0, "limit": 500, "order": "-role",
                        "timeframe": timeframe, "battle_type": bt},
                headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  warn: activité Bastion ({bt}) indispo pour {clan_id} ({exc}).")
            return None
        if data.get("is_hidden_statistics"):
            print(f"  info: stats du clan {clan_id} masquées ; section Bastion ignorée.")
            return None
        for p in data.get("items", []):
            totals[p["id"]] = totals.get(p["id"], 0) + (p.get("battles_count") or 0)
    return totals


def fetch_tank_stats(account_id):
    """Stats cumulées par char d'un compte : {tank_id: [battles, wins, dmg, frags, spot, def]}."""
    data = api_get("wot/tanks/stats", account_id=account_id,
                   fields=("tank_id,all.battles,all.wins,all.damage_dealt,"
                           "all.frags,all.spotted,all.dropped_capture_points"))
    tanks = data.get(str(account_id)) or []
    out = {}
    for t in tanks:
        a = t.get("all") or {}
        if not a.get("battles"):
            continue
        out[str(t["tank_id"])] = [
            a["battles"], a.get("wins", 0), a.get("damage_dealt", 0),
            a.get("frags", 0), a.get("spotted", 0),
            a.get("dropped_capture_points", 0),
        ]
    return out


def fetch_tank_tiers(tank_ids):
    """{tank_id: tier} depuis l'encyclopédie, pour le tier moyen de session."""
    out = {}
    ids = list(tank_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        data = api_get("wot/encyclopedia/vehicles",
                       tank_id=",".join(map(str, chunk)), fields="tier")
        for k, v in (data or {}).items():
            if v and v.get("tier"):
                out[int(k)] = v["tier"]
    return out


# --- WN8 ---------------------------------------------------------------------

def load_expected():
    """{tank_id: (expDamage, expFrag, expSpot, expDef, expWinRate)} depuis wn8exp.json."""
    try:
        with open(WN8_EXP_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {int(t["IDNum"]): (t["expDamage"], t["expFrag"], t["expSpot"],
                              t["expDef"], t["expWinRate"])
            for t in raw.get("data", [])}


EXPECTED = load_expected()


def session_wn8(sess):
    """WN8 de session (formule officielle) sur des deltas par char, ou None si incalculable.

    sess : {tank_id_str: {battles, wins, damage, frags, spot, defp}}.
    Les chars absents de la table de valeurs attendues sont ignorés du calcul.
    """
    tb = td = tf = ts = tdef = tw = 0
    ed = ef = es = edef = ew = 0.0
    for tid, s in sess.items():
        exp = EXPECTED.get(int(tid))
        if not exp:
            continue
        b = s["battles"]
        tb += b
        td += s["damage"]; tf += s["frags"]; ts += s["spot"]
        tdef += s["defp"]; tw += s["wins"]
        ed += exp[0] * b; ef += exp[1] * b; es += exp[2] * b
        edef += exp[3] * b; ew += exp[4] * b
    if tb == 0 or ed <= 0:
        return None
    r_dmg = td / ed
    r_frag = tf / ef if ef else 0
    r_spot = ts / es if es else 0
    r_def = tdef / edef if edef else 0
    r_win = (100 * tw) / ew if ew else 0
    c_win = max(0, (r_win - 0.71) / 0.29)
    c_dmg = max(0, (r_dmg - 0.22) / 0.78)
    c_frag = max(0, min(c_dmg + 0.2, (r_frag - 0.12) / 0.88))
    c_spot = max(0, min(c_dmg + 0.1, (r_spot - 0.38) / 0.62))
    c_def = max(0, min(c_dmg + 0.1, (r_def - 0.10) / 0.90))
    return (980 * c_dmg + 210 * c_dmg * c_frag + 155 * c_frag * c_spot
            + 75 * c_def * c_frag + 145 * min(1.8, c_win))


# --- Discord -----------------------------------------------------------------

def post_embed(embed, webhook):
    body = {"embeds": [embed]}
    if DRY_RUN or not webhook:
        print("[DRY-RUN] Discord embed:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return
    r = SESSION.post(webhook, json=body, timeout=20)
    r.raise_for_status()


# --- Snapshot ----------------------------------------------------------------

def load_snapshot():
    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_snapshot(snap):
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, ensure_ascii=False, indent=2)


# --- Commande : inactivité ---------------------------------------------------

def inactivity_targets():
    """Liste des clans à surveiller : [{clan_id, name, webhook}].

    Défini par le secret JSON INACTIVITY_TARGETS, sinon le clan primaire seul.
    """
    raw = os.environ.get("INACTIVITY_TARGETS", "").strip()
    if raw:
        return json.loads(raw)
    return [{"clan_id": CLAN_ID, "name": "GR0UT",
             "webhook": INACTIVITY_WEBHOOK_URL}]


def report_inactivity(clan_id, clan_name, webhook):
    now = datetime.now(timezone.utc).timestamp()
    members = fetch_members(clan_id)
    accounts = fetch_accounts([m["account_id"] for m in members])

    inactive = []
    afk_ids = set()  # membres déjà signalés comme réellement inactifs
    for m in members:
        info = accounts.get(m["account_id"])
        lbt = (info or {}).get("last_battle_time")
        if not lbt:  # profil privé ou jamais joué -> on signale à part
            inactive.append((m["name"], None))
            afk_ids.add(m["account_id"])
            continue
        days = (now - lbt) / 86400
        if days >= INACTIVITY_DAYS:
            inactive.append((m["name"], int(days)))
            afk_ids.add(m["account_id"])

    inactive.sort(key=lambda x: (x[1] is not None, -(x[1] or 0)))
    if not inactive:
        lines = [f"✅ Aucun membre inactif depuis plus de {INACTIVITY_DAYS} jours. GG !"]
    else:
        lines = []
        for name, days in inactive:
            if days is None:
                lines.append(f"• **{name}** — profil privé / jamais joué")
            else:
                lines.append(f"• **{name}** — {days} jours sans bataille")

    # --- 2e section : peu ou pas de jeu d'équipe (Bastion + Incursions, 28 j) ---
    # Signalés à part des vrais AFK : ils jouent, mais pas en équipe (donc ~0
    # ressource industrielle). Les membres déjà comptés comme AFK sont exclus.
    activity = fetch_bastion_activity(clan_id)
    low = None
    if activity is not None:
        low = sorted(
            ((m["name"], activity.get(m["account_id"], 0)) for m in members
             if m["account_id"] not in afk_ids
             and activity.get(m["account_id"], 0) < MIN_BASTION_BATTLES),
            key=lambda x: x[1],
        )
        lines.append("")  # ligne vide : sépare les vrais AFK du reste
        lines.append(f"__📦 Moins de {MIN_BASTION_BATTLES} batailles d'équipe "
                     f"(Bastion + Incursions) sur {BASTION_DAYS} j :__")
        if not low:
            lines.append("✅ Tout le monde joue en équipe. 💪")
        else:
            for name, n in low:
                s = "s" if n != 1 else ""
                lines.append(f"• **{name}** — {n} bataille{s} Bastion/Incursion")

    post_embed({
        "title": f"📉 {clan_name} — inactifs (> {INACTIVITY_DAYS} jours) : {len(inactive)}",
        "description": "\n".join(lines)[:4000],
        "color": 0xE67E22,
        "footer": {"text": f"{clan_name} • Clan Stats"},
    }, webhook)
    print(f"inactivity[{clan_name}]: {len(inactive)} AFK, "
          f"{'n/a' if low is None else len(low)} sous le seuil Bastion.")


def cmd_inactivity():
    for t in inactivity_targets():
        report_inactivity(t["clan_id"], t.get("name", t["clan_id"]),
                          t.get("webhook") or INACTIVITY_WEBHOOK_URL)


# --- Commande : leaderboard du jour ------------------------------------------

MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def rank_marker(i):
    """Marqueur du rang i (0-indexé) : médailles puis chiffres, fallback '11.'…"""
    return MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]


def today_fr():
    # Date de la SOIRÉE jouée : -6h pour qu'un run après minuit (ou en soirée)
    # retombe toujours sur le bon jour de CW.
    d = datetime.now(ZoneInfo("Europe/Paris")) - timedelta(hours=6)
    return f"{JOURS_FR[d.weekday()]} {d.day} {MOIS_FR[d.month - 1]} {d.year}"


def leaderboard_targets():
    """Clans à classer : [{clan_id, name, webhook}] via LEADERBOARD_TARGETS."""
    raw = os.environ.get("LEADERBOARD_TARGETS", "").strip()
    if raw:
        return json.loads(raw)
    return [{"clan_id": CLAN_ID, "name": "GR0UT",
             "webhook": LEADERBOARD_WEBHOOK_URL}]


def load_snapshot_all():
    """Snapshots par clan {clan_id: {taken_at, players}} (migre les anciens formats)."""
    data = load_snapshot()
    if not data:
        return {}
    if "stats" in data and "taken_at" in data:  # très ancien format mono-clan
        return {str(CLAN_ID): data}
    return data


def _delta_session(cur, base):
    """Deltas de session par char entre deux relevés cumulés (par char)."""
    sess = {}
    for tid, c in cur.items():
        b0 = base.get(tid) if base else None
        db = c[0] - (b0[0] if b0 else 0)
        if db <= 0:
            continue
        sess[tid] = {
            "battles": db,
            "wins": c[1] - (b0[1] if b0 else 0),
            "damage": c[2] - (b0[2] if b0 else 0),
            "frags": c[3] - (b0[3] if b0 else 0),
            "spot": c[4] - (b0[4] if b0 else 0),
            "defp": c[5] - (b0[5] if b0 else 0),
        }
    return sess


def report_leaderboard(clan_id, name, webhook, snapshot_all):
    key = str(clan_id)
    prev = snapshot_all.get(key) or {}
    prev_players = prev.get("players")  # None => pas de baseline v2 (re-seed)
    prev_ts = 0.0
    if prev.get("taken_at"):
        try:
            prev_ts = datetime.fromisoformat(prev["taken_at"]).timestamp()
        except ValueError:
            prev_ts = 0.0

    members = {m["account_id"]: m["name"] for m in fetch_members(clan_id)}
    # Sans baseline on doit tout re-fetcher ; sinon on cible les joueurs actifs.
    last_bt = fetch_last_battle_times(list(members)) if prev_players is not None else {}

    new_players = {}
    session = {}  # account_id -> deltas de session par char
    for aid in members:
        said = str(aid)
        if prev_players is None:                      # premier run : on sème la base
            try:
                new_players[said] = fetch_tank_stats(aid)
            except Exception as exc:                  # noqa: BLE001 — un joueur ne doit pas tout casser
                print(f"  warn: seed {aid} échoué ({exc}) ; ré-essai au prochain run.")
            continue
        if (last_bt.get(aid) or 0) <= prev_ts:        # aucune bataille depuis le snapshot
            if said in prev_players:
                new_players[said] = prev_players[said]  # baseline inchangée, pas d'appel API
            continue
        try:
            cur = fetch_tank_stats(aid)
        except Exception as exc:                      # noqa: BLE001
            print(f"  warn: stats {members.get(aid, aid)} indisponibles ({exc}) ; ignoré ce soir.")
            if said in prev_players:
                new_players[said] = prev_players[said]  # on conserve la baseline du joueur
            continue
        new_players[said] = cur
        base = prev_players.get(said)
        if base is None:
            # Nouveau membre : aucune référence -> on sème sa baseline et on
            # l'exclut du classement ce soir (sinon toute sa carrière compterait
            # comme une seule session). Il sera classé dès le prochain run.
            print(f"  info: {members.get(aid, aid)} nouveau (pas de baseline) ; "
                  "semé, classé au prochain run.")
            continue
        sess = _delta_session(cur, base)
        if sess:
            session[aid] = sess

    snapshot_all[key] = {"taken_at": datetime.now(timezone.utc).isoformat(),
                         "players": new_players}

    if prev_players is None:
        print(f"leaderboard[{name}]: snapshot initial ({len(new_players)} joueurs), "
              "classement au prochain run.")
        return

    tiers = fetch_tank_tiers({int(tid) for s in session.values() for tid in s})

    rows = []
    for aid, sess in session.items():
        battles = sum(s["battles"] for s in sess.values())
        if battles < MIN_BATTLES:
            continue
        wn8 = session_wn8(sess)
        if wn8 is None:
            continue
        dmg = sum(s["damage"] for s in sess.values())
        spot = sum(s["spot"] for s in sess.values())
        wins = sum(s["wins"] for s in sess.values())
        tier_b = sum(s["battles"] for tid, s in sess.items() if int(tid) in tiers)
        tier_w = sum(tiers[int(tid)] * s["battles"]
                     for tid, s in sess.items() if int(tid) in tiers)
        rows.append({
            "name": members.get(aid, aid), "wn8": wn8, "battles": battles,
            "avg_dmg": dmg / battles, "avg_spot": spot / battles,
            "winrate": 100 * wins / battles,
            "avg_tier": (tier_w / tier_b) if tier_b else 0,
        })

    rows.sort(key=lambda r: r["wn8"], reverse=True)
    top = rows[:TOP_N]
    if not top:
        desc = (f"Personne n'a joué au moins {MIN_BATTLES} batailles "
                "sur la période. 😴")
    else:
        lines = []
        for i, r in enumerate(top):
            line = (
                f"{rank_marker(i)} **{r['name']}** — WN8 **{r['wn8']:,.0f}**\n"
                f"　{r['battles']} batailles · tier {r['avg_tier']:.1f} · "
                f"{r['avg_dmg']:,.0f} dmg/bat · {r['avg_spot']:.1f} spot/bat · "
                f"{r['winrate']:.0f}% WR"
            )
            lines.append(line.replace(",", " "))
        desc = "\n\n".join(lines)

    post_embed({
        "title": f"🏆 {name} — Top {TOP_N} · {today_fr()}",
        "description": desc,
        "color": 0xF1C40F,
        "footer": {"text": f"{name} • Clan Stats • WN8 de session · min {MIN_BATTLES} batailles"},
    }, webhook)
    print(f"leaderboard[{name}]: {len(top)} au podium / {len(rows)} actifs.")


def cmd_leaderboard():
    snapshot_all = load_snapshot_all()
    targets = leaderboard_targets()
    for t in targets:
        report_leaderboard(t["clan_id"], t.get("name", t["clan_id"]),
                          t.get("webhook") or LEADERBOARD_WEBHOOK_URL, snapshot_all)
    save_snapshot(snapshot_all)

    # Une fois par semaine, on ajoute le Top contributeurs Bastion au même salon.
    if datetime.now(ZoneInfo("Europe/Paris")).weekday() == BASTION_TOP_WEEKDAY:
        for t in targets:
            report_bastion_top(t["clan_id"], t.get("name", t["clan_id"]),
                               t.get("webhook") or LEADERBOARD_WEBHOOK_URL)


# --- Commande : annonce (mise à jour du calcul des stats) ---------------------

def cmd_announce():
    """Poste une annonce ponctuelle expliquant le nouveau calcul du leaderboard."""
    desc = (
        "Le classement du soir ne récompense plus le **volume** de parties mais la "
        "**régularité et la performance réelle**.\n\n"
        "**Nouveau tri : la WN8 de session** (perf du jour, formule officielle).\n"
        "Chaque joueur affiche désormais :\n"
        "• 🎯 **WN8 de la session**\n"
        "• 🏅 **tier moyen** joué\n"
        "• 💥 **dégâts moyens** par bataille\n"
        "• 👁️ **spot moyen** par bataille\n"
        "• ✅ **% de victoires**\n\n"
        "Fini le classement au total de dégâts/XP : mieux vaut **5 bonnes parties "
        "qu'une soirée de farm**. Minimum toujours fixé à "
        f"**{MIN_BATTLES} batailles** pour apparaître au podium."
    )
    embed = {
        "title": "🔧 Mise à jour du calcul des stats par SEBonduel",
        "description": desc,
        "color": 0x3498DB,
        "footer": {"text": "GR0UT • Clan Stats • WN8 de session"},
    }
    post_embed(embed, LEADERBOARD_WEBHOOK_URL)
    print("announce: annonce postée.")


# --- Commande : promotion auto recrue -> soldat ------------------------------

def _cookie_value(cookie_str, name):
    """Extrait la valeur d'un cookie donné depuis l'en-tête Cookie complet."""
    for part in cookie_str.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


def fetch_recruits_since(clan_id, days):
    """[(account_id, name, jours)] des recrues dans le clan depuis >= days jours."""
    data = api_get("wgn/clans/info", clan_id=clan_id, game="wot",
                   fields="members.account_id,members.account_name,"
                          "members.role,members.joined_at")
    members = (data.get(str(clan_id)) or {}).get("members") or []
    now = datetime.now(timezone.utc).timestamp()
    out = []
    for m in members:
        if m.get("role") != "recruit":
            continue
        j = m.get("joined_at")
        d = int((now - j) / 86400) if j else 0
        if j and d >= days:
            out.append((m["account_id"], m["account_name"], d))
    out.sort(key=lambda x: -x[2])
    return out


def portal_change_role(clan_id, account_id, role="private"):
    """Change le grade d'un membre via l'API interne du portail (session officier).

    Renvoie ('ok'|'already'|'expired'|'error', détail). Nécessite WG_PORTAL_COOKIE.
    """
    csrf = _cookie_value(WG_PORTAL_COOKIE, "csrftoken")
    if not csrf:
        return ("expired", "cookie sans csrftoken")
    url = f"{PORTAL_BASE}/clans/wot/{clan_id}/api/change_role/"
    ref = f"{PORTAL_BASE}/clans/wot/{clan_id}/players/"
    try:
        r = SESSION.post(
            url, data={"user_ids": account_id, "role": role},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": csrf,
                "Cookie": WG_PORTAL_COOKIE,
                "Referer": ref,
                "Origin": PORTAL_BASE,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }, timeout=30, allow_redirects=False,
        )
    except requests.RequestException as exc:
        return ("error", str(exc))
    # Session morte : le portail redirige vers le login ou renvoie 401/403.
    if r.status_code in (301, 302, 401, 403):
        return ("expired", f"HTTP {r.status_code}")
    if r.status_code == 201:
        return ("ok", "201")
    body = (r.text or "")[:200]
    if "role_already_assigned" in body:
        return ("already", "409")
    return ("error", f"HTTP {r.status_code} {body}")


def portal_keep_alive(clan_id):
    """GET léger sur le portail pour garder la session chaude (expiration glissante)."""
    if not WG_PORTAL_COOKIE:
        return
    try:
        SESSION.get(f"{PORTAL_BASE}/clans/wot/{clan_id}/players/",
                    headers={"Cookie": WG_PORTAL_COOKIE}, timeout=20)
    except requests.RequestException:
        pass


def cmd_promote():
    """Passe les recrues de GR0UT à soldat après PROMOTE_AFTER_DAYS jours."""
    portal_keep_alive(PROMOTE_CLAN_ID)  # maintient la session vivante à chaque run
    recruits = fetch_recruits_since(PROMOTE_CLAN_ID, PROMOTE_AFTER_DAYS)
    if not recruits:
        print(f"promote: aucune recrue >= {PROMOTE_AFTER_DAYS} j.")
        return
    if not WG_PORTAL_COOKIE:
        # Pas de session configurée : on prévient sur Discord au lieu d'échouer.
        noms = ", ".join(n for _, n, _ in recruits)
        post_embed({
            "title": "⚠️ Promotions en attente (cookie portail manquant)",
            "description": (f"{len(recruits)} recrue(s) à passer soldat : {noms}\n\n"
                            "Configure le secret `WG_PORTAL_COOKIE` pour l'automatiser."),
            "color": 0xE67E22,
            "footer": {"text": "GR0UT • Promotion auto"},
        }, PROMOTE_WEBHOOK_URL)
        print("promote: cookie manquant ; liste postée.")
        return

    promoted, remaining, expired = [], [], False
    for i, (aid, name, days) in enumerate(recruits):
        if DRY_RUN:
            print(f"[DRY-RUN] promote {name} ({days} j) -> soldat")
            promoted.append((name, days))
            continue
        status, detail = portal_change_role(PROMOTE_CLAN_ID, aid, "private")
        if status == "ok":
            promoted.append((name, days))
            print(f"promote: {name} ({days} j) -> soldat ✅")
        elif status == "already":
            print(f"promote: {name} déjà soldat (skip).")
        elif status == "expired":
            expired = True
            remaining = recruits[i:]  # ceux qu'on n'a pas pu traiter
            print(f"promote: session portail expirée ({detail}) ; arrêt.")
            break
        else:
            print(f"promote: échec {name} ({detail}).")

    if expired:
        lines = [f"• **{n}** — recrue depuis {d} jours" for _, n, d in remaining]
        post_embed({
            "title": "🔒 Session portail expirée — promotions en pause",
            "description": (
                "Le cookie d'authentification du portail a expiré. Recolle le secret "
                "`WG_PORTAL_COOKIE` (voir README) pour réactiver l'automatisation.\n\n"
                f"**À passer soldat à la main en attendant ({len(remaining)}) :**\n"
                + "\n".join(lines)),
            "color": 0xE74C3C,
            "footer": {"text": "GR0UT • Promotion auto"},
        }, PROMOTE_WEBHOOK_URL)
        return

    if promoted:
        lines = [f"• **{n}** — recrue depuis {d} jours" for n, d in promoted]
        post_embed({
            "title": f"🎖️ Promotions : {len(promoted)} recrue(s) passée(s) soldat",
            "description": "\n".join(lines),
            "color": 0x2ECC71,
            "footer": {"text": f"GR0UT • Promotion auto • ≥ {PROMOTE_AFTER_DAYS} j"},
        }, PROMOTE_WEBHOOK_URL)
    print(f"promote: {len(promoted)} promue(s).")


# --- Commande : top contributeurs Bastion (classement positif) ---------------

def report_bastion_top(clan_id, clan_name, webhook):
    activity = fetch_bastion_activity(clan_id, BASTION_TOP_DAYS)
    if activity is None:
        print(f"bastion_top[{clan_name}]: activité indisponible (stats masquées ?).")
        return
    members = {m["account_id"]: m["name"] for m in fetch_members(clan_id)}
    rows = sorted(
        ((members.get(aid, aid), n) for aid, n in activity.items() if n > 0),
        key=lambda x: -x[1],
    )[:BASTION_TOP_N]

    if not rows:
        desc = "Personne n'a joué d'Escarmouche ni d'Incursion sur la période. 😴"
    else:
        lines = []
        for i, (name, n) in enumerate(rows):
            s = "s" if n != 1 else ""
            lines.append(f"{rank_marker(i)} **{name}** — {n} bataille{s}")
        desc = "\n".join(lines)

    fen = {1: "24 h", 7: "7 jours", 28: "28 jours"}.get(
        min((1, 7, 28), key=lambda w: abs(w - BASTION_TOP_DAYS)), f"{BASTION_TOP_DAYS} j")
    post_embed({
        "title": f"🏰 {clan_name} — Top contributeurs Bastion ({fen})",
        "description": desc,
        "color": 0x1ABC9C,
        "footer": {"text": f"{clan_name} • Escarmouches + Incursions • merci à eux 💪"},
    }, webhook)
    print(f"bastion_top[{clan_name}]: {len(rows)} au classement.")


def cmd_bastion_top():
    for t in inactivity_targets():  # mêmes clans/webhooks que le radar d'inactivité
        report_bastion_top(t["clan_id"], t.get("name", t["clan_id"]),
                           t.get("webhook") or INACTIVITY_WEBHOOK_URL)


# --- Entrée ------------------------------------------------------------------

COMMANDS = {"inactivity": cmd_inactivity, "leaderboard": cmd_leaderboard,
            "announce": cmd_announce, "promote": cmd_promote,
            "bastion_top": cmd_bastion_top}


def main():
    if not APP_ID:
        sys.exit("WG_APP_ID manquant.")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in COMMANDS:
        sys.exit(f"Usage: python main.py [{'|'.join(COMMANDS)}]")
    COMMANDS[cmd]()


if __name__ == "__main__":
    main()
