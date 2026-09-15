"""bluestar.fingerprint — preuve de déploiement par module (P0-1, 15/09/2026).

Contexte (rapport « Avis macro external sources.txt », 14/09/2026) : le HTML
prod affichait encore les labels de l'ANCIENNE version de external_sources.py
(« Bank of England · IADB », « FRED » pour la BCE). Preuve qu'une exécution
de production n'utilisait pas le fichier corrigé — copie fantôme ailleurs sur
le PYTHONPATH, ou redémarrage du serveur jamais effectué après le déploiement.

Ce module rend la vérification triviale et permanente : au boot de l'app
(app.py) — et dans toute exécution de pipeline — chaque module critique est
journalisé avec :

    nom · version déclarée · chemin importé (__file__) · sha256[:12] du
    fichier réellement chargé · taille · mtime

Le sha256 est l'identité réelle du fichier : deux copies divergentes du même
module ne peuvent PAS produire le même fingerprint, ce qui démasque toute
copie fantôme dans le PYTHONPATH. Purement additif et hors-thread : importé
et journalisé sur le thread principal uniquement ; aucune valeur n'est
inventée (module absent ou illisible → enregistrement d'erreur explicite).
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

__version__ = "2026-09-15.1"

#: Modules dont le déploiement doit être prouvé au boot (ordre de log stable
#: pour rendre les diffs de logs lisibles). "bluestar" (le package) est ajouté
#: d'office par module_fingerprint quand présent dans la liste.
CORE_MODULES: tuple[str, ...] = (
    "config",
    "credentials",
    "external_sources",
    "calendar_layer",
    "oanda_data",
    "oanda_strength",
    "institutional",
    "macro_engine",
    "regime_engine",
    "interpretation",
    "staleness",
    "validation",
    "renderer",
    "pipeline",
)


def file_sha256(path: str) -> Optional[str]:
    """sha256 hex (64) du fichier ``path``, ou ``None`` si illisible.

    Fichier entier : les modules pèsent < 110 ko, le coût au boot est
    négligeable et un seul octet divergent change le hash — c'est exactement
    le pouvoir distinctif recherché contre les copies fantômes.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:  # pragma: no cover — partage réseau éphémère
        logger.warning("fingerprint: lecture impossible de %s (%s)", path, exc)
        return None


def module_fingerprint(name: str) -> dict:
    """Fingerprint d'un sous-module ``bluestar.<name>`` importé à CE moment.

    Retourne un dict ``{module, version, file, sha256, size, mtime, error}``
    — ne lève jamais ; un module non importable produit ``error`` renseigné
    et le reste à ``None`` (échec honnête, pas de valeur inventée).
    """
    rec: dict = {"module": name, "version": None, "file": None,
                 "sha256": None, "size": None, "mtime": None, "error": None}
    try:
        import importlib
        mod = importlib.import_module(f"bluestar.{name}")
    except Exception as exc:  # noqa: BLE001 — point défensif : le boot ne doit JAMAIS
        # planter à cause d'une journalisation de diagnostic ; tout est loggé.
        rec["error"] = f"import failed: {exc!r}"
        return rec
    path = getattr(mod, "__file__", None) or "?"
    rec["version"] = getattr(mod, "__version__", None)
    rec["file"] = str(path)
    try:
        st = os.stat(path)
        rec["size"] = st.st_size
        rec["mtime"] = int(st.st_mtime)
        sha = file_sha256(path)
        rec["sha256"] = sha[:12] if sha else None
    except OSError as exc:  # pragma: no cover
        rec["error"] = f"stat failed: {exc!r}"
    return rec


def boot_fingerprints(modules: Optional[tuple[str, ...]] = None) -> list[dict]:
    """Fingerprints de ``modules`` (défaut : ``CORE_MODULES``). Pur, sans log."""
    return [module_fingerprint(m) for m in (modules or CORE_MODULES)]


def log_boot_fingerprints(modules: Optional[tuple[str, ...]] = None) -> list[dict]:
    """P0-1 — journalise le fingerprint de chaque module critique, au boot.

    Niveau WARNING : c'est le niveau par défaut de ``logging.basicConfig``
    dans app.py, donc la preuve est VISIBLE dans les logs Streamlit sans
    reconfiguration. Retourne les enregistrements (utilisables par l'UI).
    """
    recs = boot_fingerprints(modules)
    logger.warning("BLUESTAR FINGERPRINT — v%s, %d modules, cwd=%s",
                   __version__, len(recs), os.getcwd())
    for r in recs:
        if r["error"]:
            logger.warning("  %-18s ERREUR : %s", r["module"], r["error"])
            continue
        logger.warning("  %-18s v=%-14s sha=%s size=%-7s file=%s",
                       r["module"], r["version"] or "-", r["sha256"],
                       r["size"], r["file"])
    return recs


def summary_for_ui(recs: list[dict]) -> str:
    """Ligne compacte pour la sidebar Streamlit (proof-of-deploy visuelle)."""
    def _one(name: str) -> str:
        for r in recs:
            if r["module"] == name:
                if r["error"]:
                    return f"{name}: ERREUR"
                return f"{name} {(r['sha256'] or '?')[:8]}"
        return f"{name}: -"
    return " · ".join(_one(n) for n in ("external_sources", "calendar_layer",
                                        "macro_engine", "pipeline"))
