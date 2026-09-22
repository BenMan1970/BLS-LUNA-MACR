"""Calendar Layer -- Forex Factory High-Impact feed (Data Integrity Layer).

Version 5 -- réparation + consolidation mono-fichier, câblée sur
``config.py`` / ``macro_engine.py`` / ``models.py`` / ``app.py``.

=============================================================================
PATCH-COMPAT-1 — ALIGNEMENT SUR calendar_core.py 2.4.1 (app TA / Desk)
=============================================================================
Sens des corrections : le DESK est meilleur sur ces points, le macro l'adopte.
Chaque hunk est balisé [COMPAT-Cn] (grep). Aucune valeur ni aucun hash
existants ne changent (verrouillé par test_calendar_parity.py) ; SCHEMA_VERSION
et les modèles canoniques sont VOLONTAIREMENT inchangés (pas de conflit avec
tes tests de verrouillage).

[COMPAT-C1] Dérive de vocabulaire d'impact (audit B1 du desk) : pénalité 0.2 ;
            rejet TOTAL (lignes normalisées, 0 événement retenu, warning de
            vocabulaire) → INVALID + ``IMPACT_VOCABULARY_ALL_REJECTED``. Avant :
            « VALID 1.000, 0 événement » (mesuré sur flux renommé High→Critical).
            ⚠ Si ton calendar_layer v6.4 contient déjà ce miroir, SAUTE ce hunk.
[COMPAT-C2] ``SELECTION_EMPTY:impact=N,...`` : nomme la cause d'un entonnoir
            vide (s'ajoute à NO_EVENT_MATCHED_SELECTION_POLICY, ne le remplace pas).
[COMPAT-C3] HTTP 429 : plus de retry ni de sommeil sur Retry-After (mesuré :
            Retry-After=2 → 4 requêtes / 6 s bloqués ; 247 s → ~12 min de thread
            gelé) ; statut de flux ``rate_limited`` (vocabulaire du desk).
[COMPAT-C4] Miroir de repli du flux primaire (comme le desk), jamais après un
            429 (un quota n'est pas une panne). Désactivable :
            BLUESTAR_SOURCE_URL_MIRROR="".
[COMPAT-C5] ``feed_sha256`` par flux exposé dans les métadonnées legacy.
[COMPAT-C6] Bloc « calendar-contract-1.0 » (clés de métadonnées communes,
            ``high_content_hash`` comparable inter-politiques, ``events_upcoming``,
            ``rebase_metadata`` pour re-dater les horizons côté consommateur).

=============================================================================
v6.2 (15/09/2026, 14:05) — gate « production-ready » : suite de tests
entièrement verte + dernière correction de véracité de donnée restante.
=============================================================================

[B8] SESSIONS ANZ : WELLINGTON (05:00-14:00 NZST/NZDT) et SYDNEY
     (07:00-15:00 AEST/AEDT) rejoignent la table des places. Mesure
     d'origine : « GDP q/q » NZD mercredi 22:45 UTC = 10:45 Wellington /
     08:45 Sydney, cœur de séance étiqueté « OFF » par la table à trois
     centres. Le bloc asiatique de ``classify_session`` (enum ``Session``
     INCHANGÉE) absorbe les deux nouveaux centres. Contrat d'impact :
     zéro. La session n'est projetée NI dans le content_hash, NI dans le
     rapport (la « Session idéale » des cartes est une chaîne statique de
     macro_engine l.1893) ; seuls l'expander calendrier et les futurs
     consommateurs ``session``/``active_market_centers`` voient la donnée
     devenir vraie. ``get_session()`` (API de compat v4, UTC-only,
     appelée par aucun chemin de données) reste volontairement intacte.

[B2] STATUT OFFICIEL — NON IMPLÉMENTABLE EN « OPTION A » SANS TOUCHER UNE
     ZONE INTERDITE, documenté après lecture de la plomberie macro :
     ``build_catalysts`` découpe les catalyseurs par PRIORITÉ TEMPORELLE
     (CRITICAL/HIGH ≤48h ; MEDIUM 48-168h) et non par impact — le bucket
     « medium » du rapport désigne donc des High@>48h, et tout Medium FF
     ingéré arriverait en ``events_engine``, la liste unique qui alimente
     AUSSI scoring, blackout et regime (macro_engine l.2121-2150, 2177).
     Un « Medium affichage seul » exigerait soit une nouvelle liste + un
     nouveau gabarit renderer (zone gelée), soit des gardes d'impact dans
     macro_engine (zone verrouillée P0-3 + divergence avec bucket() v10 qui
     ne blackoute QUE les High), soit la bascule coordonnée du hash partagé
     avec l'app TA. Chiffré, arbitré, REPOUSSSÉ — pas oublié : 10 Medium
     cette semaine, dont Retail Sales USD jeudi (= 62 % de volume
     informationnel en plus des 16 High), contre 3 chemin de code à rouvrir.
     La recommandation au desk reste l'option « Medium en annexe dédiée »,
     qui nécessite un ticket renderer.

=============================================================================
v6.1 (15/09/2026) — CORRECTIFS POST-AUDIT EXTERNE (« audit Opus »), CHAQUE
POINT MESURÉ AVANT ÊTRE ADMIS. Zéro régression décisionnelle : see tests
``tests/test_calendar_locks_v61.py``.
=============================================================================

[M1] RÉPLIQUE v10 RESTAURÉE (le seul écart prouvé avec l'autorité ENGINE.V10.py)
     * ``_TIER_S`` : ajout de ``"refinancing"`` — présent dans v10 (l.251),
       absent ici. Sans lui, « Main Refinancing Rate » (décision BCE) = tier S
       côté Desk mais NONE côté Macro : divergence possible de blackout sur
       le jour le plus risqué de la zone euro. Mesuré 15/09 :
       classify_tier("Main Refinancing Rate") → NONE avant, S après.
     * Promotion NONE→A des événements HIGH : v10.CalendarEvent._derive
       (l.335-336) promeut tout événement HIGH au nom non classé en tier A.
       Ici seule la LABELLING change (fenêtre défaut (2,24) == fenêtre A) ;
       le verdict booléen de blackout est bit-for-bit identique. Effet
       visible : « tier NONE » → « tier A » dans les motifs d'évitement
       (ex. Claimant Count Change GBP), conforme à ce que le Desk énonce.

[M2] BANDEAU DE TRONCATURE : [F1] RECONNAÎT UNE PRÉMISSE FAUSSE (audit B1)
     La fusion thisweek+nextweek supposait l'existence d'un flux « nextweek ».
     Mesuré le 15/09/2026 (5 probes espacés de ~12 h + famille complète) :
     ff_calendar_nextweek/lastweek/thismonth/nextmonth .json et nextweek.xml
     → TOUS 404 ; seul thisweek existe (.json/.xml/.csv). Conséquence : le
     bandeau « tronqué à Xh » se levait 7 jours sur 7 (horizon max mesuré au
     dimanche : 130,2 h < 168 h) — exactement le bruit permanent que [F1]
     prétendait supprimer — plus une requête HTTP morte par cycle.
     → v6.1 : la source publie une semaine glissante dim→sam ; c'est sa
       nature, pas une anomalie. ``_horizon_diagnosis`` distingue
       « nominal_weekly » (thisweek ok, horizon court) de « degraded »
       (thisweek en erreur alors qu'un second flux répond) et « unreachable ».
       Le drapeau rouge ``feed_horizon_truncated`` ne se lève plus QUE sur
       « degraded ». L'horizon réel reste exposé (``feed_horizon_h`` +
       ``feed_horizon_state``) et la fusion multi-flux reste opérationnelle :
       réactivable par ``BLUESTAR_SOURCE_URL_NEXT`` si l'éditeur publie un
       jour un second flux. Aucun code de fusion supprimé.

[M3] STATUT DE PARCOURS EXPORTÉ (audit B5) : les lignes legacy portent
     désormais ``forecast_status`` / ``previous_status`` (le parse_status
     calculé puis jeté depuis la v3). ``_NUM_RE`` accepte ``≤ ≥ ±`` (les
     préfixes que macro_engine._parse_feed_rate sait déjà lire) : les deux
     vocabulaires convergent sur ces cas. Le contenu économique du flux FF
     actuel ne change pas de hash (aucune de ces chaînes dans le feed —
     verrouillé par test). Le format « 3,50–3,75% » (plage) reste
     UNPARSEABLE ici et n'est traité que par macro_engine : écart résiduel
     DOCUMENTÉ, non corrigé (déviner une médiane n'est pas normaliser).

[M4] GROUPEMENT PRESSE BoJ (audit B7) : fenêtre d'anciadité conférence→
     décision portée à 240 min pour JPY (mesuré : décision 02:30 UTC,
     conférence 05:30 UTC = écart 3,0 h > 120 min) ; 120 min partout
     ailleurs (FOMC 14:00→14:30 verrouillé). ``release_group_id`` entre dans
     le content_hash [F5] : c'est le seul champ dont ce correctif fait bouger
     le hash — sans consommateur décisionnel ni affichage dans le rapport.

[M5] HYGIÈNE (audits B9 + mineurs) :
     * ``view_hash`` ajouté en métadonnées (alias explicite de content_hash,
       qui est un hash de VUE fenêtrée, pas de contenu : mesuré, il change à
       T+52 h sur un flux octet-pour-octet identique quand un événement
       franchit la borne 72 h. La stabilité inter-apps reste
       ``source.payload_sha256``.)
     * ``to_legacy_payload`` ne retourne plus ``events`` et ``events_engine``
       comme le MÊME objet list.
     * docstring ``institutional.fetch_macro_surprise`` : « actual present in
       the production calendar layer » → FAUX mesuré (0/104 lignes JSON avec
       clé actual, XML sans <actual>) ; corrigé sur place.

NON CORRIGÉS VOLONTAIREMENT (documentés, décisions hors périmètre zero-régression) :
     * B2 (filtrage HIGH à l'ingestion, 16/104 retenu) : changement de
       contrat desk, pas un bug ; le policy et [S2] le documentent déjà.
     * B8 (sessions LON/NY/TKY seulement → NZD GDP 22:45Z « OFF ») : aucun
       consommateur dans le briefing (la « Session idéale » des cartes est
       une chaîne statique macro_engine l.1893) ; addition de Wellington/
       Sydney = enrichissement à arbitrer.
     * Commentaire erroné de renderer.py l.243 (« Le flux FF n'a pas de
       Medium » — mesuré : 10 Medium dans le flux) : renderer = zone
       intouchable de la mission ; signalé à son owner.

=============================================================================
CE QUE LA v5 CORRIGE PAR RAPPORT À LA v4 (chaque point vérifié sur le
briefing HTML du 08/09/2026)
=============================================================================

[F1] FLUX TRONQUÉ EN PERMANENCE (cause du bandeau rouge « tronqué à 93.4h »)
     ``FF_JSON_URL`` = ``ff_calendar_thisweek.json`` est un flux HEBDOMADAIRE
     (dimanche → samedi). Comparer sa fin à ``FF_WATCH_HORIZON_H = 168h``
     produisait ``feed_horizon_truncated = True`` TOUS LES JOURS sauf le
     dimanche (mardi 93h, mercredi 70h, jeudi 46h...). L'avertissement
     « un silence calendaire n'est PAS une absence de risque » était donc
     structurellement toujours affiché : bruit permanent, zéro information.
     → v5 : fusion ``thisweek`` + ``nextweek`` (deux GET séquentiels sur la
       même session, dédoublonnage par (country, title, date_utc)). Horizon
       roulant de 7 à 14 jours, donc >= 168h en permanence. Le drapeau ne se
       lève plus que si le second flux est réellement indisponible -- et il
       redevient alors un vrai signal.
       ⚠ RECTIFIÉ v6.1 (mesure 15/09/2026) : ``ff_calendar_nextweek`` n'a
       JAMAIS été publié par l'éditeur (famille complète → HTTP 404). Le
       bandeau restait donc permanent (7j/7) et chaque cycle émettait un GET
       mort. Voir [M2] en tête de fichier : le drapeau ne se lève plus que
       sur un flux réellement dégradé ; la fusion multi-flux reste
       opérationnelle via ``BLUESTAR_SOURCE_URL_NEXT``.

[F2] INCOHÉRENCE DE FUSEAU DANS LE HTML (1 heure d'écart)
     ``DEFAULT_DISPLAY_TZ = "Africa/Casablanca"`` (UTC+1) alors que toute
     l'app macro affiche ``config.TZ_CET`` = Europe/Paris (UTC+2 en été).
     Le HTML mélangeait donc « 12:48 CEST » (sous-barre, macro_engine) et
     « 13:15 (UTC+1) » (section 2, calendar_layer) : la BCE était en réalité
     à 14:15 CEST. Un lecteur qui cale une session sur l'heure affichée se
     trompait d'une heure.
     → v5 : ``DISPLAY_TIMEZONE`` est LU depuis ``config.TZ_CET.key``
       (override par ``BLUESTAR_DISPLAY_TZ``). Plus aucun fuseau codé en
       dur. Les décisions (priority / is_blackout / hours_until) restent en
       UTC pur : inchangées.

[F3] ``actual`` À CHAÎNE VIDE AU LIEU DE "—"
     Le ``or ""`` de la v4 cassait DEUX consommateurs :
       * ``app.py`` : ``if e['actual'] != '—'`` → affichait « / actual »
         (vide) sur chaque ligne de l'expander calendrier ;
       * ``models.MacroEvent.from_enriched`` : ``d.get("actual","—")`` ne
         retombe sur le tiret QUE si la clé est absente, jamais si elle vaut
         "" -- le contrat "—" de models.py n'était donc jamais honoré.
     → v5 : placeholder unique ``ABSENT_DISPLAY = "—"`` pour
       forecast/previous/actual, aligné sur models.py. ``*_value`` reste
       ``None`` pour l'exploitation numérique.

[F4] FRAÎCHEUR DE SOURCE NON MESURABLE
     ``SourceInfo.fetched_at_utc = now_utc`` → ``source_age_seconds`` = 0 et
     ``is_stale`` = False par construction. Le seuil
     ``max_source_age_seconds`` ne pouvait rien détecter, et le score de
     qualité était structurellement plafonné à 1.0.
     → v5 : horodatage RÉEL du fetch, propagé via la méta.

[F5] ``content_hash`` INSTABLE (l'objectif même du fichier)
     Le hash v4 incluait ``scheduled_at_display``, ``date_display``,
     ``day_of_week``, ``display_timezone`` et ``source_index`` : changer le
     fuseau d'affichage OU l'ordre de fusion des flux changeait le hash pour
     un contenu économique identique. « Les deux apps voient le même
     calendrier » était donc invérifiable.
     → v5 : hash calculé sur une PROJECTION ÉCONOMIQUE explicite
       (occurrence_id, event_type_id, UTC, devise, nom, impact,
       forecast/previous/actual bruts, release_group_id).
       ``CONTENT_HASH_METHOD`` documente la méthode. Divergence assumée et
       tracée vs calendar_core.py -- à y répercuter (voir DETTE ci-dessous).

[F6] PARSEUR NUMÉRIQUE : SÉPARATEUR DE MILLIERS
     ``"1,234"`` (mille deux cent trente-quatre) était lu ``1.234``, et
     ``"1.234"`` symétriquement. Sur un NFP ou des ventes au détail publiés
     sans suffixe K/M, l'erreur est d'un facteur 1000.
     → v5 : ``_to_float`` distingue milliers et décimales (dernier
       séparateur = décimal si les deux sont présents ; groupes de 3 exacts
       = milliers). Nouveau statut ``APPROXIMATE`` pour ``<``/``>``/``~``.

[F7] ROBUSTESSE / DIVERS
     * ``_LEGACY_SESSION[e.session]`` en accès direct → KeyError si un
       membre est ajouté à ``Session``. Passé en ``.get(...)``.
     * ``day_of_week`` via ``strftime("%A")`` → dépend de la locale du
       conteneur (« JEUDI » vs « THURSDAY »). Table fixe désormais.
     * ``payload_sha256`` calculé sur ``body.decode(errors="replace")`` →
       hash d'un texte dégradé, pas de la charge réelle. Hash sur octets.
     * ``Retry(backoff_jitter=...)`` lève ``TypeError`` sur urllib3 < 2.0
       (donc fetch mort et calendrier vide). Repli automatique.
     * ``build_payload`` levait ``ValueError`` sur un flux hors-norme
       (racine non-liste, > max_events) → exception non rattrapée dans
       ``build_calendar`` → app Streamlit morte. Désormais rattrapé et
       dégradé proprement (contrat v3 : jamais d'exception).
     * ``FetchError`` : code mort, conservé en alias de compatibilité.
     * ``window_past_hours`` était dupliqué (72.0 codé en dur) : dérivé de
       ``config.RESIDUAL_RISK_WINDOW_H`` (source unique, cohérent avec le
       ``since_h=72`` de ``macro_engine._events_for_ccys``).
     * ``window_future_hours`` passé de 192h à ``FF_WATCH_HORIZON_H`` (168h)
       pour que la fenêtre annoncée et la fenêtre servie soient la MÊME.

=============================================================================
CE QUI RESTE VOLONTAIREMENT INCHANGÉ (zéro régression décisionnelle)
=============================================================================
Les seuils ``imminent_hours = 6`` / ``soon_hours = 48`` et donc la
projection ``priority`` (PAST / CRITICAL / HIGH / MEDIUM) sont IDENTIQUES à
la v4. Tentation écartée : élargir HIGH à 168h pour peupler
``catalysts_high`` aurait alimenté ``macro_engine._compute_asset_score``
(``catalyst_pen = 0.15 * len(HIGH)``, plafonné à 0.30) et pouvait faire
tomber les setups sous ``MODE_SELECTION_MIN_SCORE`` -- soit un rapport vide.
Le tag « 🟡 ÉLEVÉ · >48h » du renderer est la bonne réponse à ce point ; il
n'appartient pas à cette couche.

``TIER_WINDOWS`` / ``classify_tier`` / ``is_blackout`` restent une réplique
EXACTE de ``v10.py`` (Desk Engine), qui fait autorité. Sujet ORTHOGONAL à la
normalisation. Toute modification dans v10.py DOIT être répercutée ici.

=============================================================================
CE QUE LA v6 EMPRUNTE À calendar_core.py / calendar_ingestor.py (app TA) --
SYNTHÈSE LÉGÈRE, ZÉRO RUPTURE DE CONTRAT
=============================================================================
Objectif : que le module macro expose les MÊMES informations de diagnostic
que l'app TA sur ce que les deux apps ont déjà en commun (même
``occurrence_id``, même flux Fair Economy), sans importer l'architecture de
producteur autonome de ``calendar_ingestor.py`` (cron/systemd, écriture
atomique, verrou inter-processus, historique) : ``calendar_layer`` reste
SANS état persistant, appelé en synchrone par l'app macro -- ce choix n'est
pas remis en cause (cf. en-tête SECTION 3).

[S1] STATUT PAR FLUX (``ok`` / ``absent_404`` / ``error:CODE``)
     Avant la v6, un ``nextweek`` non encore publié par Fair Economy (cas
     NORMAL en milieu de semaine, cf. [F1]) était compté comme un flux en
     échec au même titre qu'une vraie panne réseau : ``feeds_ok < feeds_total``
     déclenchait ``PARTIAL_FEED_COVERAGE`` et une pénalité de score de 0.15,
     pour une situation qui ne signifie STRICTEMENT RIEN sur la qualité des
     données retenues. ``calendar_ingestor.py`` distingue déjà les deux cas
     (404 = normal, log info ; erreur = anormal, log warning ; aucun des
     deux n'affecte le flux primaire).
     → v6 : ``SourceInfo.feed_status`` (``{"thisweek": "ok", "nextweek":
       "absent_404"}``) porte ce diagnostic. Seul un échec du flux PRIMAIRE
       (``thisweek``) déclenche avertissement + pénalité ; un flux
       secondaire absent ou en erreur est journalisé (``SECONDARY_FEED_
       ERROR``) mais n'entame plus le score -- exactement le traitement que
       l'app TA applique déjà à son flux bonus. Repli explicite sur l'ancien
       calcul (``feeds_ok``/``feeds_total``) si ``feed_status`` est vide
       (compat totale avec tout appelant direct de ``build_payload`` qui
       construirait encore son propre ``SourceInfo``).

[S2] COUVERTURE PAR DEVISE : POLICY vs SOURCE RÉELLE (``CoverageInfo``)
     ``calendar_layer`` ne distinguait pas « AUD absent parce que la policy
     ne retient que HIGH et la source n'a que du MEDIUM pour l'AUD cette
     semaine » de « AUD absent parce que la source n'a RIEN sur l'AUD ». Le
     premier cas est un artefact de configuration ; le second est une
     information de marché neutre. Confondre les deux, c'est exactement le
     type de bruit que [F1] corrige déjà pour l'horizon global -- même
     défaut, à la maille devise.
     → v6 : ``CoverageInfo`` (``currencies_scope`` / ``currencies_covered``
       / ``currencies_excluded_by_policy`` / ``currencies_no_data_in_
       source``), calculée en comparant les lignes normalisées AVANT et
       APRÈS filtre de policy sur la fenêtre ``[lo, hi]``. Purement
       diagnostique : ne change NI la sélection, NI ``priority``, NI
       ``is_blackout``. Exposée dans ``metadata`` sous les mêmes noms de
       clé que ``calendar_core.to_legacy_payload`` pour rester lisible par
       quiconque connaît déjà l'app TA.

CE QUI N'EST PAS PORTÉ (et pourquoi)
     Circuit breaker, last-known-good sur disque avec plafond d'âge dur,
     ``health.json``, verrou inter-processus, rotation/historique : tout
     cela appartient au PRODUCTEUR autonome (``calendar_ingestor.py``), pas
     à la couche de normalisation appelée en direct par l'app macro. Le
     porter ici transformerait un module sans effet de bord en composant
     avec état disque et sémantique de concurrence -- un changement
     d'architecture, pas une synthèse légère, et un risque de régression
     (FS en lecture seule, exécutions concurrentes) pour un bénéfice qui ne
     s'exprime que si l'app macro tourne, elle aussi, en producteur détaché.
     Si ce besoin se confirme, il mérite sa propre revue, pas un ajout
     silencieux ici.

=============================================================================
DETTE DOCUMENTÉE
=============================================================================
[F2] et [F5] sont des divergences DÉLIBÉRÉES vs ``calendar_core.py`` (app
TA). Elles ne changent aucun instant UTC, aucun ``occurrence_id``, aucune
valeur économique -- seulement l'affichage et la méthode de hachage. Les
deux apps restent réconciliables événement par événement sur
``occurrence_id`` (fonction de ``event_type_id`` + UTC uniquement, donc
inchangé). À porter dans calendar_core.py lors de l'extraction du module
commun ``bluestar_shared.calendar_rules``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Source unique de vérité pour les constantes partagées avec le reste de
# l'app (URL du flux, TTL, fenêtre de risque résiduel, fuseau d'affichage).
from . import config as _C

logger = logging.getLogger(__name__)

# =============================================================================
# SECTION 0bis -- SOCLE COMMUN (ex-calendar_compat.py), FUSIONNÉ ICI [C11..C15]
# =============================================================================
# Décision d'architecture : ce bloc vivait dans un fichier séparé
# (calendar_compat.py) importé aussi par calendar_core.py (desk), pour que
# le fuseau adaptatif, le hash de parité et la vue committee soient calculés
# par EXACTEMENT le même code des deux côtés. Choix retenu : mono-fichier
# (fidèle à la philosophie "réparation + consolidation mono-fichier" de
# l'en-tête). Conséquence assumée : quand calendar_core.py recevra son
# propre round de patchs, ce bloc devra y être RECOPIÉ à l'identique — même
# dette de duplication, déjà assumée pour TIER_WINDOWS/classify_tier (voir
# SECTION 2) et déjà responsable d'un bug réel ([M1] : "refinancing" absent
# d'un côté). Toute modification ici DOIT être répercutée dans calendar_core.py.
#
# Positionné ICI (avant toute création de ZoneInfo) parce que l'épinglage de
# la base tzdata doit s'exécuter avant TZ_LONDON/TZ_NEW_YORK/TZ_TOKYO plus
# bas : sinon ces trois zones utiliseraient la base système, les suivantes la
# base pip -- une incohérence interne au même fichier.
#
# Kill-switches (production) :
#     BLUESTAR_TZ_AUTO=0    -> désactive la détection système (retour au
#                              comportement historique : env puis fallback).
#     BLUESTAR_TZ_PIN=0     -> désactive l'épinglage sur le tzdata pip.
#     BLUESTAR_DISPLAY_TZ   -> force un fuseau (IANA ou raccourci pays).
# =============================================================================
import hashlib as _hashlib
import zoneinfo as _zoneinfo
from pathlib import Path as _Path

COMPAT_BLOCK_VERSION = "calendar-compat-inline-1.0.0"


def _pin_tzdata(force: Optional[bool] = None) -> Dict[str, Any]:
    """Force zoneinfo à utiliser le paquet pip ``tzdata`` plutôt que la base
    du système d'exploitation. Sûr par construction : restaure le TZPATH
    système si le paquet est absent ou incomplet. Jamais d'exception propagée."""
    if force is None:
        force = os.getenv("BLUESTAR_TZ_PIN", "1") != "0"
    diag: Dict[str, Any] = {"pinned": False,
                            "reason": "disabled" if not force else "not_attempted",
                            "tzdata_version": None, "tzpath": list(_zoneinfo.TZPATH)}
    if not force:
        return diag
    previous = list(_zoneinfo.TZPATH)
    try:
        import tzdata  # noqa: F401
        diag["tzdata_version"] = (getattr(tzdata, "IANA_VERSION", None)
                                  or getattr(tzdata, "__version__", None))
        _zoneinfo.reset_tzpath(to=[])
        ZoneInfo("Africa/Tunis")      # canari : zone récente, peu courante
        ZoneInfo("America/Toronto")
        ZoneInfo("Europe/Paris")
        diag.update(pinned=True, reason="ok", tzpath=[])
    except Exception as exc:                                  # noqa: BLE001
        _zoneinfo.reset_tzpath(to=previous)
        diag.update(pinned=False, reason=f"unavailable:{type(exc).__name__}",
                    tzpath=previous)
        logger.info("tzdata pip indisponible (%s) — base système conservée", exc)
    return diag


