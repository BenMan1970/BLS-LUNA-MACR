"""bluestar.credentials — isolated, testable secret/credential resolution.

AUDIT-FIX (25/07/2026, A004/A006): credential resolution used to live
directly inside ``oanda_data.py``, coupled to the data-fetching logic. This
is exactly the kind of coupling that let the ``OANDA_ACCESS_TOKEN`` vs
``OANDA_API_KEY`` naming mismatch survive five separate deployments before
it was caught (24/07/2026) — the resolution logic had no isolated place to
unit-test independently of the rest of the market-data pipeline. Extracted
here with IDENTICAL behavior to the two functions it replaces
(``oanda_data._oanda_creds`` and ``oanda_data._strength_access_token``);
this is a pure move, not a rewrite.

SCOPE NOTE: this module intentionally does NOT also absorb
``external_sources._fred_api_key()`` or ``institutional._resolve_fred_key()``.
The latter carries an explicit, hard-won constraint (documented in
institutional.py): it must not be called from inside a ThreadPoolExecutor
worker without re-verifying thread-safety, following a prior SIGSEGV caused
by ``st.secrets`` access off the main thread. Consolidating all three into
one shared module without first auditing every call site across
institutional.py (702 lines, not fully re-verified here) risks silently
reintroducing that crash. Left untouched — scope limited to what was
directly verified safe (both OANDA resolvers are only ever called from
oanda_data.py's main thread, before its ThreadPoolExecutor block opens).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

__version__ = "2026-09-15.1"  # P0-2/secrets : chargement .env (python-dotenv)


# ---------------------------------------------------------------------------
# MISSION SECRETS (15/09/2026) — le trou identifié : Streamlit ne charge PAS
# .env (il ne lit que st.secrets / secrets.toml). Les trois secrets du desk
# (OANDA_API_KEY ou OANDA_ACCESS_TOKEN, OANDA_ACCOUNT_ID, FRED_API_KEY) sont
# donc relégués dans l'environnement système, rare dans un conteneur. Ce bloc
# comble le trou SANS toucher à la résolution existante : le .env est copié
# dans os.environ (override=False : ce qui existe déjà — secrets.toml, env
# système, service — garde la main), et les fonctions de résolution ci-dessous
# continuent de lire st.secrets PUIS os.environ comme avant.
#
# Discipline thread (cf. docstring du module + SIGSEGV 23/07) : python-dotenv
# n'est importé qu'ici et dans external_sources.py, À L'IMPORT DU MODULE (donc
# thread principal uniquement) — jamais depuis un worker. Idempotent via le
# marqueur os.environ "_BLUESTAR_DOTENV_PATH" : premier importé = unique
# lecteur du fichier ; poser ce marqueur avant tout import désactive le
# chargement .env pour tout le processus (tests « sans clé »).
#
# Chemin alternatif documenté : .streamlit/secrets.toml (toujours prioritaire
# sur le .env grâce à override=False + ordre st.secrets-then-env).
# ---------------------------------------------------------------------------
def _ensure_dotenv_loaded() -> None:
    """Charge le .env projet dans os.environ — main thread, à l'import.

    Échec honnête : absence de python-dotenv ou d'un fichier candidat → log
    WARNING et on continue (les chemins st.secrets / environnement système
    restent fonctionnels). N'invente jamais une clé, ne masque jamais un
    secret déjà posé.
    """
    if os.environ.get("_BLUESTAR_DOTENV_PATH"):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning(
            "python-dotenv absent — le .env n'est PAS chargé depuis "
            "credentials.py ; secrets attendus dans st.secrets "
            "(.streamlit/secrets.toml) ou l'environnement système.")
        return
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("BLUESTAR_DOTENV_PATH"),          # override explicite
        os.path.join(here, ".env"),                       # package bluestar/
        os.path.join(os.path.dirname(here), ".env"),      # racine applicative (app.py)
        os.path.join(os.path.dirname(os.path.dirname(here)), ".env"),  # parent
        os.path.join(os.getcwd(), ".env"),                # courant au lancement
    ]
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            load_dotenv(path, override=False)
        except OSError as exc:  # pragma: no cover — partage réseau éphémère
            logger.warning("Chargement .env impossible (%s) : %s", path, exc)
            continue
        os.environ["_BLUESTAR_DOTENV_PATH"] = path
        logger.warning("BLUESTAR — secrets .env chargés depuis %s "
                       "(override=False ; secrets.toml/environnement système "
                       "prioritaires)", path)
        return
    logger.warning(
        "BLUESTAR — aucun .env trouvé (candidats : BLUESTAR_DOTENV_PATH, "
        "%s, %s, %s, cwd) — les secrets doivent venir de st.secrets/"
        "secrets.toml ou de l'environnement système ; sinon mode sans clé "
        "(dégradation FALLBACK assumée).", here, os.path.dirname(here),
        os.path.dirname(os.path.dirname(here)))


_ensure_dotenv_loaded()

try:
    import streamlit as st  # type: ignore
    _ST_OK = True
except Exception:  # pragma: no cover
    _ST_OK = False

_OANDA_KEY_NAMES = ("OANDA_API_KEY", "OANDA_ACCESS_TOKEN",
                    "oanda_api_key", "oanda_access_token")
_OANDA_ACCOUNT_NAMES = ("OANDA_ACCOUNT_ID", "oanda_account_id")


def oanda_creds() -> tuple[Optional[str], Optional[str]]:
    """Return (api_key, account_id) from st.secrets, then os.environ, else (None, None).

    Tries every documented spelling of the OANDA key (``OANDA_API_KEY`` and
    ``OANDA_ACCESS_TOKEN``, both cases) — same contract, same fallback
    order, same log messages as the function this replaces
    (``oanda_data._oanda_creds``, fixed 24/07/2026 after the
    ``OANDA_ACCESS_TOKEN`` naming mismatch was found in production).
    """
    key = acc = None
    if _ST_OK:
        try:
            for name in _OANDA_KEY_NAMES:
                val = st.secrets.get(name)
                if val:
                    key = val
                    break
            acc = st.secrets.get("OANDA_ACCOUNT_ID") or st.secrets.get("oanda_account_id")
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "st.secrets access failed while resolving OANDA credentials "
                "(%s) — falling back to os.environ, then to yfinance-only "
                "routing if that is empty too.", exc,
            )
            key = acc = None
    if not key:
        key = (os.environ.get("OANDA_API_KEY") or os.environ.get("oanda_api_key")
               or os.environ.get("OANDA_ACCESS_TOKEN") or os.environ.get("oanda_access_token"))
    acc = acc or os.environ.get("OANDA_ACCOUNT_ID") or os.environ.get("oanda_account_id")
    if not key:
        logger.warning(
            "OANDA credentials introuvables sous aucun nom connu "
            "(OANDA_API_KEY / OANDA_ACCESS_TOKEN, ni st.secrets ni "
            "os.environ) — tous les instruments Oanda-capables vont router "
            "vers Frankfurter/yfinance pour cette exécution."
        )
    return (str(key) if key else None, str(acc) if acc else None)
