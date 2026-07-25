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
