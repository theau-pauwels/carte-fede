from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from .models import db, Membership, User, Role

bp_mem = Blueprint("memberships", __name__)

# ---------- Membres : consulter ses cartes ----------
@bp_mem.route("/api/memberships", methods=["GET"])
@login_required
def my_memberships():
    rows = (
        Membership.query
        .filter_by(user_id=current_user.id)
        .order_by(Membership.annee.desc())
        .all()
    )
    return jsonify([{"annee": r.annee, "annee_code": r.annee_code} for r in rows])


# ---------- Helpers (validation/normalisation) ----------
ALLOWED_PREFIXES = {"A", "F", "E", "EA", "MI", "S"}

def normalize_card_code(raw: str) -> str:
    """
    Normalise un code 'PREFIX-NUM':
      - majuscules, espaces retirés
      - PREFIX ∈ ALLOWED_PREFIXES
      - NUM entier >= 1 (zéros de tête supprimés)
    """
    if not raw:
        raise ValueError("annee_code requis")
    s = raw.strip().upper()
    if "-" not in s:
        raise ValueError("Format attendu: PREFIX-NUM (ex: A-23)")
    prefix, num = s.split("-", 1)
    prefix = prefix.strip()
    num = num.strip()
    if prefix not in ALLOWED_PREFIXES:
        raise ValueError(f"Préfixe invalide. Autorisés: {', '.join(sorted(ALLOWED_PREFIXES))}")
    if not num.isdigit():
        raise ValueError("La partie numérique doit être un entier")
    n = int(num)
    if n < 1:
        raise ValueError("Le numéro doit être ≥ 1")
    return f"{prefix}-{n}"

def parse_year_range_to_start(raw: str) -> int:
    """
    Accepte '2025' ou '2025-2026' et retourne l'année de début (int).
    """
    s = str(raw).strip()
    if "-" in s:
        left, _ = s.split("-", 1)
        s = left.strip()
    try:
        y = int(s)
    except Exception:
        raise ValueError("Année invalide")
    if y < 1900 or y > 2100:
        raise ValueError("Année hors plage")
    return y


# ---------- Admin : lister / créer / mettre à jour une carte ----------
@bp_mem.route("/api/admin/users/<user_id>/annees", methods=["GET", "PUT"])
@login_required
def upsert_or_list_year(user_id):
    # admin only
    u = User.query.get(user_id)
    if not u:
        return jsonify({"error": "User introuvable"}), 404
    if getattr(current_user, "role", None) != Role.ADMIN:
        return jsonify({"error": "Forbidden"}), 403

    if request.method == "GET":
        rows = (
            Membership.query
            .filter_by(user_id=user_id)
            .order_by(Membership.annee.desc())
            .all()
        )
        return jsonify([{"annee": r.annee, "annee_code": r.annee_code} for r in rows])

    # PUT: upsert (année -> code)
    data = request.json or {}
    try:
        annee_start = parse_year_range_to_start(data.get("annee"))
        code = normalize_card_code(data.get("annee_code", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    # 1 carte max par user et par année
    row = Membership.query.filter_by(user_id=u.id, annee=annee_start).first()

    # code unique **par année** (mais réutilisable sur une autre année)
    same_year_conflict = (
        Membership.query
        .filter_by(annee=annee_start, annee_code=code)
        .first()
    )
    if same_year_conflict and (not row or same_year_conflict.id != row.id):
        return jsonify({"error": "Ce numéro de carte est déjà utilisé pour cette année."}), 409

    if row:
        row.annee_code = code
    else:
        row = Membership(user_id=u.id, annee=annee_start, annee_code=code)
        db.session.add(row)

    db.session.commit()
    return jsonify({"ok": True, "id": row.id})


# ---------- Admin : suppression d’une carte ----------
@bp_mem.route("/api/admin/users/<user_id>/annees/<int:annee>", methods=["DELETE"])
@login_required
def delete_year_card(user_id, annee):
    # admin only
    u = User.query.get(user_id)
    if not u:
        return jsonify({"error": "User introuvable"}), 404
    if getattr(current_user, "role", None) != Role.ADMIN:
        return jsonify({"error": "Forbidden"}), 403

    row = Membership.query.filter_by(user_id=user_id, annee=annee).first()
    if not row:
        return jsonify({"error": "Carte introuvable pour cette année"}), 404

    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})

