"""Administrator blueprint.

Only the landing page exists so far: the SAD routes a logged-in administrator to
the Administrator Dashboard. The admin tools (create stock, market hours) are
built in a later phase.
"""

from flask import Blueprint, render_template

from app.routes.auth import require_role

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.before_request
def _administrators_only():
    return require_role("admin")


@bp.route("/")
def dashboard():
    return render_template("admin_dashboard.html")