TZDATA_PIN: Dict[str, Any] = _pin_tzdata()


def tz_environment() -> Dict[str, Any]:
    """Empreinte de l'environnement fuseau, exposée dans les métadonnées."""
    return {
        "compat_module_version": COMPAT_BLOCK_VERSION,
        "tzdata_pinned": TZDATA_PIN.get("pinned"),
        "tzdata_source": TZDATA_PIN.get("reason"),
        "tzdata_version": TZDATA_PIN.get("tzdata_version"),
        "tzpath": TZDATA_PIN.get("tzpath"),
    }


# Raccourcis opérateur : "TN", "FR", "CA-QC"... — pas une clé IANA.
TZ_SHORTCUTS: Dict[str, str] = {
    "TN": "Africa/Tunis", "TUNISIA": "Africa/Tunis", "TUNISIE": "Africa/Tunis",
    "FR": "Europe/Paris", "FRANCE": "Europe/Paris", "PARIS": "Europe/Paris",
    "MA": "Africa/Casablanca", "MAROC": "Africa/Casablanca",
    "CA": "America/Toronto", "CA-ON": "America/Toronto", "CA-QC": "America/Toronto",
    "CA-AB": "America/Edmonton", "CA-BC": "America/Vancouver",
    "CA-MB": "America/Winnipeg", "CA-NS": "America/Halifax",
    "CANADA": "America/Toronto", "MONTREAL": "America/Toronto",
    "UK": "Europe/London", "GB": "Europe/London", "LONDON": "Europe/London",
    "US": "America/New_York", "US-ET": "America/New_York",
    "US-CT": "America/Chicago", "US-PT": "America/Los_Angeles", "NY": "America/New_York",
    "CH": "Europe/Zurich", "BE": "Europe/Brussels", "DE": "Europe/Berlin",
    "AE": "Asia/Dubai", "SG": "Asia/Singapore", "JP": "Asia/Tokyo",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland",
    "UTC": "UTC", "Z": "UTC",
}


def _valid_tz_token(key: Optional[str]) -> Optional[str]:
    if not key:
        return None
    try:
        ZoneInfo(key)
        return key
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        return None


def normalize_tz_token(token: Optional[str]) -> Optional[str]:
    """Accepte une clé IANA ("Africa/Tunis") ou un raccourci ("TN", "ca-qc")."""
    if not token:
        return None
    raw = str(token).strip()
    direct = _valid_tz_token(raw)
    if direct:
        return direct
    return _valid_tz_token(TZ_SHORTCUTS.get(raw.upper().replace("_", "-")))


def _system_iana_key() -> Optional[str]:
    """Fuseau IANA de la machine hôte : TZ, puis /etc/timezone, puis la
    cible du lien /etc/localtime. None si rien n'est concluant."""
    env_tz = normalize_tz_token(os.getenv("TZ"))
    if env_tz:
        return env_tz
    try:
        p = _Path("/etc/timezone")
        if p.is_file():
            key = _valid_tz_token(p.read_text(encoding="utf-8").strip())
            if key:
                return key
    except OSError:
        pass
    try:
        link = _Path("/etc/localtime")
        if link.exists():
            target = str(link.resolve())
            if "zoneinfo" in target:
                candidate = target.split("zoneinfo", 1)[1].lstrip("/")
                for prefix in ("posix/", "right/"):
                    if candidate.startswith(prefix):
                        candidate = candidate[len(prefix):]
                key = _valid_tz_token(candidate)
                if key:
                    return key
    except OSError:
        pass
    return None


def resolve_display_tz(explicit: Optional[str] = None,
                       fallback: str = "Europe/Paris") -> Tuple[str, str]:
    """Fuseau d'AFFICHAGE. Ordre : explicit > BLUESTAR_DISPLAY_TZ > système
    (désactivable, BLUESTAR_TZ_AUTO=0) > fallback > "UTC". Retourne
    (clé_iana, origine) — l'origine est exportée, une heure affichée doit
    toujours pouvoir être expliquée. Rien de décisionnel n'en dépend :
    priority / hours_until / is_blackout restent en UTC pur."""
    key = normalize_tz_token(explicit)
    if key:
        return key, "explicit"
    key = normalize_tz_token(os.getenv("BLUESTAR_DISPLAY_TZ"))
    if key:
        return key, "env"
    if os.getenv("BLUESTAR_TZ_AUTO", "1") != "0":
        key = _system_iana_key()
        if key:
            return key, "system"
    key = normalize_tz_token(fallback)
    if key:
        return key, "fallback"
    logger.error("Aucun fuseau d'affichage résoluble — repli UTC")
    return "UTC", "utc_last_resort"


def _parse_utc_loose(value: Any) -> Optional[datetime]:
    """Comme parse_source_datetime, mais ne lève jamais (retourne None)."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def localize_rows(rows: Sequence[Dict[str, Any]], tz_key: str,
                  origin: str = "unknown") -> int:
    """Réécrit EN PLACE les seuls champs d'affichage des lignes legacy
    (date_display/time_display/datetime_display/display_timezone/
    day_of_week), ajoute datetime_local_iso/display_tz_origin/
    utc_offset_label. Ne touche à AUCUN champ décisionnel."""
    try:
        tz = ZoneInfo(tz_key)
    except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
        logger.error("localize_rows: fuseau '%s' inconnu — lignes inchangées", tz_key)
        return 0
    done = 0
    for row in rows:
        when = _parse_utc_loose(row.get("datetime_utc"))
        if when is None:
            continue
        local = when.astimezone(tz)
        label = _utc_offset_label(local)
        row["date_display"] = local.strftime("%Y-%m-%d")
        row["time_display"] = f"{local.strftime('%H:%M')} ({label})"
        row["datetime_display"] = f"{local.strftime('%Y-%m-%d')} · {local.strftime('%H:%M')} ({label})"
        row["display_timezone"] = tz_key
        row["day_of_week"] = _DAY_NAMES[local.weekday()]
        row["datetime_local_iso"] = local.isoformat()
        row["display_tz_origin"] = origin
        row["utc_offset_label"] = label
        done += 1
    return done


def rebuild_summary_by_day(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
    """summary_by_day est indexé par date d'AFFICHAGE : à rappeler après
    localize_rows, sinon sommaire et lignes se contredisent d'une journée."""
    out: Dict[str, List[str]] = {}
    for row in rows:
        out.setdefault(str(row.get("date_display", "")), []).append(
            f"{row.get('currency', '?')} – {row.get('event_name', '?')}")
    return {k: out[k] for k in sorted(out)}


# --- Hash de parité (périmètre de CONTRAT, avant policy) --------------------
PARITY_HASH_METHOD = "economic_projection_v2:contract_scope_no_group"
CONTRACT_IMPACT_SCOPE: Tuple[str, ...] = ("HIGH", "MEDIUM")
PARITY_WINDOW_PAST_H = 72.0
PARITY_WINDOW_FUTURE_H = 168.0


def parity_window(now: datetime) -> Tuple[datetime, datetime, datetime]:
    """Fenêtre ANCRÉE À L'HEURE RONDE : deux exécutions dans la même heure
    UTC comparent exactement le même périmètre, quel que soit l'écart de
    quelques minutes entre le cycle macro et le cycle desk."""
    anchor = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return (anchor, anchor - timedelta(hours=PARITY_WINDOW_PAST_H),
            anchor + timedelta(hours=PARITY_WINDOW_FUTURE_H))


def _impact_str(value: Any) -> str:
    return str(getattr(value, "value", value) or "").upper()


def _raw_of(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "raw", value)
    return raw if raw is None else str(raw)