# --- QR code: génération & vérification ---
from flask import current_app, send_file
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import io
import qrcode

def _qr_serializer():
    # token signé avec la SECRET_KEY
    return URLSafeTimedSerializer(
        secret_key=current_app.config["SECRET_KEY"],
        salt="qr-token-v1"
    )

def _abs_host():
    # ex: http://localhost/
    base = request.host_url  # ex: 'http://localhost/'
    return base

@bp_mem.route("/api/qr/<int:annee>.png", methods=["GET"])
@login_required
def qr_png_for_year(annee: int):
    """Génère un QR PNG pour l'utilisateur connecté et l'année donnée.

    Le QR encode une URL absolue /verify?token=...
    """
    # retrouver la carte de l'utilisateur pour cette année
    row = Membership.query.filter_by(user_id=current_user.id, annee=annee).first()
    if not row:
        return jsonify({"error": "Aucune carte pour cette année"}), 404

    s = _qr_serializer()
    payload = {
        "uid": current_user.id,
        "annee": row.annee,
        "annee_code": row.annee_code,
    }
    # token valable 400 jours (exemple)
    token = s.dumps(payload)

    verify_url = _abs_host().rstrip("/") + "/verify?token=" + token
    # génère le QR PNG
    img = qrcode.make(verify_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")

@bp_mem.route("/api/verify", methods=["GET"])
def api_verify():
    """API JSON: ?token=... -> {valid:bool, ...}"""
    token = request.args.get("token", "").strip()
    if not token:
        return jsonify({"valid": False, "reason": "missing token"}), 400

    s = _qr_serializer()
    try:
        data = s.loads(token, max_age=400*24*3600)  # même TTL que génération
    except SignatureExpired:
        return jsonify({"valid": False, "reason": "expired"}), 400
    except BadSignature:
        return jsonify({"valid": False, "reason": "bad-signature"}), 400

    # RE-vérifier en DB que la carte est toujours valide et correspond
    u = User.query.get(data.get("uid"))
    if not u:
        return jsonify({"valid": False, "reason": "user-not-found"}), 404

    row = Membership.query.filter_by(user_id=u.id, annee=data.get("annee")).first()
    if not row:
        return jsonify({"valid": False, "reason": "card-not-found"}), 404

    if row.annee_code != data.get("annee_code"):
        return jsonify({"valid": False, "reason": "code-mismatch"}), 400

    # OK
    return jsonify({
        "valid": True,
        "user": {"nom": u.nom, "prenom": u.prenom},
        "annee": row.annee,
        "periode": f"{row.annee}-{row.annee+1}",
        "code": row.annee_code
    })

@bp_mem.route("/verify", methods=["GET"])
def human_verify_page():
    """Page HTML simple si on ouvre directement l'URL du QR dans un navigateur."""
    token = request.args.get("token", "")
    # On réutilise l'API JSON côté serveur
    with current_app.test_request_context(f"/api/verify?token={token}"):
        resp = api_verify()
        # resp est (jsonify, status) ou jsonify direct
        if isinstance(resp, tuple):
            data, status = resp
        else:
            data, status = resp, 200
        j = data.get_json()
    ok = j.get("valid")
    if ok:
        return f"""
<!doctype html><meta charset="utf-8">
<h1>Carte valide ✅</h1>
<p><strong>{j['user']['prenom']} {j['user']['nom']}</strong></p>
<p>Période : {j['periode']}<br>Code : {j['code']}</p>
""", 200
    else:
        return f"""
<!doctype html><meta charset="utf-8">
<h1>Carte invalide ❌</h1>
<p>Raison : {j.get('reason','unknown')}</p>
""", 400
