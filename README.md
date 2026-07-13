# GR0UT — Bot Clan Stats

Deux automatisations Discord pour le clan GR0UT (World of Tanks EU), gratuites via
GitHub Actions :

- 📉 **Radar d'inactivité** — chaque lundi, liste les membres sans bataille depuis
  plus de 28 jours (configurable).
- 🏆 **Leaderboard du jour** — chaque soir, le **top 3** des joueurs des dernières
  ~24h, classé par **WN8 de session** (perf réelle par bataille, pas par volume).
  Chaque joueur affiche : WN8, tier moyen, dégâts moyens, spot moyen et % de victoires.

## Comment ça marche

- `main.py inactivity` : récupère les membres (`wgn/clans/info`) + leur
  `last_battle_time` (`account/info`) et signale les inactifs.
- `main.py leaderboard` : compare les stats **par char** (`wot/tanks/stats`) à un
  **snapshot quotidien** (`snapshot.json`, committé automatiquement d'un run à l'autre)
  pour calculer la **WN8 de session** de chaque joueur. La WN8 utilise la table de
  valeurs attendues officielle embarquée dans `wn8exp.json` (source XVM) et le tier
  moyen via `wot/encyclopedia/vehicles`. Pour rester léger, seuls les joueurs ayant
  joué depuis le dernier snapshot sont re-interrogés char par char.
- `main.py announce` : poste une **annonce ponctuelle** (« Mise à jour du calcul des
  stats par SEBonduel ») détaillant le passage au classement WN8. Déclenchable à la
  main via *Actions → Annonce mise à jour stats → Run workflow*.
- Publication via **webhook Discord**. Aucun serveur à héberger.

## Mise en place

1. **Webhook Discord** dans le salon voulu (ex. `#clan-stats`) :
   *Modifier le salon → Intégrations → Webhooks → Nouveau webhook → Copier l'URL*.
2. Pousser ce dossier sur GitHub (repo **public** recommandé = minutes Actions illimitées).
3. Secrets du repo (*Settings → Secrets and variables → Actions*) :

   | Secret | Valeur |
   |--------|--------|
   | `WG_APP_ID` | `00eed50e0468215e87ec936f17c52d8f` |
   | `WG_CLAN_ID` | `500165786` (GR0UT) |
   | `STATS_WEBHOOK_URL` | webhook par défaut (leaderboard) |
   | `INACTIVITY_WEBHOOK_URL` | *(optionnel)* webhook d'un salon séparé pour l'inactivité |
   | `LEADERBOARD_WEBHOOK_URL` | *(optionnel)* webhook dédié au leaderboard |

   Si tu ne définis qu'un `STATS_WEBHOOK_URL`, les deux rapports vont dans le même
   salon. Pour les séparer, ajoute `INACTIVITY_WEBHOOK_URL` (et/ou `LEADERBOARD_WEBHOOK_URL`).

4. Les crons tournent tout seuls. Test manuel : onglet **Actions** → *Run workflow*.

## Réglages

| Variable | Défaut | Rôle |
|----------|--------|------|
| `INACTIVITY_DAYS` | `28` | Seuil d'inactivité (jours) |
| `MIN_BATTLES` | `5` | Minimum de batailles pour figurer au leaderboard |
| `WN8_EXP_FILE` | `wn8exp.json` | Table des valeurs attendues WN8 (XVM) |
| `DRY_RUN` | — | `1` = n'envoie rien, affiche dans la console |

> **`wn8exp.json`** est la table officielle des valeurs attendues (source XVM). Pour
> la mettre à jour de temps en temps :
> `curl -sSL -o wn8exp.json https://static.modxvm.com/wn8-data-exp/json/wn8exp.json`

## Test en local

```bash
pip install -r requirements.txt
export WG_APP_ID=xxxx DRY_RUN=1
python main.py inactivity
python main.py leaderboard    # 1er run = baseline ; le classement arrive au run suivant
```

## Notes

- Le **leaderboard** a besoin de deux snapshots : le tout premier run enregistre la
  base, le classement apparaît dès le run suivant.
- Les joueurs au **profil privé** ne peuvent pas être classés (stats masquées) ; ils
  sont ignorés du leaderboard et signalés à part dans le radar d'inactivité.
- Horaires des crons en UTC (leaderboard 21:00 UTC ≈ 23h Paris été ; inactivité lundi
  08:00 UTC). Ajuste dans `.github/workflows/` au besoin.