def parity_projection(rows: Iterable[Dict[str, Any]], lo: datetime,
                      hi: datetime) -> List[Dict[str, Any]]:
    """Projection du périmètre de CONTRAT (HIGH+MEDIUM, avant policy), à
    partir des rows normalisées. Exclut release_group_id (dépend de la
    sélection), l'affichage et le volatil (hours_until, priority)."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        impact = _impact_str(r.get("impact"))
        if impact not in CONTRACT_IMPACT_SCOPE:
            continue
        when = _parse_utc_loose(r.get("scheduled_at_utc") or r.get("datetime_utc"))
        if when is None or not (lo <= when <= hi):
            continue
        out.append({
            "occurrence_id": r.get("occurrence_id"), "event_type_id": r.get("event_type_id"),
            "scheduled_at_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": r.get("currency"), "name": r.get("name") or r.get("event_name"),
            "impact": impact, "forecast": _raw_of(r.get("forecast")),
            "previous": _raw_of(r.get("previous")), "actual": _raw_of(r.get("actual")),
        })
    out.sort(key=lambda d: (d["scheduled_at_utc"], d["currency"] or "",
                            d["name"] or "", d["occurrence_id"] or ""))
    return out


def parity_hash(rows: Iterable[Dict[str, Any]], lo: datetime, hi: datetime) -> str:
    projection = parity_projection(rows, lo, hi)
    canonical = json.dumps({"method": PARITY_HASH_METHOD, "scope": list(CONTRACT_IMPACT_SCOPE),
                            "events": projection},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + _hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Sortie structurée pour le committee -------------------------------------
COMMITTEE_SCHEMA = "bluestar-committee-calendar-1.0"
_COMMITTEE_EVENT_KEYS = (
    "occurrence_id", "event_type_id", "release_group_id", "release_group_type",
    "currency", "event_name", "datetime_utc", "impact", "priority",
    "tier", "is_blackout", "hours_until", "is_upcoming", "time_proximity",
    "status", "session_v2", "pairs_affected",
    "forecast_value", "forecast_status", "previous_value", "previous_status",
    "actual_value", "actual_status",
)


def to_committee_payload(legacy: Dict[str, Any], now: datetime, *,
                         module: str) -> Dict[str, Any]:
    """Vue machine du calendrier pour l'app committee : pas une seule chaîne
    d'affichage, pas un seul fuseau local — que de l'UTC et du brut."""
    meta: Dict[str, Any] = dict(legacy.get("metadata") or {})
    rows: List[Dict[str, Any]] = list(legacy.get("events_engine") or legacy.get("events") or [])
    events: List[Dict[str, Any]] = [
        {k: r.get(k) for k in _COMMITTEE_EVENT_KEYS if k in r} for r in rows]
    events.sort(key=lambda d: (str(d.get("datetime_utc") or ""), str(d.get("currency") or ""),
                               str(d.get("occurrence_id") or "")))
    blackout = [e for e in events if e.get("is_blackout")]
    return {
        "committee_schema": COMMITTEE_SCHEMA, "module": module,
        "computed_at_utc": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "identity": {
            "parity_hash": meta.get("parity_hash"), "parity_hash_method": meta.get("parity_hash_method"),
            "parity_window_anchor_utc": meta.get("parity_window_anchor_utc"),
            "high_content_hash": meta.get("high_content_hash"), "view_content_hash": meta.get("content_hash"),
            "source_payload_sha256": meta.get("source_payload_sha256") or meta.get("payload_sha256"),
            "feed_sha256": meta.get("feed_sha256") or {},
            "contract_version": meta.get("contract_version"), "schema_version": meta.get("schema_version"),
        },
        "scope": {
            "impact_levels_included": meta.get("impact_levels_included") or [],
            "contract_impact_scope": list(CONTRACT_IMPACT_SCOPE),
            "currencies_filter": meta.get("currencies_filter"),
            "currencies_covered": meta.get("currencies_covered") or [],
            "currencies_excluded_by_policy": meta.get("currencies_excluded_by_policy") or [],
            "currencies_no_data_in_source": meta.get("currencies_no_data_in_source") or [],
            "window_past_hours": meta.get("window_past_hours"),
            "window_future_hours": meta.get("window_future_hours"),
            "priority_thresholds": meta.get("priority_thresholds"),
        },
        "quality": {
            "status": meta.get("quality_status"), "data_quality_score": meta.get("data_quality_score"),
            "reachable": meta.get("reachable"), "is_stale": meta.get("is_stale"),
            "source_age_seconds": meta.get("source_age_seconds"), "serving_mode": meta.get("serving_mode"),
            "feed_horizon_state": meta.get("feed_horizon_state"), "feed_horizon_h": meta.get("feed_horizon_h"),
            "week_rollover_pending": meta.get("week_rollover_pending"), "warnings": meta.get("warnings") or [],
        },
        "timezone_environment": {**tz_environment(), "display_timezone": meta.get("display_timezone"),
                                 "display_tz_origin": meta.get("display_tz_origin")},
        "counters": {
            "events_total": len(events),
            "high_impact_count": sum(1 for e in events if _impact_str(e.get("impact")) == "HIGH"),
            "critical_count": meta.get("critical_count"), "blackout_count": len(blackout),
            "blackout_currencies": sorted({e["currency"] for e in blackout if e.get("currency")}),
        },
        "events": events,
    }


def reconcile(macro: Dict[str, Any], desk: Dict[str, Any]) -> Dict[str, Any]:
    """Compare deux sorties de to_committee_payload. ``aligned=True`` exige
    l'égalité du parity_hash ET de l'ancre de fenêtre."""
    mi, di = macro.get("identity", {}), desk.get("identity", {})
    m_ids = {e.get("occurrence_id") for e in macro.get("events", [])}
    d_ids = {e.get("occurrence_id") for e in desk.get("events", [])}
    same_anchor = mi.get("parity_window_anchor_utc") == di.get("parity_window_anchor_utc")
    same_hash = mi.get("parity_hash") is not None and mi.get("parity_hash") == di.get("parity_hash")
    report: Dict[str, Any] = {
        "aligned": bool(same_hash and same_anchor), "parity_hash_match": same_hash,
        "anchor_match": same_anchor, "macro_parity_hash": mi.get("parity_hash"),
        "desk_parity_hash": di.get("parity_hash"), "macro_anchor_utc": mi.get("parity_window_anchor_utc"),
        "desk_anchor_utc": di.get("parity_window_anchor_utc"),
        "source_payload_match": mi.get("source_payload_sha256") == di.get("source_payload_sha256"),
        "counts": {"macro": len(m_ids), "desk": len(d_ids), "intersection": len(m_ids & d_ids)},
        "only_in_macro": sorted(i for i in (m_ids - d_ids) if i),
        "only_in_desk": sorted(i for i in (d_ids - m_ids) if i), "divergences": [],
    }
    if not same_anchor:
        report["divergences"].append(
            "ANCHOR_MISMATCH: exécutions dans deux heures UTC différentes — "
            "rejouer les deux modules dans la même heure avant de conclure.")
    elif not same_hash:
        if not report["source_payload_match"]:
            report["divergences"].append(
                "SOURCE_PAYLOAD_MISMATCH: les deux modules n'ont pas lu le "
                "même flux (fetch décalé, miroir, ou cache). Cause probable.")
        else:
            report["divergences"].append(
                "CONTRACT_SCOPE_MISMATCH: même flux, périmètre de contrat "
                "différent — vérifier le parseur numérique et le filtre de "
                "fenêtre avant de suspecter la policy.")
    m_by_id = {e.get("occurrence_id"): e for e in macro.get("events", [])}
    d_by_id = {e.get("occurrence_id"): e for e in desk.get("events", [])}
    field_diffs: List[Dict[str, Any]] = []
    for oid in sorted(i for i in (m_ids & d_ids) if i):
        me, de = m_by_id[oid], d_by_id[oid]
        for field in ("datetime_utc", "impact", "forecast_value", "previous_value",
                      "actual_value", "tier", "is_blackout"):
            if field in me and field in de and me[field] != de[field]:
                field_diffs.append({"occurrence_id": oid, "field": field,
                                    "macro": me[field], "desk": de[field]})
    report["field_divergences"] = field_diffs[:200]
    report["field_divergence_count"] = len(field_diffs)
    if field_diffs:
        report["aligned"] = False
        report["divergences"].append(
            f"FIELD_MISMATCH: {len(field_diffs)} écart(s) sur des événements "
            "pourtant partagés — à traiter avant toute décision de trading.")
    return report



# =============================================================================
# SECTION 1 -- NORMALISATION
# =============================================================================

SCHEMA_VERSION = "2.3.0"  # 2.2.2 v6.2 ; 2.3.0 [C11..C15] parity_hash + TZ adaptatif + committee
PAIR_MAPPING_METHOD = "static_currency_membership_v1"
SESSION_POLICY_VERSION = "exchange_local_dst_aware_v2"  # v2 : +WELLINGTON/SYDNEY (v6.2)
NUMERIC_PARSER_VERSION = "ff_numeric_v2"
CONTENT_HASH_METHOD = "economic_projection_v2"

UTC = timezone.utc

TZ_LONDON = ZoneInfo("Europe/London")
TZ_NEW_YORK = ZoneInfo("America/New_York")
TZ_TOKYO = ZoneInfo("Asia/Tokyo")

# [F2] Fuseau d'AFFICHAGE lu depuis config.TZ_CET (Europe/Paris) : le HTML
# macro ne peut plus mélanger deux fuseaux. Override possible pour les tests
# ou un desk situé ailleurs.
# [COMPAT-C13] Le fuseau d'affichage suit désormais l'OPÉRATEUR (Tunis,
# Paris, Toronto...) au lieu d'être figé sur Europe/Paris côté macro et sur
# Africa/Casablanca côté desk — deux heures différentes pour le même instant.
# Ordre : BLUESTAR_DISPLAY_TZ > fuseau système > config.TZ_CET > Europe/Paris.
# Le fallback reste config.TZ_CET : si la détection système est coupée
# (BLUESTAR_TZ_AUTO=0), le comportement v6.2 est restitué bit pour bit.
# RIEN de décisionnel n'en dépend : priority / hours_until / is_blackout sont
# calculés en UTC pur, et content_hash exclut tout champ d'affichage [F5].
_FALLBACK_DISPLAY_TZ = getattr(_C.TZ_CET, "key", "Europe/Paris")
DISPLAY_TIMEZONE, DISPLAY_TZ_ORIGIN = resolve_display_tz(fallback=_FALLBACK_DISPLAY_TZ)
DEFAULT_DISPLAY_TZ = DISPLAY_TIMEZONE  # alias de compatibilité v4
logger.info("Fuseau d'affichage calendrier : %s (origine=%s)",
            DISPLAY_TIMEZONE, DISPLAY_TZ_ORIGIN)

# Placeholder d'affichage unique, aligné sur models.MacroEvent.from_enriched
# et sur le test ``e['actual'] != '—'`` de app.py. [F3]
ABSENT_DISPLAY = "—"

SESSION_HOURS = {
    # [B8 v6.2] WELLINGTON + SYDNEY ajoutés (audit externe B8, mesuré
    # 15/09/2026) : sans eux, le GDP q/q NZD de mercredi 22:45 UTC —
    # 10:45 à Wellington, 08:45 à Sydney, cœur de la séance ANZ — était
    # étiqueté « OFF ». Fenêtres = conventions de place en heure LOCALE de
    # chaque centre (DST gérée par ZoneInfo, comme LONDON/NEW_YORK/TOKYO).
    "LONDON": (TZ_LONDON, 8 * 60, 16 * 60 + 30),
    "NEW_YORK": (TZ_NEW_YORK, 8 * 60, 17 * 60),
    "TOKYO": (TZ_TOKYO, 8 * 60, 17 * 60),
    "WELLINGTON": (ZoneInfo("Pacific/Auckland"), 5 * 60, 14 * 60),
    "SYDNEY": (ZoneInfo("Australia/Sydney"), 7 * 60, 15 * 60),
}

# [F7] Table fixe : ``strftime("%A")`` dépend de la locale du conteneur.
_DAY_NAMES = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
              "FRIDAY", "SATURDAY", "SUNDAY")

G10_PAIRS: Tuple[str, ...] = (
    "EUR/USD", "GBP/USD", "USD/JPY", "USD/CHF", "USD/CAD", "AUD/USD", "NZD/USD",
    "EUR/GBP", "EUR/JPY", "EUR/CHF", "EUR/CAD", "EUR/AUD", "EUR/NZD",
    "GBP/JPY", "GBP/CHF", "GBP/CAD", "GBP/AUD", "GBP/NZD",
    "AUD/JPY", "AUD/CHF", "AUD/CAD", "AUD/NZD",
    "NZD/JPY", "NZD/CHF", "NZD/CAD",
    "CAD/JPY", "CAD/CHF", "CHF/JPY",
)
EXTRA_PAIRS: Dict[str, Tuple[str, ...]] = {"CNY": ("USD/CNY", "EUR/CNY")}

KNOWN_CURRENCIES: Tuple[str, ...] = (
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "CHF", "CNY",
)
GLOBAL_COUNTRY_TOKENS = {"ALL", "GLOBAL", "WORLD", ""}


def pairs_for_currency(ccy: str) -> List[str]:
    """Appartenance mécanique de la devise à la paire. Aucune causalité."""
    ccy = (ccy or "").upper()
    out = [p for p in G10_PAIRS if ccy in p.split("/")]
    out.extend(p for p in EXTRA_PAIRS.get(ccy, ()) if p not in out)
    return out


class Impact(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    HOLIDAY = "HOLIDAY"
    UNKNOWN = "UNKNOWN"


class Session(str, Enum):
    ASIAN = "ASIAN"
    LONDON = "LONDON"
    NEW_YORK = "NEW_YORK"
    OVERLAP_ASIA_LONDON = "OVERLAP_ASIA_LONDON"
    OVERLAP_LONDON_NY = "OVERLAP_LONDON_NY"
    OFF = "OFF"


class TimeProximity(str, Enum):
    IMMINENT = "IMMINENT"
    SOON = "SOON"
    LATER = "LATER"
    PAST = "PAST"


class EventStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    DUE = "DUE"
    PAST_SCHEDULE = "PAST_SCHEDULE"
    HOLIDAY = "HOLIDAY"


class ActualStatus(str, Enum):
    UNSUPPORTED_BY_SOURCE = "UNSUPPORTED_BY_SOURCE"
    NOT_YET_RELEASED = "NOT_YET_RELEASED"
    RELEASED = "RELEASED"


class PairMappingStatus(str, Enum):
    MAPPED = "MAPPED"
    NO_MAPPING_GLOBAL_EVENT = "NO_MAPPING_GLOBAL_EVENT"
    NO_MAPPING_UNKNOWN_CURRENCY = "NO_MAPPING_UNKNOWN_CURRENCY"


class QualityStatus(str, Enum):
    VALID = "VALID"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


class ReleaseGroupType(str, Enum):
    CENTRAL_BANK_DECISION = "CENTRAL_BANK_DECISION"
    LABOR_MARKET_RELEASE = "LABOR_MARKET_RELEASE"
    SIMULTANEOUS_RELEASE = "SIMULTANEOUS_RELEASE"


IMPACT_ALIASES = {
    "HIGH": Impact.HIGH, "RED": Impact.HIGH,
    "MEDIUM": Impact.MEDIUM, "ORANGE": Impact.MEDIUM, "MED": Impact.MEDIUM,
    "LOW": Impact.LOW, "YELLOW": Impact.LOW,
    "HOLIDAY": Impact.HOLIDAY, "NON-ECONOMIC": Impact.HOLIDAY,
    "GRAY": Impact.HOLIDAY, "GREY": Impact.HOLIDAY,
}

# Projection ``priority`` consommée comme variable de DÉCISION par
# macro_engine (determine_market_regime / _compute_asset_score /
# build_catalysts). Seuils identiques à ``time_proximity``. [inchangé v4]
PRIORITY_PAST = "PAST"
PRIORITY_CRITICAL = "CRITICAL"
PRIORITY_HIGH = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    """[F7] Hash des octets réels, pas d'un décodage ``errors='replace'``."""
    return hashlib.sha256(payload).hexdigest()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text or "")
    norm = "".join(c for c in norm if not unicodedata.combining(c))
    return _SLUG_RE.sub("-", norm.lower()).strip("-")


