"""Company-logo uploads (SAD Administrator Requirement: "upload a company logo
image during stock creation").

This file answers two questions:
  1. Is this uploaded file really a small image we are willing to store?
  2. Where do we store it, under what name, and how do we remove it later?

Why so careful? An upload is a file chosen by someone else and stored on OUR
server, then shown to every customer. So nothing the uploader tells us is trusted:

  * The file's CONTENT decides whether it is an image. Every image format starts
    with a few fixed "signature" bytes (a PNG always starts with the bytes
    89 50 4E 47 ..., which spell "PNG"). We check those. The file name
    ("logo.png") and the browser's claimed type are ignored, because both are
    trivially faked.
  * SVG is refused on purpose. SVG is really text that can contain <script>,
    which could run in a customer's browser.
  * The stored file gets a brand-new random name, so a hostile file name
    (for example ``../../.env``) can never choose where the file is written.
  * Size is capped at 1 MB.

No Flask imports, so it can be tested directly.
"""

import os
import re
import uuid

from app.services.trading import ValidationError

MAX_LOGO_BYTES = 1024 * 1024  # 1 MB

# Matches ONLY names this module creates: 32 random hex characters + an image
# extension. Used to refuse anything else when serving or deleting files.
STORED_NAME_PATTERN = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")

LOGO_TYPE_MESSAGE = "The logo must be a PNG, JPEG, GIF or WebP image."
LOGO_SIZE_MESSAGE = "The logo must be 1 MB or smaller."


def detect_image_extension(data):
    """Look at the file's first bytes and return 'png', 'jpg', 'gif' or 'webp', else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    # WebP files are a "RIFF" container whose bytes 8-11 say "WEBP".
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def check_logo(data):
    """Validate the uploaded bytes and return the extension to store them under.

    Raises ``ValidationError`` with a message fit to show the administrator if
    the file is too big or is not a supported image.
    """
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationError(LOGO_SIZE_MESSAGE)
    extension = detect_image_extension(data)
    if extension is None:
        raise ValidationError(LOGO_TYPE_MESSAGE)
    return extension


def save_logo(folder, data, extension):
    """Write the bytes into ``folder`` under a new random name and return that name."""
    os.makedirs(folder, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{extension}"  # uuid4 = 32 random hex characters
    with open(os.path.join(folder, name), "wb") as f:
        f.write(data)
    return name


def delete_logo(folder, name):
    """Remove a stored logo. Does nothing unless ``name`` looks like one we created."""
    if not name or not STORED_NAME_PATTERN.match(name):
        return
    try:
        os.remove(os.path.join(folder, name))
    except FileNotFoundError:
        pass  # already gone: nothing to do
