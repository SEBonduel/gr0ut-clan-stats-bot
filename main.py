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
from datetime import datetime, timezone
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
    d = datetime.now(ZoneInfo("Europe/Paris"))
    return f"{JOURS_FR[d.weekday()]} {d.day} {MOIS_FR[d.month - 1]} {d.year}"


def leaderboard_targets():
    """Clans à classer : [{clan_id, name, webhook}] via LEADERBOARD_TARGETS."""
    raw = os.environ.get("LEADERBOARD_TARGETS", "").strip()
    if raw:
        return json.loads(raw)
    return [{"clan_id": CLAN_ID, "name": "GR0UT",
             "webhook": LEADERBOARD_WEBHOOK_URL}]


def load_snapshot_all():
    """Snapshots par clan {clan_id: {taken_at, stats}} (migre l'ancien format)."""
    data = load_snapshot()
    if not data:
        return {}
    if "stats" in data and "taken_at" in data:  # ancien format mono-clan
        return {str(CLAN_ID): data}
    return data


def _current_stats(clan_id):
    members = {m["account_id"]: m["name"] for m in fetch_members(clan_id)}
    accounts = fetch_accounts(list(members))
    current = {}
    for aid, info in accounts.items():
        st = (info.get("statistics") or {}).get("all") or {}
        if st.get("battles") is not None:
            current[str(aid)] = {
                "battles": st["battles"], "wins": st["wins"],
                "damage_dealt": st["damage_dealt"], "xp": st["xp"],
            }
    return members, current


def report_leaderboard(clan_id, name, webhook, snapshot_all):
    members, current = _current_stats(clan_id)
    key = str(clan_id)
    prev = (snapshot_all.get(key) or {}).get("stats")
    snapshot_all[key] = {"taken_at": datetime.now(timezone.utc).isoformat(),
                         "stats": current}

    if not prev:
        print(f"leaderboard[{name}]: snapshot initial, classement au prochain run.")
        return

    rows = []
    for aid, cur in current.items():
        old = prev.get(aid)
        if not old:
            continue
        db = cur["battles"] - old["battles"]
        if db < MIN_BATTLES:
            continue
        dw = cur["wins"] - old["wins"]
        dd = cur["damage_dealt"] - old["damage_dealt"]
        dx = cur["xp"] - old["xp"]
        rows.append({
            "name": members.get(int(aid), aid),
            "battles": db, "winrate": 100 * dw / db,
            "avg_dmg": dd / db, "total_xp": dx, "total_dmg": dd,
        })

    rows.sort(key=lambda r: r["total_dmg"], reverse=True)
    top = rows[:3]
    if not top:
        desc = (f"Personne n'a joué au moins {MIN_BATTLES} batailles "
                "sur la période. 😴")
    else:
        lines = []
        for i, r in enumerate(top):
            lines.append(
                f"{MEDALS[i]} **{r['name']}**\n"
                f"　{r['battles']} batailles · {r['winrate']:.0f}% victoires · "
                f"{r['avg_dmg']:.0f} dégâts/bataille · {r['total_xp']:,} XP".replace(",", " ")
            )
        desc = "\n\n".join(lines)

    post_embed({
        "title": f"🏆 {name} — Top 3 · {today_fr()}",
        "description": desc,
        "color": 0xF1C40F,
        "footer": {"text": f"{name} • Clan Stats • par dégâts totaux · min {MIN_BATTLES} batailles"},
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