def parse_source_datetime(raw: Any) -> datetime:
    """Accepte 'Z', '+00:00', '-04:00', naïf (traité UTC). Retourne aware UTC."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("empty datetime")
        txt = raw.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# --- Parseur numérique -------------------------------------------------------
_NUM_RE = re.compile(
    # [M3 v6.1] ≤ ≥ ± ajoutés : mêmes préfixes que macro_engine._parse_feed_rate
    # (audit B5 — deux parseurs, deux vocabulaires = dérive silencieuse). Le
    # feed FF actuel n'émet que < > ~ et nombres nus : zéro impact sur le
    # contenu économique (donc sur le content_hash) — verrouillé par test.
    r"^([<>~≈≤≥±]?)\s*(-?[\d]+(?:[.,][\d]+)*)\s*([KMBT]?)\s*(%?)$", re.IGNORECASE
)
_SCALES = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_THOUSANDS_COMMA = re.compile(r"^-?\d{1,3}(?:,\d{3})+$")
# [COMPAT-C11] BUG PARTAGÉ macro+desk, corrigé ici et à l'identique dans
# calendar_core.py. L'ancienne expression `(?:\.\d{3})+` (UN groupe suffisait)
# lisait "4.375" comme « 4 375 » : la décision Fed à 4.375% devenait 4375.0,
# la SNB à 0.125% → 125.0, un pas de -0.125 → -125.0. Toute comparaison
# forecast/previous sur une décision de taux était donc fausse d'un facteur
# 1000, silencieusement. Deux groupes au minimum sont désormais exigés
# ("12.345.678" reste 12345678.0), un groupe unique redevient un décimal.
# Le flux FF écrit ses milliers avec une VIRGULE (_THOUSANDS_COMMA, intact),
# donc aucun NFP / ventes au détail n'est concerné par ce resserrement.
# content_hash INCHANGÉ : la projection économique hashe forecast.raw /
# previous.raw (la chaîne brute), jamais .value.
_THOUSANDS_DOT = re.compile(r"^-?\d{1,3}(?:\.\d{3}){2,}$")


def _to_float(text: str) -> Optional[float]:
    """[F6] Distingue séparateur de milliers et séparateur décimal.

    Règles, dans l'ordre :
      1. Les deux séparateurs présents → le DERNIER rencontré est le
         décimal, l'autre est un séparateur de milliers ("1.234,5" → 1234.5,
         "1,234.5" → 1234.5).
      2. Un seul séparateur, en groupes de 3 exacts ("1,234", "12.345.678")
         → séparateur de milliers.
      3. Sinon → séparateur décimal ("1,5" → 1.5, "0.25" → 0.25).
    Retourne ``None`` si non convertible (le caller émet UNPARSEABLE).
    """
    t = text.strip()
    has_comma, has_dot = "," in t, "." in t
    try:
        if has_comma and has_dot:
            if t.rfind(",") > t.rfind("."):
                return float(t.replace(".", "").replace(",", "."))
            return float(t.replace(",", ""))
        if has_comma:
            if _THOUSANDS_COMMA.match(t):
                return float(t.replace(",", ""))
            return float(t.replace(",", "."))
        if has_dot:
            if _THOUSANDS_DOT.match(t):
                return float(t.replace(".", ""))
            return float(t)
        return float(t)
    except ValueError:
        return None


class NumericValue(BaseModel):
    """Valeur économique : brut conservé + interprétation numérique explicite."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: Optional[str] = None
    value: Optional[float] = None
    unit: Optional[str] = None
    scale: Optional[str] = None
    parse_status: str = "ABSENT"


ABSENT_NUMERIC = NumericValue()
_PLACEHOLDERS = {"", "-", "—", "–", "n/a", "n.a.", "na", "null", "none", "tentative"}


def normalize_numeric(raw: Any) -> NumericValue:
    """Ne devine jamais l'unité au-delà de ce que le suffixe garantit.
    '0' et 0 ne doivent JAMAIS devenir absents (piège du falsy Python)."""
    if raw is None:
        return ABSENT_NUMERIC
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return NumericValue(raw=str(raw), value=float(raw), unit="number",
                            scale=None, parse_status="PARSED")
    if not isinstance(raw, str):
        return NumericValue(raw=str(raw), parse_status="UNPARSEABLE")

    text = raw.strip()
    if text.lower() in _PLACEHOLDERS:
        return ABSENT_NUMERIC

    status = "PARSED"
    candidate = text
    if "|" in candidate:                      # ex. "0.3% | 1.2% y/y"
        candidate = candidate.split("|", 1)[0].strip()
        status = "COMPOSITE"

    m = _NUM_RE.match(candidate)
    if not m:
        return NumericValue(raw=text, parse_status="UNPARSEABLE")

    prefix, number, suffix, pct = m.groups()
    base = _to_float(number)
    if base is None:
        return NumericValue(raw=text, parse_status="UNPARSEABLE")

    if prefix and status == "PARSED":
        status = "APPROXIMATE"                # "<0.1%", "~2.5"

    suffix = suffix.upper()
    if pct:
        return NumericValue(raw=text, value=base, unit="percent",
                            scale=None, parse_status=status)
    return NumericValue(raw=text, value=base * _SCALES[suffix], unit="number",
                        scale=suffix or None, parse_status=status)


def classify_session(dt_utc: datetime) -> Tuple[Session, List[str]]:
    """Sessions calculées en heure LOCALE de chaque place, DST inclus."""
    active: List[str] = []
    for name, (tz, start_min, end_min) in SESSION_HOURS.items():
        local = dt_utc.astimezone(tz)
        if local.weekday() >= 5:
            continue
        minutes = local.hour * 60 + local.minute
        if start_min <= minutes < end_min:
            active.append(name)

    has_ldn = "LONDON" in active
    has_ny = "NEW_YORK" in active
    # [B8 v6.2] Wellington/Sydney rejoignent le bloc asiatique : l'enum
    # Session est inchangée (aucun consommateur de « session » dans le
    # briefing — voir en-tête v6.2 ; seule la véridicité de la donnée compte).
    has_asia = bool({"TOKYO", "WELLINGTON", "SYDNEY"} & set(active))
    if has_ldn and has_ny:
        session = Session.OVERLAP_LONDON_NY
    elif has_ldn and has_asia:
        session = Session.OVERLAP_ASIA_LONDON
    elif has_ny:
        session = Session.NEW_YORK
    elif has_ldn:
        session = Session.LONDON
    elif has_asia:
        session = Session.ASIAN
    else:
        session = Session.OFF
    return session, sorted(active)


def fmt_until(hours: float) -> str:
    total_min = int(round(abs(hours) * 60))
    d, rem = divmod(total_min, 1440)
    h, m = divmod(rem, 60)
    if d:
        body = f"{d}d {h}h {m}m"
    elif h:
        body = f"{h}h {m}m"
    else:
        body = f"{m}m"
    return body if hours > 0 else f"{body} ago"


# --- Politique de sélection --------------------------------------------------
# [F1] Horizon de veille. Doit rester synchronisé avec v10.WATCH_MAX_H (même
# dette de duplication assumée que TIER_WINDOWS).
FF_WATCH_HORIZON_H = 168.0


def _horizon_diagnosis(feed_horizon_h: Optional[float],
                       feed_status: Dict[str, Any]) -> Tuple[str, bool]:
    """[M2 v6.1] « Flux normalement court » != « flux réellement dégradé ».

    La source FF ne publie qu'une semaine glissante dimanche→samedi (mesuré
    15/09/2026 : famille nextweek/lastweek/thismonth/nextmonth → HTTP 404,
    thisweek seul vivant). L'horizon utile décroît donc structurellement de
    ~154h (dimanche) à ~0h (samedi) : ce n'est PAS une anomalie et le bandeau
    rouge ne doit plus le signaler (il sortait 7 jours sur 7).

    Retourne ``(state, alerte_rouge)`` :
      * ``("unreachable", False)``  — horizon non mesurable (flux vide) ;
      * ``("degraded", True)``      — thisweek en échec alors qu'un autre
        flux a répondu (seul cas où « ne pas voir au-delà du flux » devient
        une anomalie au sens v4/v5) ;
      * ``("nominal_weekly", False)`` — thisweek ok, horizon < 168h : nature
        de la source ; mention neutre (logger.info), pas d'alerte ;
      * ``("full_watch_horizon", False)`` — ≥ 168h (second flux réellement
        publié, ex. via BLUESTAR_SOURCE_URL_NEXT).

    Cas ``feed_status`` vide (raw_data injectée, tests/rejeux) : la donnée
    fournie est considérée de confiance → pas de dégradation affirmée.
    """
    if feed_horizon_h is None:
        return "unreachable", False
    tw = str((feed_status or {}).get("thisweek") or "")
    if feed_status and tw and not tw.startswith("ok"):
        return "degraded", True
    if feed_horizon_h < FF_WATCH_HORIZON_H:
        return "nominal_weekly", False
    return "full_watch_horizon", False

# [F7] Dérivé de config au lieu d'être dupliqué : cohérent avec le
# ``since_h=72`` de macro_engine._events_for_ccys (gating de blackout) et
# avec le split events / events_engine plus bas.
MACRO_RESIDUAL_RISK_WINDOW_H = float(_C.RESIDUAL_RISK_WINDOW_H)


class SelectionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = "1.1.0"
    impact_levels: Tuple[Impact, ...] = (Impact.HIGH,)
    currencies: Optional[Tuple[str, ...]] = None
    include_global_events: bool = True
    # Fenêtre passée = fenêtre de risque résiduel du moteur macro.
    window_past_hours: float = MACRO_RESIDUAL_RISK_WINDOW_H
    # [F7] Fenêtre servie == fenêtre annoncée (168h), plus 192h vs 168h.
    window_future_hours: float = FF_WATCH_HORIZON_H
    imminent_hours: float = 6.0          # INCHANGÉ (décisionnel)
    soon_hours: float = 48.0             # INCHANGÉ (décisionnel)
    display_timezone: str = DISPLAY_TIMEZONE
    max_source_age_seconds: int = int(_C.CALENDAR_CACHE_TTL) * 3
    max_events: int = 6000               # 2 semaines de flux, tous impacts
    include_holidays_in_metadata: bool = True

    @field_validator("currencies")
    @classmethod
    def _upper(cls, v):
        return None if v is None else tuple(sorted({c.upper() for c in v}))

    @field_validator("display_timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
            return v
        except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
            # [COMPAT-C13] Accepte aussi un raccourci opérateur ("TN", "CA-QC")
            # avant de renoncer : l'utilisateur ne doit pas être obligé de
            # connaître la nomenclature IANA pour se déclarer en déplacement.
            resolved = normalize_tz_token(v)
            if resolved:
                return resolved
            logger.error("display_timezone '%s' inconnu — repli sur %s",
                         v, _FALLBACK_DISPLAY_TZ)
            return _FALLBACK_DISPLAY_TZ

    @model_validator(mode="after")
    def _coherent(self):
        if self.window_past_hours < 0 or self.window_future_hours <= 0:
            raise ValueError("window bounds must be positive")
        if not 0 < self.imminent_hours < self.soon_hours:
            raise ValueError("imminent_hours must be < soon_hours")
        return self

    def display_tz(self) -> ZoneInfo:
        return ZoneInfo(self.display_timezone)


DEFAULT_POLICY = SelectionPolicy()


class TimeContext(BaseModel):
    """VOLATIL. Exclu du content_hash. Recalculable à tout instant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    computed_at_utc: datetime
    hours_until: float
    hours_until_display: str
    is_upcoming: bool
    time_proximity: TimeProximity
    status: EventStatus

    @field_serializer("computed_at_utc")
    def _ser(self, v: datetime, _info) -> str:
        return iso_z(v)


class CalendarEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    occurrence_id: str
    event_type_id: str
    release_group_id: Optional[str] = None
    release_group_type: Optional[ReleaseGroupType] = None

    currency: str
    is_global: bool = False
    name: str

    scheduled_at_utc: datetime
    scheduled_at_display: datetime
    display_timezone: str
    date_utc: str
    date_display: str
    day_of_week: str

    impact: Impact
    session: Session
    active_market_centers: Tuple[str, ...] = ()

    forecast: NumericValue = ABSENT_NUMERIC
    previous: NumericValue = ABSENT_NUMERIC
    actual: NumericValue = ABSENT_NUMERIC
    actual_status: ActualStatus = ActualStatus.UNSUPPORTED_BY_SOURCE

    pairs_with_currency_exposure: Tuple[str, ...] = ()
    pair_mapping_status: PairMappingStatus = PairMappingStatus.MAPPED
    pair_mapping_method: str = PAIR_MAPPING_METHOD

    source_index: int
    source_feed: str = ""            # v5 : quel flux a fourni la ligne
    time_context: Optional[TimeContext] = None

    @field_validator("scheduled_at_utc")
    @classmethod
    def _aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("scheduled_at_utc must be timezone-aware")
        return v.astimezone(UTC)

    @field_validator("scheduled_at_display")
    @classmethod
    def _aware_display(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("scheduled_at_display must be timezone-aware")
        return v

    @field_serializer("scheduled_at_utc")
    def _ser_utc(self, v: datetime, _info) -> str:
        return iso_z(v)

    @field_serializer("scheduled_at_display")
    def _ser_display(self, v: datetime, _info) -> str:
        return v.isoformat()

    def with_time_context(self, ctx: TimeContext) -> "CalendarEvent":
        return self.model_copy(update={"time_context": ctx})


class SourceInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    url: str
    urls: Tuple[str, ...] = ()            # v5 : fusion multi-flux [F1]
    feeds_ok: int = 0
    feeds_total: int = 0
    fetched_at_utc: datetime              # [F4] horodatage RÉEL du fetch
    fetch_duration_ms: int = 0
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    payload_bytes: int = 0
    payload_sha256: str
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    supports_actual: bool = False
    from_last_known_good: bool = False
    # [S1] Statut par flux ("ok" / "absent_404" / "error:CODE"), emprunté à
    # calendar_ingestor.SourceInfo.feed_status. Exclu du content_hash (root
    # "source" déjà exclu) : la disponibilité variable du flux bonus ne doit
    # jamais faire dériver le hash économique. Vide ({}) si l'appelant a
    # construit ce SourceInfo directement (compat totale, cf. build_payload).
    feed_status: Dict[str, str] = Field(default_factory=dict)

    @field_serializer("fetched_at_utc")
    def _ser(self, v: datetime, _info) -> str:
        return iso_z(v)


class QualityInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: QualityStatus
    is_stale: bool
    source_age_seconds: int
    raw_event_count: int
    accepted_event_count: int
    rejected_event_count: int
    duplicate_event_count: int
    coverage_start_utc: Optional[str] = None
    coverage_end_utc: Optional[str] = None
    data_quality_score: float = Field(ge=0.0, le=1.0)
    warnings: Tuple[str, ...] = ()
    rejections: Tuple[str, ...] = ()


class CoverageInfo(BaseModel):
    """[S2] Emprunté à calendar_core.CoverageInfo. Sépare deux causes bien
    distinctes d'absence de devise dans l'artefact final :

      - currencies_excluded_by_policy : la source avait des événements pour
        cette devise sur la fenêtre, mais à un niveau d'impact (ou hors
        filtre devise) non retenu par la policy active. Artefact de
        configuration, PAS une information de marché.
      - currencies_no_data_in_source : la source n'avait AUCUN événement
        pour cette devise sur la fenêtre, quel que soit l'impact. Calendrier
        réellement creux -- statut neutre, sans dramatisation.

    Purement diagnostique : n'influence ni la sélection, ni ``priority``,
    ni ``is_blackout``, ni le content_hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start_utc: str
    window_end_utc: str
    currencies_scope: Tuple[str, ...]
    currencies_covered: Tuple[str, ...]
    currencies_excluded_by_policy: Tuple[str, ...]
    currencies_no_data_in_source: Tuple[str, ...]


class CalendarPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    generated_at_utc: datetime
    generator: str = "bluestar-calendar-macro-unified-v6"
    content_hash: Optional[str] = None
    content_hash_method: str = CONTENT_HASH_METHOD
    # [COMPAT-C14] Hash du périmètre de CONTRAT (HIGH+MEDIUM, avant policy,
    # fenêtre ancrée à l'heure ronde). Seul hash réellement comparable entre
    # macro et desk : content_hash est un hash de VUE, post-policy, et projette
    # release_group_id qui dépend de la sélection. Optionnels → tout appelant
    # existant qui construit un CalendarPayload à la main reste valide.
    parity_hash: Optional[str] = None
    parity_hash_method: Optional[str] = None
    parity_window_anchor_utc: Optional[str] = None
    parity_scope_event_count: int = 0
    source: SourceInfo
    quality: QualityInfo
    selection_policy: SelectionPolicy
    # [S2] Optionnel (None) pour rester constructible par tout code qui
    # bâtirait encore un CalendarPayload sans ce champ -- build_payload() le
    # renseigne systématiquement.
    coverage: Optional[CoverageInfo] = None
    session_policy_version: str = SESSION_POLICY_VERSION
    numeric_parser_version: str = NUMERIC_PARSER_VERSION
    events: Tuple[CalendarEvent, ...]

    @field_serializer("generated_at_utc")
    def _ser(self, v: datetime, _info) -> str:
        return iso_z(v)

    @model_validator(mode="after")
    def _invariants(self):
        ids = [e.occurrence_id for e in self.events]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate occurrence_id in payload")
        times = [e.scheduled_at_utc for e in self.events]
        if times != sorted(times):
            raise ValueError("events must be sorted by scheduled_at_utc")
        if self.quality.accepted_event_count != len(self.events):
            raise ValueError("accepted_event_count mismatch")
        return self


