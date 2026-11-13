"""Serves the uploaded company logos at ``/media/logos/<name>``.

Only names that match the pattern this app generates (32 random hex characters
plus .png/.jpg/.gif/.webp) are served, and they are always looked up inside the
upload folder. So a request such as ``/media/logos/../../.env`` can never reach
any other file on the server; it simply gets "404 Not Found".
"""

from flask import Blueprint, abort, current_app, send_from_directory

from app.services.uploads import STORED_NAME_PATTERN

bp = Blueprint("media", __name__)


@bp.route("/media/logos/<filename>")
def logo(filename):
    if not STORED_NAME_PATTERN.match(filename):
        abort(404)
    # send_from_directory also refuses any path that tries to leave the folder.
    response = send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)
    # "nosniff" tells the browser: treat this as the image type we said it is,
    # never guess it might be a web page or a script.
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
