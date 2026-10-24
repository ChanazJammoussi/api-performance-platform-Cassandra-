"""
model_identity.py -- identite et tracabilite du modele REELLEMENT charge par le
detecteur (lot 2b-5).

Distinctions :
  - modele PRESENT sur disque (iforest_latest.joblib) : ne prouve rien a lui seul ;
  - modele CHARGE et ACTIF dans le detecteur : celui dont les octets ont ete lus,
    hashes, charges et valides par ce processus (ModelTracker) ;
  - modele ENREGISTRE pour une detection : l'identite ecrite dans
    anomalies.contributing_features["model"] au moment du calcul.

L'identite = SHA-256 des octets EXACTEMENT charges (lus une seule fois, puis
deserialises depuis ces memes octets) : un fichier remplace entre la lecture et
le chargement ne peut pas produire une identite fausse.
Aucun chemin local n'est expose dans l'identite.
"""

import hashlib
import io
import os
from datetime import datetime, timezone

import joblib

import ml_model
import validation_checks as vc

# Statuts d'un chargement.
LOADED, UNREADABLE, INCOMPATIBLE, ABSENT = "charge", "illisible", "incompatible", "absent"


def _now():
    return datetime.now(timezone.utc)


def artifact_sha256(raw):
    """SHA-256 hexadecimal d'un artefact (octets)."""
    return hashlib.sha256(raw).hexdigest()


def load_artifact(path):
    """
    Charge un artefact {model, meta}. Renvoie un dict :
      statut  : charge | illisible | incompatible | absent
      sha256  : empreinte des octets lus (None si illisible avant lecture)
      bundle  : le modele (uniquement si statut == charge)
      trained_at, feature_fingerprint, raisons
    """
    try:
        raw = open(path, "rb").read()
    except FileNotFoundError:
        return {"statut": ABSENT, "sha256": None, "bundle": None, "raisons": ["artefact absent"]}
    except OSError as e:
        return {"statut": UNREADABLE, "sha256": None, "bundle": None, "raisons": [f"lecture impossible ({e})"]}
    sha = artifact_sha256(raw)
    try:
        bundle = joblib.load(io.BytesIO(raw))
    except Exception as e:
        return {"statut": UNREADABLE, "sha256": sha, "bundle": None,
                "raisons": [f"deserialisation impossible ({type(e).__name__})"]}
    problems = ml_model.check_bundle_compat(bundle)
    if problems:
        return {"statut": INCOMPATIBLE, "sha256": sha, "bundle": None, "raisons": problems}
    meta = bundle["meta"]
    return {"statut": LOADED, "sha256": sha, "bundle": bundle, "raisons": [],
            "trained_at": meta.get("trained_at"),
            "feature_fingerprint": vc.feature_fingerprint(meta["feature_names"])}


class ActiveModel:
    """Instantane immuable (modele + identite) utilise pour UN cycle de detection."""
    __slots__ = ("bundle", "identity")

    def __init__(self, bundle, identity):
        self.bundle = bundle
        self.identity = identity


class ModelTracker:
    """
    Etat du modele dans le detecteur. Recharge l'artefact si sa signature
    (mtime, taille) change. Un nouvel artefact illisible ou incompatible est REFUSE :
    l'ancien modele (s'il existe) reste actif et la tentative echouee est tracee,
    jamais presentee comme une activation. Un artefact disparu ne desactive pas le
    modele deja charge (comportement historique du detecteur).
    """

    def __init__(self, path_fn=None, clock=_now):
        self._path_fn = path_fn or ml_model.latest_path
        self._clock = clock
        self._active = None        # dict : bundle, sha256, trained_at, feature_fingerprint, charge_a
        self._signature = None     # (mtime, taille) de l'artefact actif
        self.last_attempt = None   # dict : resultat, sha256, a, raisons

    def refresh(self):
        """
        Tente un (re)chargement si necessaire. Renvoie le resultat de la tentative
        (dict de load_artifact sans le modele) ou None si rien n'a change.
        """
        path = self._path_fn()
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return self._record({"statut": ABSENT, "sha256": None, "raisons": ["artefact absent"]})
        except OSError as e:
            return self._record({"statut": UNREADABLE, "sha256": None, "raisons": [f"stat impossible ({e})"]})
        signature = (st.st_mtime, st.st_size)
        if self._active is not None and signature == self._signature:
            return None
        res = load_artifact(path)
        if res["statut"] == LOADED:
            self._active = {"bundle": res["bundle"], "sha256": res["sha256"],
                            "trained_at": res["trained_at"],
                            "feature_fingerprint": res["feature_fingerprint"],
                            "charge_a": self._clock().isoformat()}
            self._signature = signature
        return self._record(res)

    def _record(self, res):
        attempt = {"resultat": res["statut"], "sha256": res.get("sha256"),
                   "a": self._clock().isoformat(), "raisons": list(res.get("raisons", []))}
        changed = self.last_attempt is None or (
            (self.last_attempt["resultat"], self.last_attempt["sha256"])
            != (attempt["resultat"], attempt["sha256"]))
        self.last_attempt = attempt
        return dict(attempt, nouveau=changed)

    def identity(self):
        """Identite du modele ACTIF (sans chemin), + derniere tentative si elle a echoue."""
        if self._active is None:
            ident = {"statut": "aucun_modele_actif"}
        else:
            ident = {"statut": "actif", "sha256": self._active["sha256"],
                     "trained_at": self._active["trained_at"],
                     "feature_fingerprint": self._active["feature_fingerprint"],
                     "charge_a": self._active["charge_a"]}
        if self.last_attempt is not None and self.last_attempt["resultat"] != LOADED:
            ident["dernier_chargement_refuse"] = {
                k: self.last_attempt[k] for k in ("resultat", "sha256", "a", "raisons")}
        return ident

    def current(self):
        """Instantane coherent pour un cycle : le MEME objet sert au calcul et a la tracabilite."""
        bundle = self._active["bundle"] if self._active else None
        return ActiveModel(bundle, self.identity())


def detection_identity(active, ml_used):
    """
    Identite a enregistrer pour UNE detection : identite du modele actif du cycle
    + utilise_pour_ce_point (le score ML a-t-il effectivement ete calcule ?).
    """
    ident = dict(active.identity)
    ident["utilise_pour_ce_point"] = bool(ml_used and active.bundle is not None)
    return ident


def identity_from_contributing(contributing):
    """
    Identite du modele ENREGISTREE pour une detection (contributing_features).
    Si elle est absente (detection anterieure a la tracabilite), renvoie
    statut 'inconnue' : on ne la deduit JAMAIS du modele actuellement sur disque.
    """
    model = (contributing or {}).get("model") if isinstance(contributing, dict) else None
    if not isinstance(model, dict) or "statut" not in model:
        return {"statut": "inconnue",
                "raison": "aucune identite de modele enregistree pour cette detection "
                          "(anterieure a la tracabilite) ; non deduite du disque"}
    return dict(model)
