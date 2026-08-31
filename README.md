# GR0UT — Bot Clan Stats

Deux automatisations Discord pour le clan GR0UT (World of Tanks EU), gratuites via
GitHub Actions :

- 📉 **Radar d'inactivité** — chaque lundi, liste les membres sans bataille depuis
  plus de 28 jours (configurable). Une 2ᵉ section, séparée, signale les membres qui
  **jouent mais pas en équipe** : moins de `MIN_BASTION_BATTLES` batailles
  **Bastion (Escarmouches) + Incursions** sur 28 jours (≈ contribution nulle en
  ressources industrielles, qui ne se gagnent que dans ces modes).
- 🏆 **Leaderboard du jour** — chaque soir, le **top 5** des joueurs des dernières
  ~24h, classé par **WN8 de session** (perf réelle par bataille, pas par volume).
  Chaque joueur affiche : WN8, tier moyen, dégâts moyens, spot moyen et % de victoires.
- 🎖️ **Promotion auto** (GR0UT) — chaque jour, passe les **recrues → soldat** après
  `PROMOTE_AFTER_DAYS` jours (défaut 30) et poste la liste des promus sur Discord.
- 🏰 **Top contributeurs Bastion** (`main.py bastion_top`) — classement positif
  hebdo des joueurs qui font le plus d'**Escarmouches + Incursions** (fenêtre
  `BASTION_TOP_DAYS`, défaut 7 j ; top `BASTION_TOP_N`, défaut 10). Réutilise la
  même source que le radar d'inactivité et les mêmes clans (`INACTIVITY_TARGETS`).

## Promotion auto recrue → soldat

`main.py promote` détecte les recrues présentes depuis ≥ 30 jours (`wgn/clans/info`,
champ `joined_at`) et les passe **soldat** via l'endpoint interne du portail clan
(`api/change_role`), qui **exige une session d'officier authentifiée**.

Comme l'API publique WG est en lecture seule pour les grades, on fournit la session
via le secret **`WG_PORTAL_COOKIE`** (l'en-tête `Cookie` complet du portail) :

1. Connecte-toi au portail clan (`eu.wargaming.net/clans/wot/500165786/players/`)
   avec un **compte officier** ayant le droit de gérer les grades.
2. Ouvre les **DevTools → Network**, recharge, clique une requête vers
   `eu.wargaming.net`, section *Request Headers* → copie **toute** la valeur `Cookie`.
3. Colle-la dans le secret GitHub `WG_PORTAL_COOKIE`.

Le cron quotidien fait aussi un **keep-alive** (GET portail) pour garder la session
vivante le plus longtemps possible. Quand elle finit par expirer, le bot **ne casse
pas en silence** : il poste sur Discord un message d'alerte **avec la liste des
recrues à passer soldat à la main**, jusqu'à ce que tu recolles le cookie.

> Réglages : `PROMOTE_CLAN_ID` (défaut GR0UT `500165786`), `PROMOTE_AFTER_DAYS`
> (défaut 30), `PROMOTE_WEBHOOK_URL` (sinon retombe sur le webhook d'inactivité).
> `DRY_RUN=1` liste sans rien changer.

## Comment ça marche

- `main.py inactivity` : récupère les membres (`wgn/clans/info`) + leur
  `last_battle_time` (`account/info`) et signale les inactifs. Pour la 2ᵉ section
  (jeu d'équipe), il lit le **nombre de batailles Bastion + Incursions sur 28 j par
  membre** via l'API interne du portail clan
  (`eu.wargaming.net/clans/wot/<id>/api/players/`, publique mais non documentée ;
  l'API publique WG n'expose pas cette donnée). Si le clan masque ses stats ou si
  l'endpoint est indisponible, la section est simplement omise.
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
| `MIN_BASTION_BATTLES` | `10` | Sous ce nb de batailles Bastion+Incursions sur 28 j → signalé « ne joue pas en équipe » |
| `WG_PORTAL_BASE` | `https://eu.wargaming.net` | Base du portail clan (change de région au besoin) |
| `MIN_BATTLES` | `5` | Minimum de batailles pour figurer au leaderboard |
| `TOP_N` | `5` | Nombre de joueurs affichés au classement |
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
