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

INACTIVITY_DAYS = int(os.environ.get("INACTIVITY_DAYS", "28"))
MIN_BATTLES = int(os.environ.get("MIN_BATTLES", "5"))  # seuil pour le leaderboard
SNAPSHOT_FILE = os.environ.get("SNAPSHOT_FILE", "snapshot.json")
WN8_EXP_FILE = os.environ.get("WN8_EXP_FILE", "wn8exp.json")  # valeurs attendues (XVM)
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "gr0ut-clan-stats/1.0"})


# --- API ---------------------------------------------------------------------

def api_get(path, **params):
    params["application_id"] = APP_ID
    url = f"{API_BASE}/{path.strip('/')}/"
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"API error on {path}: {payload.get('error')}")
    return payload["data"]


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
    for m in members:
        info = accounts.get(m["account_id"])
        lbt = (info or {}).get("last_battle_time")
        if not lbt:  # profil privé ou jamais joué -> on signale à part
            inactive.append((m["name"], None))
            continue
        days = (now - lbt) / 86400
        if days >= INACTIVITY_DAYS:
            inactive.append((m["name"], int(days)))

    inactive.sort(key=lambda x: (x[1] is not None, -(x[1] or 0)))
    if not inactive:
        desc = f"✅ Aucun membre inactif depuis plus de {INACTIVITY_DAYS} jours. GG !"
    else:
        lines = []
        for name, days in inactive:
            if days is None:
                lines.append(f"• **{name}** — profil privé / jamais joué")
            else:
                lines.append(f"• **{name}** — {days} jours sans bataille")
        desc = "\n".join(lines)

    post_embed({
        "title": f"📉 {clan_name} — inactifs (> {INACTIVITY_DAYS} jours) : {len(inactive)}",
        "description": desc[:4000],
        "color": 0xE67E22,
        "footer": {"text": f"{clan_name} • Clan Stats"},
    }, webhook)
    print(f"inactivity[{clan_name}]: {len(inactive)} membre(s) signalé(s).")


def cmd_inactivity():
    for t in inactivity_targets():
        report_inactivity(t["clan_id"], t.get("name", t["clan_id"]),
                          t.get("webhook") or INACTIVITY_WEBHOOK_URL)


# --- Commande : leaderboard du jour ------------------------------------------

MEDALS = ["🥇", "🥈", "🥉"]
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
            new_players[said] = fetch_tank_stats(aid)
            continue
        if (last_bt.get(aid) or 0) <= prev_ts:        # aucune bataille depuis le snapshot
            if said in prev_players:
                new_players[said] = prev_players[said]  # baseline inchangée, pas d'appel API
            continue
        cur = fetch_tank_stats(aid)
        new_players[said] = cur
        sess = _delta_session(cur, prev_players.get(said))
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
    top = rows[:3]
    if not top:
        desc = (f"Personne n'a joué au moins {MIN_BATTLES} batailles "
                "sur la période. 😴")
    else:
        lines = []
        for i, r in enumerate(top):
            line = (
                f"{MEDALS[i]} **{r['name']}** — WN8 **{r['wn8']:,.0f}**\n"
                f"　{r['battles']} batailles · tier {r['avg_tier']:.1f} · "
                f"{r['avg_dmg']:,.0f} dmg/bat · {r['avg_spot']:.1f} spot/bat · "
                f"{r['winrate']:.0f}% WR"
            )
            lines.append(line.replace(",", " "))
        desc = "\n\n".join(lines)

    post_embed({
        "title": f"🏆 {name} — Top 3 · {today_fr()}",
        "description": desc,
        "color": 0xF1C40F,
        "footer": {"text": f"{name} • Clan Stats • WN8 de session · min {MIN_BATTLES} batailles"},
    }, webhook)
    print(f"leaderboard[{name}]: {len(top)} au podium / {len(rows)} actifs.")


def cmd_leaderboard():
    snapshot_all = load_snapshot_all()
    for t in leaderboard_targets():
        report_leaderboard(t["clan_id"], t.get("name", t["clan_id"]),
                          t.get("webhook") or LEADERBOARD_WEBHOOK_URL, snapshot_all)
    save_snapshot(snapshot_all)


# --- Entrée ------------------------------------------------------------------

COMMANDS = {"inactivity": cmd_inactivity, "leaderboard": cmd_leaderboard}


def main():
    if not APP_ID:
        sys.exit("WG_APP_ID manquant.")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in COMMANDS:
        sys.exit(f"Usage: python main.py [{'|'.join(COMMANDS)}]")
    COMMANDS[cmd]()


if __name__ == "__main__":
    main()