# --- Regroupement des publications liées ------------------------------------
_RATE_KEYWORDS = (
    "official cash rate", "overnight rate", "rate statement", "cash rate",
    "monetary policy statement", "interest rate", "policy rate",
    "main refinancing", "federal funds", "bank rate", "fomc statement",
)
_PRESSER_KEYWORDS = ("press conference", "monetary policy press")
_LABOR_KEYWORDS = (
    "non-farm employment change", "unemployment rate", "average hourly earnings",
    "employment change", "claimant count",
)
# [M4 v6.1] Fenêtre rattachement conférence→décision, par devise (minutes).
# Mesures 15/09/2026 sur flux live : BoJ 02:30→05:30 UTC (3,0 h) ; FOMC
# 18:00→18:30 UTC (0,5 h). Toute autre banque reste à 120 min (comportement
# d'origine, verrouillé par test).
_PRESSER_ANCHOR_WINDOW_MIN: Dict[str, float] = {"JPY": 240.0}


def _match(title: str, keywords: Sequence[str]) -> bool:
    low = (title or "").lower()
    return any(k in low for k in keywords)


def assign_release_groups(rows: List[Dict[str, Any]]) -> None:
    """Groupe (mutation in place de 'release_group_*') :
      1. publications strictement simultanées d'un même pays,
      2. conférence de presse rattachée à la décision de taux du même pays
         survenue dans la fenêtre d'ancienneté (120 min par défaut ;
         [M4 v6.1] 240 min pour JPY — mesuré 15/09/2026 sur le flux live :
         décision BoJ 02:30 UTC → conférence 05:30 UTC = 3,0 h, hors de la
         fenêtre v6 ; FOMC 18:00→18:30 UTC = 30 min, dans la fenêtre)."""
    buckets: Dict[Tuple[str, datetime], List[Dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault((row["currency"], row["scheduled_at_utc"]), []).append(row)

    ordered = sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    anchors: Dict[str, Tuple[datetime, str]] = {}

    for (ccy, when), members in ordered:
        titles = [m["name"] for m in members]
        is_cb = any(_match(t, _RATE_KEYWORDS) for t in titles)
        is_presser = all(_match(t, _PRESSER_KEYWORDS) for t in titles)
        is_labor = any(_match(t, _LABOR_KEYWORDS) for t in titles)

        gid: Optional[str] = None
        gtype: Optional[ReleaseGroupType] = None

        if is_presser and ccy in anchors:
            anchor_time, anchor_gid = anchors[ccy]
            window_min = _PRESSER_ANCHOR_WINDOW_MIN.get(ccy, 120.0)
            if timedelta(0) <= (when - anchor_time) <= timedelta(minutes=window_min):
                gid, gtype = anchor_gid, ReleaseGroupType.CENTRAL_BANK_DECISION

        if gid is None and (is_cb or len(members) > 1):
            gid = "grp_" + sha256_hex(f"{ccy}|{iso_z(when)}")[:16]
            if is_cb:
                gtype = ReleaseGroupType.CENTRAL_BANK_DECISION
                anchors[ccy] = (when, gid)
            elif is_labor:
                gtype = ReleaseGroupType.LABOR_MARKET_RELEASE
            else:
                gtype = ReleaseGroupType.SIMULTANEOUS_RELEASE

        for m in members:
            m["release_group_id"] = gid
            m["release_group_type"] = gtype


def _coverage_diagnostics(
    rows: List[Dict[str, Any]],
    selected: List[Dict[str, Any]],
    policy: SelectionPolicy,
    lo: datetime,
    hi: datetime,
) -> CoverageInfo:
    """[S2] Emprunté à calendar_core._coverage_diagnostics. Compare les
    lignes normalisées AVANT filtre de policy (``rows``, restreintes à la
    fenêtre ``[lo, hi]``) et APRÈS (``selected``) pour séparer exclusion-
    policy et silence-source réels, devise par devise."""
    in_window = [r for r in rows if not r["is_global"] and lo <= r["scheduled_at_utc"] <= hi]

    scope: Tuple[str, ...] = policy.currencies if policy.currencies is not None else KNOWN_CURRENCIES
    raw_currencies_in_window = {r["currency"] for r in in_window}
    covered = {r["currency"] for r in selected if not r["is_global"]}

    excluded_by_policy: List[str] = []
    no_data_in_source: List[str] = []
    for ccy in scope:
        if ccy in covered:
            continue
        if ccy in raw_currencies_in_window:
            excluded_by_policy.append(ccy)
        else:
            no_data_in_source.append(ccy)

    return CoverageInfo(
        window_start_utc=iso_z(lo),
        window_end_utc=iso_z(hi),
        currencies_scope=tuple(sorted(scope)),
        currencies_covered=tuple(sorted(covered)),
        currencies_excluded_by_policy=tuple(sorted(excluded_by_policy)),
        currencies_no_data_in_source=tuple(sorted(no_data_in_source)),
    )


def render_coverage_note(coverage: Optional[CoverageInfo],
                         impact_levels: Sequence[Impact]) -> Optional[str]:
    """[S2] Formulation neutre pour un rendu desk. Jamais de vocabulaire de
    risque ("fail-closed", "non écarté") pour un simple constat de
    périmètre de données ; la distinction policy vs source réelle reste
    explicite mais factuelle. Retourne ``None`` si ``coverage`` est absent
    (compat)."""
    if coverage is None:
        return None
    levels = "+".join(lvl.value for lvl in impact_levels)

    if not coverage.currencies_excluded_by_policy and not coverage.currencies_no_data_in_source:
        return f"Couverture calendrier complète ({levels}) sur la fenêtre analysée."

    parts = [f"Couverture calendrier ({levels}) : {', '.join(coverage.currencies_covered) or '—'}."]
    if coverage.currencies_excluded_by_policy:
        parts.append(
            "Hors périmètre de sélection actif (données disponibles, non retenues) : "
            f"{', '.join(coverage.currencies_excluded_by_policy)}.")
    if coverage.currencies_no_data_in_source:
        parts.append(
            "Aucune publication programmée sur la fenêtre pour : "
            f"{', '.join(coverage.currencies_no_data_in_source)}.")
    return " ".join(parts)


def compute_time_context(
    event: CalendarEvent, now_utc: datetime, policy: SelectionPolicy = DEFAULT_POLICY
) -> TimeContext:
    hours = (event.scheduled_at_utc - now_utc).total_seconds() / 3600.0
    if event.impact is Impact.HOLIDAY:
        status = EventStatus.HOLIDAY
    elif hours > 0:
        status = EventStatus.SCHEDULED
    elif hours > -0.5:
        status = EventStatus.DUE
    else:
        status = EventStatus.PAST_SCHEDULE

    if hours <= 0:
        proximity = TimeProximity.PAST
    elif hours <= policy.imminent_hours:
        proximity = TimeProximity.IMMINENT
    elif hours <= policy.soon_hours:
        proximity = TimeProximity.SOON
    else:
        proximity = TimeProximity.LATER

    return TimeContext(
        computed_at_utc=now_utc,
        hours_until=round(hours, 4),
        hours_until_display=fmt_until(hours),
        is_upcoming=hours > 0,
        time_proximity=proximity,
        status=status,
    )


def compute_priority(hours_until: float,
                     policy: SelectionPolicy = DEFAULT_POLICY) -> str:
    """Projection ``priority`` attendue par macro_engine.

    Champ de DÉCISION : determine_market_regime (CRITICAL),
    _compute_asset_score (HIGH → catalyst_pen), build_catalysts
    (CRITICAL/HIGH vs MEDIUM). Seuils IDENTIQUES à ``time_proximity``.
    """
    if hours_until <= 0:
        return PRIORITY_PAST
    if hours_until <= policy.imminent_hours:
        return PRIORITY_CRITICAL
    if hours_until <= policy.soon_hours:
        return PRIORITY_HIGH
    return PRIORITY_MEDIUM


def _normalize_row(
    raw: Any, index: int, policy: SelectionPolicy, display_tz: ZoneInfo,
    feed: str = "",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(raw, dict):
        return None, f"idx={index}: root element is {type(raw).__name__}, expected object"

    title = str(raw.get("title", "") or "").strip()
    if not title:
        return None, f"idx={index}: missing title"

    country = str(raw.get("country", "") or "").strip().upper()
    impact = IMPACT_ALIASES.get(
        str(raw.get("impact", "") or "").strip().upper(), Impact.UNKNOWN)

    try:
        when = parse_source_datetime(raw.get("date"))
    except (ValueError, TypeError) as exc:
        return None, f"idx={index} '{title}': invalid date ({exc})"

    is_global = country in GLOBAL_COUNTRY_TOKENS
    if is_global:
        currency, pairs, mapping = "ALL", (), PairMappingStatus.NO_MAPPING_GLOBAL_EVENT
    elif country in KNOWN_CURRENCIES:
        currency, mapping = country, PairMappingStatus.MAPPED
        pairs = tuple(pairs_for_currency(country))
    else:
        currency, pairs = country, ()
        mapping = PairMappingStatus.NO_MAPPING_UNKNOWN_CURRENCY

    has_actual_key = "actual" in raw
    actual = normalize_numeric(raw.get("actual")) if has_actual_key else ABSENT_NUMERIC
    if actual.parse_status != "ABSENT":
        actual_status = ActualStatus.RELEASED
    elif has_actual_key:
        actual_status = ActualStatus.NOT_YET_RELEASED
    else:
        actual_status = ActualStatus.UNSUPPORTED_BY_SOURCE

    local = when.astimezone(display_tz)
    type_id = f"ff:{currency.lower()}:{slugify(title)}"

    return {
        # occurrence_id : fonction de (event_type_id, UTC) UNIQUEMENT — donc
        # identique entre les deux apps quel que soit le fuseau d'affichage.
        "occurrence_id": sha256_hex(f"{type_id}|{iso_z(when)}")[:32],
        "event_type_id": type_id,
        "release_group_id": None,
        "release_group_type": None,
        "currency": currency,
        "is_global": is_global,
        "name": title,
        "scheduled_at_utc": when,
        "scheduled_at_display": local,
        "display_timezone": policy.display_timezone,
        "date_utc": when.strftime("%Y-%m-%d"),
        "date_display": local.strftime("%Y-%m-%d"),
        "day_of_week": _DAY_NAMES[local.weekday()],       # [F7] locale-safe
        "impact": impact,
        "session": None,
        "active_market_centers": None,
        "forecast": normalize_numeric(raw.get("forecast")),
        "previous": normalize_numeric(raw.get("previous")),
        "actual": actual,
        "actual_status": actual_status,
        "pairs_with_currency_exposure": pairs,
        "pair_mapping_status": mapping,
        "pair_mapping_method": PAIR_MAPPING_METHOD,
        "source_index": index,
        "source_feed": feed,
    }, None


def build_payload(
    raw_list: Any,
    *,
    source: SourceInfo,
    now_utc: datetime,
    policy: SelectionPolicy = DEFAULT_POLICY,
    extra_warnings: Sequence[str] = (),
) -> CalendarPayload:
    """Transforme le payload brut en artefact canonique validé.

    Lève ``ValueError`` uniquement sur une erreur de PROGRAMMATION (racine
    non-liste, volume absurde) — ``build_calendar()`` la rattrape et dégrade.
    """
    if not isinstance(raw_list, list):
        raise ValueError(f"source root must be a JSON array, got {type(raw_list).__name__}")
    if len(raw_list) > policy.max_events:
        raise ValueError(f"payload too large: {len(raw_list)} > {policy.max_events}")

    display_tz = policy.display_tz()
    warnings: List[str] = list(extra_warnings)
    rejections: List[str] = []
    rows: List[Dict[str, Any]] = []

    for index, raw in enumerate(raw_list):
        feed = raw.get("_feed", "") if isinstance(raw, dict) else ""
        payload_row = {k: v for k, v in raw.items() if k != "_feed"} if isinstance(raw, dict) else raw
        row, err = _normalize_row(payload_row, index, policy, display_tz, feed)
        if err:
            rejections.append(err)
            continue
        rows.append(row)

    # [COMPAT-C1] la dérive de vocabulaire n'est jamais neutre (audit B1 desk).
    score_vocab_penalty = 0.0
    if any(r["impact"] is Impact.UNKNOWN for r in rows):
        warnings.append("SOURCE_IMPACT_VOCABULARY_CHANGED")
        score_vocab_penalty = 0.2

    lo = now_utc - timedelta(hours=policy.window_past_hours)
    hi = now_utc + timedelta(hours=policy.window_future_hours)

    # [COMPAT-C2] entonnoir instrumenté : « 0 événement » devient « N écartés
    # par impact / devise / fenêtre » (même vocabulaire que le desk).
    drop = {"impact": 0, "global": 0, "currency": 0, "window": 0}
    selected: List[Dict[str, Any]] = []
    for row in rows:
        if row["impact"] not in policy.impact_levels:
            drop["impact"] += 1
            continue
        if row["is_global"] and not policy.include_global_events:
            drop["global"] += 1
            continue
        if (policy.currencies is not None and not row["is_global"]
                and row["currency"] not in policy.currencies):
            drop["currency"] += 1
            continue
        if not (lo <= row["scheduled_at_utc"] <= hi):
            drop["window"] += 1
            continue
        selected.append(row)
    if rows and not selected:
        warnings.append("SELECTION_EMPTY:"
                        + ",".join(f"{k}={v}" for k, v in drop.items() if v))

    # [S2] Diagnostic de couverture par devise -- AVANT dédoublonnage (qui ne
    # change aucune devise) et sur la même fenêtre [lo, hi] que la sélection.
    coverage = _coverage_diagnostics(rows, selected, policy, lo, hi)

    # [COMPAT-C14] Hash de parité, calculé sur ``rows`` (toutes devises, tous
    # impacts normalisés) AVANT le filtre de policy : c'est ce qui permet à
    # macro (HIGH) et desk (HIGH+MEDIUM) de se comparer sans changer leur
    # contrat de sélection respectif. Fenêtre indépendante de la policy et
    # ancrée à l'heure ronde, sinon deux exécutions à 5 min d'écart
    # divergeraient pour une raison purement horlogère.
    _parity_hash = _parity_method = _parity_anchor = None
    _parity_count = 0
    try:
        _anchor, _plo, _phi = parity_window(now_utc)
        _parity_hash = parity_hash(rows, _plo, _phi)
        _parity_method = PARITY_HASH_METHOD
        _parity_anchor = iso_z(_anchor)
        _parity_count = len(parity_projection(rows, _plo, _phi))
    except Exception as exc:                              # noqa: BLE001
        # Un diagnostic ne doit jamais faire tomber une chaîne de
        # production : on renonce au hash, pas au calendrier.
        logger.error("parity_hash indisponible (%s)", exc)
        warnings.append(f"PARITY_HASH_UNAVAILABLE:{type(exc).__name__}")

    seen: Dict[str, Dict[str, Any]] = {}
    duplicates = 0
    for row in selected:
        if row["occurrence_id"] in seen:
            duplicates += 1
            continue
        seen[row["occurrence_id"]] = row
    selected = list(seen.values())
    if duplicates:
        warnings.append(f"DUPLICATE_OCCURRENCES_DROPPED:{duplicates}")

    selected.sort(key=lambda r: (r["scheduled_at_utc"], r["currency"], r["name"]))
    assign_release_groups(selected)

    events: List[CalendarEvent] = []
    for row in selected:
        session, centers = classify_session(row["scheduled_at_utc"])
        row["session"] = session
        row["active_market_centers"] = tuple(centers)
        event = CalendarEvent(**row)
        events.append(event.with_time_context(
            compute_time_context(event, now_utc, policy)))

    # [F4] Âge réel de la source (fetched_at_utc n'est plus == now_utc).
    age = max(0, int((now_utc - source.fetched_at_utc).total_seconds()))
    is_stale = age > policy.max_source_age_seconds
    if is_stale:
        warnings.append(f"SOURCE_AGE_EXCEEDS_{policy.max_source_age_seconds}S")
    if source.from_last_known_good:
        warnings.append("SERVING_LAST_KNOWN_GOOD")
    if not source.supports_actual:
        warnings.append("SOURCE_DOES_NOT_PROVIDE_ACTUAL")

    # [S1] Un flux SECONDAIRE (nextweek) absent ou en échec n'est PAS traité
    # comme le flux primaire : un 404 mi-semaine est la condition NORMALE de
    # la source (cf. en-tête [F1]/[S1]) et une erreur dessus reste un simple
    # bonus manqué. Repli explicite sur l'ancien calcul feeds_ok/feeds_total
    # si feed_status est vide (compat avec tout SourceInfo construit à la
    # main, hors fetch_raw).
    thisweek_status = source.feed_status.get("thisweek")
    nextweek_status = source.feed_status.get("nextweek")
    primary_failed = False
    if thisweek_status is not None:
        primary_failed = thisweek_status != "ok"
        if primary_failed:
            warnings.append(f"PRIMARY_FEED_FAILED:{thisweek_status}")
        if nextweek_status and nextweek_status not in ("ok", "absent_404"):
            warnings.append(f"SECONDARY_FEED_ERROR:{nextweek_status}")
    elif source.feeds_total and source.feeds_ok < source.feeds_total:
        # Ancien comportement (v5), conservé à l'identique quand la
        # granularité par flux n'est pas disponible.
        primary_failed = True
        warnings.append(f"PARTIAL_FEED_COVERAGE:{source.feeds_ok}/{source.feeds_total}")

    all_times = [r["scheduled_at_utc"] for r in rows]
    coverage_start = min(all_times) if all_times else None
    coverage_end = max(all_times) if all_times else None
    if coverage_end is not None and coverage_end < now_utc:
        warnings.append("ALL_SOURCE_EVENTS_IN_THE_PAST_WEEK_ROLLOVER_PENDING")
    if coverage_start is not None and coverage_start > now_utc + timedelta(days=9):
        warnings.append("SOURCE_COVERAGE_STARTS_TOO_FAR_IN_FUTURE")
    if not rows:
        warnings.append("EMPTY_NORMALIZED_PAYLOAD")
    if rows and not selected:
        # Cas piégeux : flux joignable, lignes normalisées, mais AUCUNE ne
        # passe le filtre d'impact. Sans ce warning, l'app affiche 0 event
        # avec reachable=True — silence indiscernable d'un jour calme.
        warnings.append("NO_EVENT_MATCHED_SELECTION_POLICY")

    score = 1.0
    if is_stale:
        score -= 0.35
    if source.from_last_known_good:
        score -= 0.25
    if rejections:
        score -= min(0.25, 0.05 * len(rejections))
    if primary_failed:
        score -= 0.15
    if "ALL_SOURCE_EVENTS_IN_THE_PAST_WEEK_ROLLOVER_PENDING" in warnings:
        score -= 0.30
    score -= score_vocab_penalty                      # [COMPAT-C1]
    if not rows:
        score = 0.0
    score = round(max(0.0, min(1.0, score)), 3)

    # [COMPAT-C1] Rejet TOTAL par dérive de vocabulaire : des lignes ont
    # survécu à la normalisation, AUCUN événement n'est retenu et le warning de
    # vocabulaire l'atteste = artefact de parsing, pas un calme de marché.
    total_vocab_rejection = bool(rows) and not events \
        and "SOURCE_IMPACT_VOCABULARY_CHANGED" in warnings
    if total_vocab_rejection:
        warnings.append("IMPACT_VOCABULARY_ALL_REJECTED")

    if not rows or score < 0.4 or total_vocab_rejection:
        status = QualityStatus.INVALID
    elif warnings and (is_stale or source.from_last_known_good or score < 0.85):
        status = QualityStatus.DEGRADED
    else:
        status = QualityStatus.VALID

    quality = QualityInfo(
        status=status,
        is_stale=is_stale,
        source_age_seconds=age,
        raw_event_count=len(raw_list),
        accepted_event_count=len(events),
        rejected_event_count=len(rejections),
        duplicate_event_count=duplicates,
        coverage_start_utc=iso_z(coverage_start) if coverage_start else None,
        coverage_end_utc=iso_z(coverage_end) if coverage_end else None,
        data_quality_score=score,
        warnings=tuple(dict.fromkeys(warnings)),   # dédoublonne, ordre stable
        rejections=tuple(rejections[:50]),
    )

    payload = CalendarPayload(
        generated_at_utc=now_utc,
        source=source,
        quality=quality,
        selection_policy=policy,
        coverage=coverage,
        parity_hash=_parity_hash,                     # [COMPAT-C14]
        parity_hash_method=_parity_method,
        parity_window_anchor_utc=_parity_anchor,
        parity_scope_event_count=_parity_count,
        events=tuple(events),
    )
    return payload.model_copy(update={"content_hash": canonical_content_hash(payload)})


def canonical_content_hash(payload: CalendarPayload) -> str:
    """[F5] SHA-256 d'une PROJECTION ÉCONOMIQUE explicite.

    Insensible à : fuseau d'affichage, ordre/origine de fusion des flux,
    horodatages volatils, métadonnées de qualité. Deux instances de l'app
    (macro / TA) configurées avec des fuseaux d'affichage différents
    produisent donc le MÊME hash pour le même calendrier — ce que la v4
    ne pouvait structurellement pas garantir.
    """
    projection = [
        {
            "occurrence_id": e.occurrence_id,
            "event_type_id": e.event_type_id,
            "scheduled_at_utc": iso_z(e.scheduled_at_utc),
            "currency": e.currency,
            "name": e.name,
            "impact": e.impact.value,
            "forecast": e.forecast.raw,
            "previous": e.previous.raw,
            "actual": e.actual.raw,
            "release_group_id": e.release_group_id,
        }
        for e in payload.events
    ]
    canonical = json.dumps(
        {"method": CONTENT_HASH_METHOD, "events": projection},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return "sha256:" + sha256_hex(canonical)


def refresh_time_contexts(
    payload: CalendarPayload, now_utc: datetime
) -> Tuple[CalendarEvent, ...]:
    return tuple(
        e.with_time_context(compute_time_context(e, now_utc, payload.selection_policy))
        for e in payload.events
    )


# =============================================================================
# [COMPAT-C6] CONTRAT INTER-MODULES « calendar-contract-1.0 »
# Bloc IDENTIQUE (octet pour octet) dans calendar_layer.py (macro) et
# calendar_core.py (desk). NE PAS diverger : le test de parité
# (test_calendar_parity.py) compare les deux sorties sur un même flux brut.
#
# Principe : ce bloc n'AJOUTE que des clés (setdefault) — aucune valeur
# existante n'est modifiée, aucun hash existant ne bouge (verrouillé par test).
# =============================================================================
CONTRACT_VERSION = "calendar-contract-1.0"
# Hash COMPARABLE entre politiques différentes (macro HIGH seul vs desk
# HIGH+MEDIUM) : sous-ensemble HIGH, sans release_group_id (le regroupement
# dépend du reste de la sélection : un HIGH simultané d'un MEDIUM est groupé
# côté desk et pas côté macro).
COMPARABLE_HASH_METHOD = "economic_projection_v2:high_no_group"


def comparable_high_hash(events: Iterable["CalendarEvent"]) -> str:
    projection = [
        {
            "occurrence_id": e.occurrence_id,
            "event_type_id": e.event_type_id,
            "scheduled_at_utc": iso_z(e.scheduled_at_utc),
            "currency": e.currency,
            "name": e.name,
            "impact": e.impact.value,
            "forecast": e.forecast.raw,
            "previous": e.previous.raw,
            "actual": e.actual.raw,
        }
        for e in events if e.impact is Impact.HIGH
    ]
    canonical = json.dumps(
        {"method": COMPARABLE_HASH_METHOD, "events": projection},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return "sha256:" + sha256_hex(canonical)


def _augment_contract(legacy: Dict[str, Any], payload: "CalendarPayload",
                      rows: List[Dict[str, Any]], *,
                      feed_sha256: Optional[Dict[str, str]] = None,
                      bounds_basis: str) -> None:
    """Complète ``legacy['metadata']`` avec les clés du contrat commun.

    ``bounds_basis`` dit ce que signifient les clés HISTORIQUES
    ``feed_start_utc / feed_end_utc / feed_horizon_h`` du module appelant
    (elles ne sont PAS modifiées) : ``raw_all_impacts`` (macro : bornes du flux
    brut, tous impacts) ou ``retained_events`` (desk : bornes des événements
    retenus). Les clés ``source_feed_*`` (brut) et ``data_coverage_*`` (retenu)
    ont, elles, le MÊME sens dans les deux modules.
    """
    meta = legacy["metadata"]
    pol, q, cov, src = (payload.selection_policy, payload.quality,
                        payload.coverage, payload.source)
    ref = payload.generated_at_utc

    def _h(t: Optional[datetime]) -> Optional[float]:
        return round((t - ref).total_seconds() / 3600.0, 2) if t is not None else None

    ev_times = [e.scheduled_at_utc for e in payload.events]
    d_start = min(ev_times) if ev_times else None
    d_end = max(ev_times) if ev_times else None
    s_start = parse_source_datetime(q.coverage_start_utc) if q.coverage_start_utc else None
    s_end = parse_source_datetime(q.coverage_end_utc) if q.coverage_end_utc else None

    fs = dict(src.feed_status or {})
    h_end = d_end or s_end
    hz = ((h_end - ref).total_seconds() / 3600.0
          if h_end is not None and h_end > ref else None)
    coverage_short = hz is not None and hz < pol.soon_hours
    rollover = bool(
        coverage_short and fs and fs.get("thisweek", "ok") == "ok"
        and (fs.get("nextweek") or "absent") != "ok"
        and "ALL_SOURCE_EVENTS_IN_THE_PAST_WEEK_ROLLOVER_PENDING" not in q.warnings
    )
    levels = [lvl.value.lower() for lvl in pol.impact_levels]
    claimable = (sorted(set(cov.currencies_covered) | set(cov.currencies_no_data_in_source))
                 if cov is not None else [])
    cov_note = ((render_coverage_note(cov, pol.impact_levels) or "") + (
        " Rollover hebdomadaire en attente : flux semaine suivante non publié "
        "(publication en fin de semaine) — couverture réelle jusqu'au "
        + str(iso_z(d_end) if d_end else "—")
        + " ; au-delà, silence non mesuré, pas risque nul." if rollover else "")
    ).strip() or None

    upcoming = [r for r in rows if r.get("is_upcoming")]
    nxt = upcoming[0] if upcoming else None
    contract: Dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "view_hash": payload.content_hash,
        "high_content_hash": comparable_high_hash(payload.events),
        "high_content_hash_method": COMPARABLE_HASH_METHOD,
        # [COMPAT-C14] Clés IDENTIQUES côté desk : c'est sur elles que
        # reconcile() se prononce.
        "parity_hash": payload.parity_hash,
        "parity_hash_method": payload.parity_hash_method,
        "parity_window_anchor_utc": payload.parity_window_anchor_utc,
        "parity_scope_event_count": payload.parity_scope_event_count,
        "contract_impact_scope": list(CONTRACT_IMPACT_SCOPE),
        # [COMPAT-C13] Traçabilité du fuseau : une heure affichée doit
        # toujours pouvoir être expliquée (origine + version de la base tz).
        "display_tz_origin": DISPLAY_TZ_ORIGIN,
        "timezone_environment": tz_environment(),
        "source_payload_sha256": src.payload_sha256,
        "feed_sha256": dict(feed_sha256 or {}),
        "horizon_reference_utc": iso_z(ref),
        "feed_bounds_basis": bounds_basis,
        "source_feed_start_utc": iso_z(s_start) if s_start else None,
        "source_feed_end_utc": iso_z(s_end) if s_end else None,
        "source_feed_horizon_h": _h(s_end),
        "data_coverage_start_utc": iso_z(d_start) if d_start else None,
        "data_coverage_end_utc": iso_z(d_end) if d_end else None,
        "data_coverage_horizon_h": _h(d_end),
        "week_rollover_pending": rollover,
        "serving_mode": "last_known_good" if src.from_last_known_good else "live",
        "max_source_age_seconds": pol.max_source_age_seconds,
        "raw_event_count": q.raw_event_count,
        "rejections": list(q.rejections),
        "display_timezone": pol.display_timezone,
        "window_past_hours": pol.window_past_hours,
        "window_future_hours": pol.window_future_hours,
        "imminent_hours": pol.imminent_hours,
        "soon_hours": pol.soon_hours,
        "priority_thresholds": {"critical_max_h": pol.imminent_hours,
                                "high_max_h": pol.soon_hours},
        "residual_risk_window_h": pol.window_past_hours,
        "high_impact_count": sum(1 for e in payload.events if e.impact is Impact.HIGH),
        "critical_count": sum(1 for r in rows if r.get("priority") == PRIORITY_CRITICAL),
        "high_count": sum(1 for r in rows if r.get("priority") == PRIORITY_HIGH),
        "medium_count": sum(1 for r in rows if r.get("priority") == PRIORITY_MEDIUM),
        "next_event": ({
            "currency": nxt["currency"], "event_name": nxt["event_name"],
            "hours_until": nxt["hours_until"], "priority": nxt["priority"],
            "datetime_display": nxt["datetime_display"],
        } if nxt else None),
        # Contrat lu par ENGINE (F-3) : couverture revendiquée = couvertes ∪
        # « vraiment vides » ; les exclues-par-policy restent des angles morts.
        "filters_applied": {
            "basis": "machine_policy",
            "policy_version": pol.policy_version,
            "currencies": claimable,
            "impact_levels": levels,
        },
        "impact_levels_included": levels,
        "currencies_filter": list(pol.currencies) if pol.currencies is not None else "ALL",
        "feed_coverage_detail": cov_note,
    }
    for k, v in contract.items():
        meta.setdefault(k, v)
    # Liste explicite des événements À VENIR : ``events`` n'a PAS le même sens
    # dans les deux modules (macro : à venir ; desk : population complète).
    legacy.setdefault("events_upcoming", [dict(r) for r in upcoming])


def rebase_metadata(meta: Dict[str, Any], now_utc: datetime) -> Dict[str, Any]:
    """Recalcule, à l'instant du CONSOMMATEUR, ce qui dépend de l'horloge dans
    un ``metadata`` déjà écrit (les horizons y sont datés de la génération).

    Cas d'usage : un rapport lu 11 h après la génération de calendar.json
    affichait « horizon 111 h » alors que la fin de couverture est à ~100 h.
    Pure, sans effet de bord, ne modifie pas ``meta``.
    """
    def _t(key: str) -> Optional[datetime]:
        v = meta.get(key)
        if not v:
            return None
        try:
            return parse_source_datetime(v)
        except (ValueError, TypeError):
            return None

    def _h_now(key: str) -> Optional[float]:
        t = _t(key)
        if t is None or t <= now_utc:
            return None
        return round((t - now_utc).total_seconds() / 3600.0, 2)

    gen = _t("generated_at_utc")
    age = max(0.0, (now_utc - gen).total_seconds()) if gen else None
    limit = float(meta.get("max_source_age_seconds") or 900)
    return {
        "computed_at_utc": iso_z(now_utc),
        "artifact_age_seconds": int(age) if age is not None else None,
        "artifact_stale_after_seconds": int(limit),
        "artifact_stale": (age > limit) if age is not None else None,
        "feed_horizon_h_now": _h_now("feed_end_utc"),
        "source_feed_horizon_h_now": _h_now("source_feed_end_utc"),
        "data_coverage_horizon_h_now": _h_now("data_coverage_end_utc"),
    }


_LEGACY_SESSION = {
    Session.OVERLAP_LONDON_NY: "OVERLAP",
    Session.OVERLAP_ASIA_LONDON: "LONDON",
    Session.NEW_YORK: "NEW YORK",
    Session.LONDON: "LONDON",
    Session.ASIAN: "ASIAN",
    Session.OFF: "OFF",
}


def _utc_offset_label(dt: datetime) -> str:
    """Libellé « UTC+H » calculé sur l'offset RÉEL du datetime localisé.

    Jamais codé en dur : un « +1 » fixe serait faux en Europe/Paris l'hiver
    comme en Africa/Casablanca pendant le Ramadan. is_blackout / priority ne
    sont pas concernés (UTC pur via hours_until) mais l'AFFICHAGE doit rester
    exact toute l'année et pour n'importe quel display_timezone.
    """
    offset = dt.utcoffset()
    if offset is None:
        return "UTC"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hh, mm = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hh}" + (f":{mm:02d}" if mm else "")


def _disp(nv: NumericValue) -> str:
    """[F3] Contrat d'affichage : toujours une chaîne, jamais None, jamais ""
    — ``ABSENT_DISPLAY`` ("—") quand le flux ne fournit pas la valeur.
    Aligné sur models.MacroEvent.from_enriched et sur le test
    ``e['actual'] != '—'`` de app.py."""
    return nv.raw if nv.raw else ABSENT_DISPLAY


def to_legacy_payload(payload: CalendarPayload, now_utc: datetime) -> Dict[str, Any]:
    """Vue « legacy » consommée par macro_engine / renderer / app.py.
    ``build_calendar()`` y ajoute ensuite priority / tier / blackout et les
    champs de couverture de flux."""
    events = refresh_time_contexts(payload, now_utc)
    policy = payload.selection_policy
    rows: List[Dict[str, Any]] = []
    summary: Dict[str, List[str]] = {}

    for e in events:
        ctx = e.time_context
        offset_lbl = _utc_offset_label(e.scheduled_at_display)
        rows.append({
            "occurrence_id": e.occurrence_id,
            "event_type_id": e.event_type_id,
            "release_group_id": e.release_group_id,
            "release_group_type": (e.release_group_type.value
                                   if e.release_group_type else None),
            "currency": e.currency,
            "event_name": e.name,
            "datetime_utc": iso_z(e.scheduled_at_utc),
            "date_display": e.date_display,
            "time_display": f"{e.scheduled_at_display.strftime('%H:%M')} ({offset_lbl})",
            "datetime_display": (f"{e.date_display} · "
                                 f"{e.scheduled_at_display.strftime('%H:%M')} ({offset_lbl})"),
            "display_timezone": e.display_timezone,
            "day_of_week": e.day_of_week,
            "impact": e.impact.value.lower(),
            "forecast": _disp(e.forecast),
            "forecast_value": e.forecast.value,
            # [M3 v6.1] statut de parsing exporté (calculé puis jeté depuis
            # la v3 — audit B5). Additif : models.from_enriched les ignore,
            # le content_hash ne les projette pas. Consommateurs : diagnostics.
            "forecast_status": e.forecast.parse_status,
            "previous": _disp(e.previous),
            "previous_value": e.previous.value,
            "previous_status": e.previous.parse_status,
            "actual": _disp(e.actual),
            "actual_value": e.actual.value,
            "actual_status": e.actual_status.value,
            "hours_until": ctx.hours_until,
            "hours_until_display": ctx.hours_until_display,
            "is_upcoming": ctx.is_upcoming,
            "time_proximity": ctx.time_proximity.value,
            "status": ctx.status.value,
            "session": _LEGACY_SESSION.get(e.session, e.session.value),  # [F7]
            "session_v2": e.session.value,
            "pairs_affected": list(e.pairs_with_currency_exposure),
            "pair_mapping_status": e.pair_mapping_status.value,
            "source_feed": e.source_feed,
        })
        summary.setdefault(e.date_display, []).append(f"{e.currency} – {e.name}")

    return {
        "metadata": {
            "schema_version": f"legacy-1.2.0+core-{payload.schema_version}",
            "generated_at_utc": iso_z(payload.generated_at_utc),
            "content_hash": payload.content_hash,
            # [M5 v6.1] content_hash est un hash de VUE : il porte la fenêtre
            # 72h/168h et bouge quand un événement franchit une borne (mesuré :
            # T vs T+52h, flux identique octet pour octet). Pour la stabilité
            # inter-apps, comparer source.payload_sha256. Alias explicite :
            "view_hash": payload.content_hash,
            "content_hash_method": payload.content_hash_method,
            "source": payload.source.provider,
            "source_url": payload.source.url,
            "source_urls": list(payload.source.urls),
            "feeds_ok": payload.source.feeds_ok,
            "feeds_total": payload.source.feeds_total,
            # [S1] Statut par flux ("ok"/"absent_404"/"error:CODE"), même clé
            # ("feeds_status") que calendar_core.to_legacy_payload côté app TA.
            "feeds_status": dict(payload.source.feed_status),
            "fetched_at_utc": iso_z(payload.source.fetched_at_utc),
            "supports_actual": payload.source.supports_actual,
            "timezone": f"UTC (backend) / {policy.display_timezone} (display)",
            "display_timezone": policy.display_timezone,
            "window_past_hours": policy.window_past_hours,
            "window_future_hours": policy.window_future_hours,
            "imminent_hours": policy.imminent_hours,
            "soon_hours": policy.soon_hours,
            "quality_status": payload.quality.status.value,
            "data_quality_score": payload.quality.data_quality_score,
            "is_stale": payload.quality.is_stale,
            "source_age_seconds": payload.quality.source_age_seconds,
            "raw_event_count": payload.quality.raw_event_count,
            "rejected_event_count": payload.quality.rejected_event_count,
            "warnings": list(payload.quality.warnings),
            "rejections": list(payload.quality.rejections),
            "total_high_impact": len(rows),
            "upcoming_count": sum(1 for r in rows if r["is_upcoming"]),
            "imminent_count": sum(1 for r in rows if r["time_proximity"] == "IMMINENT"),
            "engine_events_count": len(rows),
            "summary_by_day_basis": "display_timezone",
            "ui_filters_applied": None,
            # [S2] Couverture par devise (policy vs source réelle), mêmes
            # noms de clé que calendar_core.to_legacy_payload.
            "coverage_window_start_utc": payload.coverage.window_start_utc if payload.coverage else None,
            "coverage_window_end_utc": payload.coverage.window_end_utc if payload.coverage else None,
            "currencies_scope": list(payload.coverage.currencies_scope) if payload.coverage else [],
            "currencies_covered": list(payload.coverage.currencies_covered) if payload.coverage else [],
            "currencies_excluded_by_policy": (
                list(payload.coverage.currencies_excluded_by_policy) if payload.coverage else []),
            "currencies_no_data_in_source": (
                list(payload.coverage.currencies_no_data_in_source) if payload.coverage else []),
            "coverage_note": render_coverage_note(payload.coverage, policy.impact_levels),
        },
        "events": rows,
        # [M5 v6.1] copie distincte : la v5 renvoyait le MÊME objet list sous
        # deux clés (mesuré : events is events_engine → True). build_calendar
        # réassigne les deux dès l'appel suivant, mais un appelant direct de
        # to_legacy_payload mutait l'un en écrivant l'autre.
        "events_engine": list(rows),
        "summary_by_day": {k: summary[k] for k in sorted(summary)},
    }


# =============================================================================
# SECTION 2 -- BLACKOUT (réplique EXACTE de v10.py / Desk Engine).
# Sujet ORTHOGONAL à la normalisation ci-dessus : ne JAMAIS fusionner cette
# logique avec calendar_core (l'app TA n'a pas cette notion).
# IMPORTANT : toute modification de TIER_WINDOWS dans v10.py DOIT être
# répercutée ici à l'identique. Dette de duplication assumée.
# =============================================================================

_TIER_S = ("non-farm", "nonfarm", "nfp", "fomc", "cpi", "cash rate",
           "bank rate", "rate statement", "interest rate", "monetary policy",
           "funds rate", "policy rate",
           # [M1 v6.1] réplique v10 l.251 : sans "refinancing", la décision
           # BCE « Main Refinancing Rate » tombait en NONE ici alors que le
           # Desk la classe S. Rétabli à l'identique.
           "refinancing")
_TIER_A = ("gdp", "pmi", "adp", "pce", "employment change", "unemployment",
           "average hourly", "retail sales", "ppi")
_TIER_B = ("speaks", "speech", "press conference", "testifies", "testimony")

TIER_WINDOWS: Dict[str, tuple] = {
    "S": (4.0, 48.0),
    "A": (2.0, 24.0),
    "B": (1.0, 6.0),
}
DEFAULT_TIER_WINDOW = (2.0, 24.0)


def classify_tier(event_name: str) -> str:
    """Identique à v10.classify_tier — mêmes mots-clés, même ordre (S>A>B)."""
    n = (event_name or "").lower()
    if any(k in n for k in _TIER_S):
        return "S"
    if any(k in n for k in _TIER_A):
        return "A"
    if any(k in n for k in _TIER_B):
        return "B"
    return "NONE"


def is_blackout(event_name: str, hours_until: float, impact: str = "high") -> tuple:
    """True si l'événement place sa devise en fenêtre de blackout, avant OU
    après l'annonce — réplique de v10.CalendarData.bucket().

    ``hours_until`` suit la convention de ``build_calendar()`` : positif =
    futur, négatif = déjà passé. Retourne ``(bloqué: bool, tier: str)``.

    [M1 v6.1] Promotion ``NONE``→``A`` des événements HIGH, réplique exacte
    de v10.CalendarEvent._derive (ENGINE.V10.py l.335-336 : « un event
    explicitement HIGH au feed mais au nom non classé ne doit pas être
    invisible du scoring »). La fenêtre réellement appliquée est inchangée
    ((2,24) défaut == (2,24) tier A) : seul le LABEL retourné devient
    conforme à l'énoncé du Desk. ``impact`` n'est pas passé par les appelants
    actuels (macro_engine l.1674) parce que la politique d'ingestion ne
    retient QUE des High ; un appelant élargissant la politique devra passer
    l'impact réel (comportement v10 : non-HIGH → pas de promotion).

    NB : la fenêtre passée la plus large est 48h (tier S) — d'où le
    ``since_h=72`` de macro_engine._events_for_ccys, strictement suffisant,
    et d'où ``window_past_hours = RESIDUAL_RISK_WINDOW_H = 72``.
    """
    tier = classify_tier(event_name)
    if tier == "NONE" and str(impact).lower() == "high":
        tier = "A"
    before, after = TIER_WINDOWS.get(tier, DEFAULT_TIER_WINDOW)
    return (-after <= hours_until <= before), tier


# =============================================================================
# SECTION 3 -- FETCH (résilient, SANS état persistant : pas de circuit
# breaker, pas de last-known-good disque, pas de health.json).
# Deux GET SÉQUENTIELS sur la même session — surtout PAS de ThreadPoolExecutor
# ici : macro_engine documente un SIGSEGV (curl_cffi/libcurl non thread-safe)
# sur les exécuteurs imbriqués.
# =============================================================================

SOURCE_PROVIDER = "Forex Factory / Fair Economy weekly public feed"
USER_AGENT = os.getenv("BLUESTAR_USER_AGENT",
                       "BluestarCalendarMacroUnified/5.0 (+ops@bluestar)")

_THISWEEK_URL = os.getenv("BLUESTAR_SOURCE_URL", _C.FF_JSON_URL)


def _derive_nextweek_url(thisweek_url: str) -> Optional[str]:
    """[F1→M2 v6.1] URL du flux « semaine suivante ».

    Mesuré le 15/09/2026 (audit externe B1, re-vérifié 5 probes sur ~12 h) :
    l'éditeur ne publie AUCUN second flux — ff_calendar_nextweek/lastweek/
    thismonth/nextmonth (.json/.xml) → HTTP 404nginx, seul thisweek vit.
    La substitution automatique « thisweek→nextweek » n'émettait donc qu'une
    requête morte par cycle, avec un feed_status "absent_404" permanent.

    La FUSION multi-flux de [F1] reste entièrement opérationnelle (mécanisme
    inchangé dans fetch_raw) : il suffit de définir ``BLUESTAR_SOURCE_URL_NEXT``
    (config Streamlit / .env) si un second flux devient disponible. Rien n'est
    codé en dur, rien n'est supprimé.
    """
    return os.getenv("BLUESTAR_SOURCE_URL_NEXT") or None


SOURCE_URL = _THISWEEK_URL                      # compat v3/v4
_NEXTWEEK_URL = _derive_nextweek_url(_THISWEEK_URL)
SOURCE_URLS: Tuple[str, ...] = tuple(u for u in (_THISWEEK_URL, _NEXTWEEK_URL) if u)

# [COMPAT-C4] Miroir du flux primaire (même URL que pipeline_calendar.SOURCE_URLS[1]).
SOURCE_MIRROR_URL = os.getenv(
    "BLUESTAR_SOURCE_URL_MIRROR",
    "https://d1tcktd03x2wof.cloudfront.net/ff_calendar_thisweek.json") or None

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


def _build_session() -> requests.Session:
    session = requests.Session()
    retry_kwargs: Dict[str, Any] = dict(
        total=4, connect=3, read=3, status=3,
        backoff_factor=1.5,
        # [COMPAT-C3] 429 EXCLU : un quota n'est pas une panne transitoire ;
        # le réessayer ET dormir sur Retry-After gèle le thread (desk : cause
        # racine de l'écran vide sur Streamlit Cloud) et rallume le quota.
        status_forcelist=(408, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=False,
    )
    try:
        # [F7] backoff_jitter n'existe qu'à partir d'urllib3 2.0 : sur 1.x le
        # constructeur levait TypeError → fetch mort → calendrier vide.
        retry = Retry(backoff_jitter=0.4, **retry_kwargs)
    except TypeError:
        logger.debug("urllib3 < 2.0 : backoff_jitter indisponible")
        retry = Retry(**retry_kwargs)
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    return session


class FetchError(RuntimeError):
    """Conservé pour compatibilité d'import. Le fetch ne lève jamais :
    ``fetch_raw`` retourne toujours ``(liste, méta)``."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _fetch_one(session: requests.Session, url: str) -> Tuple[List[Dict], Dict[str, Any]]:
    """Un seul flux. Ne lève JAMAIS : retourne ``([], {"ok": False, ...})``."""
    started = time.monotonic()
    label = "nextweek" if "nextweek" in url else "thisweek"
    fail = {"ok": False, "url": url, "feed": label, "status": "error:UNKNOWN"}
    try:
        response = session.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True)
    except requests.Timeout as exc:
        logger.error("Calendar fetch failed (%s): timeout (%s)", label, exc)
        return [], {**fail, "status": "error:NETWORK_TIMEOUT"}
    except requests.RequestException as exc:
        logger.error("Calendar fetch failed (%s): %s", label, exc)
        return [], {**fail, "status": "error:NETWORK_ERROR"}

    try:
        with response:
            if response.status_code >= 400:
                # [S1] Un 404 sur le flux secondaire est la condition NORMALE
                # de la source en milieu de semaine (cf. [F1]/[S1]) : c'est un
                # STATUT, pas une erreur à crier au même niveau qu'une vraie
                # panne. Le flux primaire n'a, lui, jamais de 404 "normal".
                # [COMPAT-C3] 429 → statut dédié (vocabulaire desk), 1 seule requête.
                status = ("absent_404" if response.status_code == 404
                          else "rate_limited" if response.status_code == 429
                          else f"error:HTTP_{response.status_code}")
                log = (logger.info if status == "absent_404"
                       else logger.warning if status == "rate_limited"
                       else logger.error)
                log("Calendar fetch (%s): HTTP %s (%s)", label, response.status_code, status)
                return [], {**fail, "http_status": response.status_code, "status": status,
                            "retry_after": response.headers.get("Retry-After")}

            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > MAX_PAYLOAD_BYTES:
                    logger.error("Calendar fetch failed (%s): payload too large (%d B)",
                                 label, size)
                    return [], {**fail, "status": "error:PAYLOAD_TOO_LARGE"}
                chunks.append(chunk)
            body = b"".join(chunks)

            meta = {
                "ok": True,
                "url": url,
                "feed": label,
                "status": "ok",
                "http_status": response.status_code,
                "content_type": (response.headers.get("Content-Type") or "")
                                .split(";")[0].strip() or None,
                "payload_bytes": size,
                # [F7] hash des octets réels
                "payload_sha256": "sha256:" + sha256_bytes(body),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "fetch_duration_ms": int((time.monotonic() - started) * 1000),
            }
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, list):
            logger.error("Calendar fetch failed (%s): root is %s, expected array",
                         label, type(parsed).__name__)
            return [], {**fail, "status": "error:SCHEMA_ROOT_NOT_ARRAY"}
        for row in parsed:
            if isinstance(row, dict):
                row["_feed"] = label
        return parsed, meta
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("Calendar fetch failed (%s): invalid JSON (%s)", label, exc)
        return [], {**fail, "status": "error:INVALID_JSON"}
    except Exception as exc:                      # ceinture + bretelles
        logger.error("Calendar fetch failed (%s): %s", label, exc)
        return [], {**fail, "status": f"error:{type(exc).__name__}"}


def _raw_dedupe_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    """Clé de dédoublonnage inter-flux, robuste aux formats de date."""
    try:
        stamp = iso_z(parse_source_datetime(row.get("date")))
    except (ValueError, TypeError):
        stamp = str(row.get("date"))
    return (str(row.get("country", "") or "").upper(),
            str(row.get("title", "") or "").strip().lower(),
            stamp)


def fetch_raw(urls: Optional[Iterable[str]] = None) -> Tuple[List[Dict], Dict[str, Any]]:
    """[F1] Fetch résilient MULTI-FLUX (thisweek + nextweek), sans état.

    Séquentiel et volontairement non parallélisé (voir en-tête de section).
    Retourne ``([], meta)`` sur échec total — ``build_calendar()`` dégrade
    proprement plutôt que de lever (même contrat que la v3).
    """
    url_list = list(urls) if urls is not None else list(SOURCE_URLS)
    session = _build_session()
    fetched_at = datetime.now(UTC)

    merged: List[Dict] = []
    metas: List[Dict[str, Any]] = []
    for url in url_list:
        rows, meta = _fetch_one(session, url)
        metas.append(meta)
        merged.extend(rows)

    # [COMPAT-C4] Repli sur le miroir SI le primaire a échoué (hors 429) et
    # seulement pour l'appel par défaut (pas quand l'appelant impose ses URLs).
    if urls is None and SOURCE_MIRROR_URL and metas:
        _prim = [m for m in metas if m.get("feed") == "thisweek"]
        if _prim and not _prim[0].get("ok") and _prim[0].get("status") != "rate_limited":
            _rows, _meta = _fetch_one(session, SOURCE_MIRROR_URL)
            _meta["mirror"] = True
            metas.append(_meta)
            merged.extend(_rows)

    seen: set = set()
    deduped: List[Dict] = []
    raw_dupes = 0
    for row in merged:
        if not isinstance(row, dict):
            deduped.append(row)
            continue
        key = _raw_dedupe_key(row)
        if key in seen:
            raw_dupes += 1
            continue
        seen.add(key)
        deduped.append(row)

    ok_metas = [m for m in metas if m.get("ok")]
    # [S1] Statut lisible par flux ("thisweek"/"nextweek" -> "ok"/
    # "absent_404"/"error:CODE"), emprunté à calendar_ingestor.feed_status.
    feed_status: Dict[str, str] = {}
    for m in metas:
        feed_status[m.get("feed", "?")] = m.get("status", "error:UNKNOWN")
    agg: Dict[str, Any] = {
        "fetched_at_utc": fetched_at,
        "urls": tuple(url_list),
        "feeds_total": len(url_list),
        "feeds_ok": len(ok_metas),
        "feeds": metas,
        "feed_status": feed_status,
        # [COMPAT-C5] sha256 des octets PAR flux (traçabilité de la fusion).
        "feed_sha256": {m.get("feed", "?"): m.get("payload_sha256")
                        for m in ok_metas if m.get("payload_sha256")},
        "raw_duplicates_dropped": raw_dupes,
        "fetch_duration_ms": sum(int(m.get("fetch_duration_ms") or 0) for m in metas),
        "payload_bytes": sum(int(m.get("payload_bytes") or 0) for m in metas),
    }
    if ok_metas:
        first = ok_metas[0]
        agg.update({
            "http_status": first.get("http_status"),
            "content_type": first.get("content_type"),
            "etag": first.get("etag"),
            "last_modified": first.get("last_modified"),
            "payload_sha256": "sha256:" + sha256_hex(
                "|".join(str(m.get("payload_sha256")) for m in ok_metas)),
        })
    if not ok_metas:
        logger.error("Calendar fetch: aucun flux disponible sur %d tentés",
                     len(url_list))
    elif len(ok_metas) < len(url_list):
        logger.warning("Calendar fetch: couverture partielle (%d/%d flux) — "
                       "l'horizon de veille peut être tronqué",
                       len(ok_metas), len(url_list))
    return deduped, agg


# =============================================================================
# SECTION 4 -- POINT D'ENTRÉE PUBLIC, compatible macro_engine.py / app.py /
# renderer.py sans AUCUNE modification côté consommateur.
# =============================================================================

def _empty_source(now_utc: datetime, meta: Dict[str, Any]) -> SourceInfo:
    fetched = meta.get("fetched_at_utc") or now_utc
    urls = tuple(meta.get("urls") or SOURCE_URLS)
    return SourceInfo(
        provider=SOURCE_PROVIDER,
        url=urls[0] if urls else SOURCE_URL,
        urls=urls,
        feeds_ok=int(meta.get("feeds_ok") or 0),
        feeds_total=int(meta.get("feeds_total") or len(urls)),
        fetched_at_utc=fetched,
        payload_sha256=str(meta.get("payload_sha256") or "sha256:unknown"),
        supports_actual=False,
        from_last_known_good=False,
        feed_status=dict(meta.get("feed_status") or {}),
    )


def _feed_bounds(raw_data: Sequence[Any]) -> Tuple[Optional[datetime], Optional[datetime],
                                                   Dict[str, int], List[Dict[str, str]]]:
    """Bornes de couverture du flux, TOUS impacts confondus (mesure la
    couverture réelle, pas celle des seuls high-impact retenus), + comptage
    par impact + jours fériés à venir. Aucune décision n'est prise ici : de
    la VISIBILITÉ, pas du gating."""
    start = end = None
    counts: Dict[str, int] = {}
    holidays: List[Dict[str, str]] = []
    for ev in raw_data:
        if not isinstance(ev, dict):
            continue
        try:
            t = parse_source_datetime(ev.get("date"))
        except (ValueError, TypeError):
            continue
        if start is None or t < start:
            start = t
        if end is None or t > end:
            end = t
        imp = IMPACT_ALIASES.get(
            str(ev.get("impact", "") or "").strip().upper(), Impact.UNKNOWN)
        counts[imp.value] = counts.get(imp.value, 0) + 1
        if imp is Impact.HOLIDAY:
            holidays.append({
                "date_utc": t.strftime("%Y-%m-%d"),
                "currency": str(ev.get("country", "") or "").upper(),
                "name": str(ev.get("title", "") or "").strip(),
            })
    return start, end, counts, holidays


def build_calendar(
    now_utc: Optional[datetime] = None,
    raw_data: Optional[List[Dict]] = None,
    policy: SelectionPolicy = DEFAULT_POLICY,
    urls: Optional[Iterable[str]] = None,
) -> Dict:
    """Construit le payload canonique avec le MÊME contrat de sortie que la
    v3/v4 (``metadata`` / ``events`` / ``events_engine`` / ``summary_by_day``),
    en s'appuyant en interne sur la normalisation canonique — donc garanti
    identique, événement par événement (au sens ``occurrence_id``), à ce que
    voit l'app TA.

    Quatre restaurations/corrections explicites par rapport à un branchement
    naïf de ``to_legacy_payload()`` :

      1. ``priority`` (CRITICAL/HIGH/MEDIUM/PAST) — absent du cœur canonique
         mais consommé comme variable de DÉCISION par macro_engine
         (determine_market_regime / _compute_asset_score / build_catalysts).
         Sans lui : incident du 05/08/2026, « Catalyseurs du Jour » vide.
      2. ``metadata.reachable`` / ``feed_horizon_h`` /
         ``feed_horizon_truncated`` — alimentent ``calendar_reachable``,
         ``calendar_feed_truncated`` et ``calendar_feed_horizon_h`` de
         BriefingContext (patch F-15 / P0-1 / V4-04), plus la caption et le
         KPI de app.py.
      3. ``events`` (à venir uniquement) distinct de ``events_engine``
         (à venir + passés dans MACRO_RESIDUAL_RISK_WINDOW_H).
      4. v5 — ``tier`` / ``is_blackout`` par ligne : le Desk et la Macro
         énoncent désormais la MÊME vérité de blackout sur la même donnée,
         et la diagnostic view de app.py peut l'afficher sans recalculer.

    Ne lève jamais : tout échec (réseau, JSON, flux hors-norme) dégrade vers
    un calendrier vide avec ``reachable = False``.
    """
    now_utc = now_utc or datetime.now(UTC)
    meta: Dict[str, Any] = {}
    fetched = False
    if raw_data is None:
        raw_data, meta = fetch_raw(urls)
        fetched = True
    raw_data = list(raw_data or [])

    # --- Couverture réelle du flux, avant tout filtrage --------------------
    feed_start, feed_end, impact_counts, holidays = _feed_bounds(raw_data)
    feed_horizon_h = ((feed_end - now_utc).total_seconds() / 3600.0) if feed_end else None
    # [M2 v6.1] Diagnostic d'horizon : la source ne publie qu'une semaine
    # glissante dim→sam (mesuré 15/09/2026, cf. _derive_nextweek_url). Un
    # horizon < 168h quand thisweek répond est NOMINAL ; le bandeau rouge ne
    # se lève plus que sur un flux PRINCIPAL réellement dégradé.
    _fs = dict(meta.get("feed_status") or {})
    horizon_state, feed_truncated = _horizon_diagnosis(feed_horizon_h, _fs)
    if not raw_data:
        # Flux injoignable : l'horizon n'est pas « tronqué », il est INEXISTANT.
        # Ne pas confondre les deux dans le HTML — reachable=False le dit déjà.
        horizon_state, feed_truncated = "unreachable", False
    elif feed_truncated:
        logger.warning(
            "FF feed PRINCIPAL dégradé (thisweek=%s) — horizon mesuré %.1fh ; "
            "un silence calendaire n'est PAS une absence de risque",
            _fs.get("thisweek"), feed_horizon_h)
    elif horizon_state == "nominal_weekly":
        logger.info(
            "FF feed horizon %.1fh < %.0fh — couverture hebdomadaire nominale "
            "de la source (roulante dim→sam), flux non dégradé : la borne 168h "
            "n'est pas atteignable avec ce fournisseur et ne constitue plus "
            "une alerte",
            feed_horizon_h, FF_WATCH_HORIZON_H)

    # --- Source + payload canonique ---------------------------------------
    urls_used = tuple(meta.get("urls") or (SOURCE_URLS if fetched else ()))
    source = SourceInfo(
        provider=SOURCE_PROVIDER,
        url=(urls_used[0] if urls_used else SOURCE_URL),
        urls=urls_used,
        feeds_ok=int(meta.get("feeds_ok") or (0 if fetched else 0)),
        feeds_total=int(meta.get("feeds_total") or len(urls_used)),
        # [F4] horodatage RÉEL du fetch (== now_utc seulement si raw_data
        # est injecté par un appelant, cas des tests).
        fetched_at_utc=(meta.get("fetched_at_utc") or now_utc),
        fetch_duration_ms=int(meta.get("fetch_duration_ms") or 0),
        http_status=meta.get("http_status"),
        content_type=meta.get("content_type"),
        payload_bytes=int(meta.get("payload_bytes") or 0),
        payload_sha256=str(meta.get("payload_sha256") or "sha256:unknown"),
        etag=meta.get("etag"),
        last_modified=meta.get("last_modified"),
        supports_actual=any(isinstance(r, dict) and "actual" in r for r in raw_data),
        from_last_known_good=False,
        feed_status=dict(meta.get("feed_status") or {}),
    )

    extra_warnings: List[str] = []
    if int(meta.get("raw_duplicates_dropped") or 0):
        extra_warnings.append(
            f"RAW_CROSS_FEED_DUPLICATES_DROPPED:{meta['raw_duplicates_dropped']}")

    # [F7] Volume hors-norme : tronquer + avertir, plutôt que lever et tuer
    # l'app Streamlit.
    if len(raw_data) > policy.max_events:
        logger.error("Calendar payload trop volumineux (%d > %d) — troncature",
                     len(raw_data), policy.max_events)
        extra_warnings.append(f"RAW_PAYLOAD_TRUNCATED_TO_{policy.max_events}")
        raw_data = raw_data[:policy.max_events]

    try:
        payload = build_payload(raw_data, source=source, now_utc=now_utc,
                                policy=policy, extra_warnings=extra_warnings)
    except Exception as exc:
        logger.error("build_payload a échoué (%s) — dégradation vers calendrier vide", exc)
        payload = build_payload([], source=_empty_source(now_utc, meta),
                                now_utc=now_utc, policy=policy,
                                extra_warnings=[*extra_warnings,
                                                f"BUILD_PAYLOAD_FAILED:{type(exc).__name__}"])
        raw_data = []

    legacy = to_legacy_payload(payload, now_utc)
    all_rows = legacy["events"]   # population complète (fenêtre policy)

    # --- 1. priority + 4. tier / blackout (par ligne) ----------------------
    for row in all_rows:
        h = row["hours_until"]
        row["priority"] = compute_priority(h, policy)
        blocked, tier = is_blackout(row["event_name"], h)
        row["tier"] = tier
        row["is_blackout"] = blocked

    # --- 3. Split events (à venir) / events_engine (à venir + résiduel) ----
    upcoming_rows = [r for r in all_rows if r["is_upcoming"]]
    engine_rows = [r for r in all_rows
                   if r["is_upcoming"] or r["hours_until"] >= -MACRO_RESIDUAL_RISK_WINDOW_H]
    legacy["events"] = upcoming_rows
    legacy["events_engine"] = engine_rows

    critical_count = sum(1 for r in all_rows if r["priority"] == PRIORITY_CRITICAL)
    high_count = sum(1 for r in all_rows if r["priority"] == PRIORITY_HIGH)
    medium_count = sum(1 for r in all_rows if r["priority"] == PRIORITY_MEDIUM)
    imminent_count = sum(1 for r in all_rows if r["time_proximity"] == "IMMINENT")
    blackout_rows = [r for r in engine_rows if r["is_blackout"]]

    next_event = None
    if upcoming_rows:
        nxt = upcoming_rows[0]
        next_event = {
            "currency": nxt["currency"],
            "event_name": nxt["event_name"],
            "hours_until": nxt["hours_until"],
            "priority": nxt["priority"],
            "datetime_display": nxt["datetime_display"],
        }

    # --- 2. Couverture du flux + reachable + diagnostics -------------------
    legacy["metadata"].update({
        "total_high_impact": len(all_rows),
        "upcoming_count": len(upcoming_rows),
        "critical_count": critical_count,
        "high_count": high_count,
        "medium_count": medium_count,
        "imminent_count": imminent_count,
        "engine_events_count": len(engine_rows),
        "blackout_count": len(blackout_rows),
        "blackout_currencies": sorted({r["currency"] for r in blackout_rows}),
        "next_event": next_event,
        # reachable : au moins un flux réellement servi (et non « la liste
        # n'est pas vide », qui confondait jour calme et panne réseau).
        "reachable": (int(meta.get("feeds_ok") or 0) > 0) if fetched else bool(raw_data),
        "feed_start_utc": iso_z(feed_start) if feed_start else None,
        "feed_end_utc": iso_z(feed_end) if feed_end else None,
        "feed_horizon_h": round(feed_horizon_h, 1) if feed_horizon_h is not None else None,
        "feed_horizon_truncated": feed_truncated,
        # [M2 v6.1] nature de l'horizon, pour diagnostics/logging :
        # unreachable | degraded | nominal_weekly | full_watch_horizon
        "feed_horizon_state": horizon_state,
        "feed_watch_horizon_h": FF_WATCH_HORIZON_H,
        "feed_impact_counts": impact_counts,
        "raw_duplicates_dropped": int(meta.get("raw_duplicates_dropped") or 0),
        "holidays_upcoming": ([h for h in holidays
                               if h["date_utc"] >= now_utc.strftime("%Y-%m-%d")][:10]
                              if policy.include_holidays_in_metadata else []),
        "priority_thresholds": {"critical_max_h": policy.imminent_hours,
                                "high_max_h": policy.soon_hours},
        "residual_risk_window_h": MACRO_RESIDUAL_RISK_WINDOW_H,
    })

    # [COMPAT-C13] Localisation d'AFFICHAGE, en toute fin de chaîne : après
    # que priority / tier / is_blackout (UTC purs) ont été figés. Réécrit
    # date_display / time_display / datetime_display / day_of_week sur TOUTES
    # les listes de lignes — all_rows, upcoming_rows et engine_rows partagent
    # les mêmes objets dict, une seule passe suffit.
    _tz_key, _tz_origin = resolve_display_tz(
        explicit=policy.display_timezone if policy is not DEFAULT_POLICY else None,
        fallback=_FALLBACK_DISPLAY_TZ)
    try:
        localize_rows(all_rows, _tz_key, _tz_origin)
        # summary_by_day est indexé par date d'AFFICHAGE : sans ce
        # recalcul, un événement de 23:00 UTC apparaîtrait sous la veille
        # dans le sommaire et sous le lendemain dans les lignes.
        legacy["summary_by_day"] = rebuild_summary_by_day(all_rows)
        legacy["metadata"]["display_timezone"] = _tz_key
        legacy["metadata"]["display_tz_origin"] = _tz_origin
        legacy["metadata"]["timezone"] = f"UTC (backend) / {_tz_key} (display)"
    except Exception as exc:                              # noqa: BLE001
        logger.error("Localisation d'affichage impossible (%s) — "
                     "heures conservées en %s", exc, policy.display_timezone)

    # [COMPAT-C5/C6] clés du contrat commun (setdefault : rien d'existant ne change).
    _augment_contract(legacy, payload, all_rows,
                      feed_sha256=meta.get("feed_sha256"),
                      bounds_basis="raw_all_impacts")

    return legacy


def build_committee_view(now_utc: Optional[datetime] = None,
                         raw_data: Optional[List[Dict]] = None,
                         policy: SelectionPolicy = DEFAULT_POLICY,
                         urls: Optional[Iterable[str]] = None,
                         legacy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """[COMPAT-C15] Vue machine destinée à l'app committee.

    Purement additive : ne remplace pas ``build_calendar()``, qui reste le
    point d'entrée de macro_engine / app.py. Passer ``legacy`` pour éviter un
    second fetch quand le calendrier a déjà été construit dans le cycle.
    """
    now_utc = now_utc or datetime.now(UTC)
    if legacy is None:
        legacy = build_calendar(now_utc=now_utc, raw_data=raw_data,
                                policy=policy, urls=urls)
    return to_committee_payload(legacy, now_utc, module="macro")


# --- Compatibilité d'import (appelants directs existants) --------------------
PAIRS_MAP: Dict[str, List[str]] = {ccy: pairs_for_currency(ccy)
                                   for ccy in KNOWN_CURRENCIES}


def get_session(t: datetime) -> str:
    """Conservé pour compatibilité avec tout appelant direct existant.
    ``build_calendar()`` utilise en interne ``classify_session()`` (DST-aware),
    plus précis — cette fonction UTC-only n'est PAS dans le chemin de données
    réel."""
    h = t.hour
    london, ny = 7 <= h < 16, 13 <= h < 22
    if london and ny:
        return "OVERLAP"
    if london:
        return "LONDON"
    if ny:
        return "NEW YORK"
    if 0 <= h < 9:
        return "ASIAN"
    return "OFF"
