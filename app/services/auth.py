"""Authentication service: sign up new customers and check logins.

No Flask imports here (like every service), so all of it can be tested without a
web server. The routes in ``app/routes/auth.py`` keep the session; this module
only decides whether the details are acceptable.

Rules (the SAD and business case only say "log in securely with valid
credentials" and "allow new users to create an account", so these are our
choices and are listed here in one place):

  * First and last name: required, up to 100 characters.
  * Email: required, must look like an email address, stored in lowercase.
  * Username: 3-30 characters, letters, digits and underscores only.
  * Password: 8-128 characters with at least one letter and one digit.
    It is stored ONLY as a Werkzeug hash, never as text.
  * A new account is always a customer and starts with 0.00 cash; the cash
    deposit feature is how they get money to trade with.
  * A failed login never says whether the username or the password was wrong.
"""

import re
from decimal import Decimal

from werkzeug.security import check_password_hash, generate_password_hash

from app import db

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,30}$")

NAME_MAX = 100
EMAIL_MAX = 255
PASSWORD_MIN = 8
PASSWORD_MAX = 128  # an upper limit stops absurdly long passwords from slowing the hashing

GENERIC_LOGIN_ERROR = "Invalid username or password."

# Checked when the username does not exist, so a wrong username takes about as
# long as a wrong password (otherwise the response time would reveal which
# usernames exist). Built on first use because hashing is deliberately slow.
_dummy_hash = None


class SignupError(Exception):
    """The signup details were not acceptable. ``errors`` maps field name -> message."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors.values()))


class LoginError(Exception):
    """The username/password did not match an active account."""


def validate_signup(first_name, last_name, email, username, password, confirm_password):
    """Check the form values (no database access).

    Returns ``(clean_values_dict, errors_dict)``. The values are trimmed (and the
    email lower-cased); ``errors`` is empty when everything is acceptable.
    """
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    email = (email or "").strip().lower()
    username = (username or "").strip()
    password = password or ""
    confirm_password = confirm_password or ""

    errors = {}
    if not first_name:
        errors["first_name"] = "Please enter your first name."
    elif len(first_name) > NAME_MAX:
        errors["first_name"] = f"First name must be at most {NAME_MAX} characters."
    if not last_name:
        errors["last_name"] = "Please enter your last name."
    elif len(last_name) > NAME_MAX:
        errors["last_name"] = f"Last name must be at most {NAME_MAX} characters."

    if not email:
        errors["email"] = "Please enter your email address."
    elif len(email) > EMAIL_MAX or not _EMAIL_PATTERN.match(email):
        errors["email"] = "Please enter a valid email address."

    if not _USERNAME_PATTERN.match(username):
        errors["username"] = "Username must be 3-30 characters: letters, numbers and underscores only."

    if len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
        errors["password"] = f"Password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters."
    elif not (re.search(r"[A-Za-z]", password) and re.search(r"[0-9]", password)):
        errors["password"] = "Password must contain at least one letter and one number."
    elif password.lower() == username.lower():
        errors["password"] = "Password must not be the same as your username."
    if "password" not in errors and password != confirm_password:
        errors["confirm_password"] = "The two passwords do not match."

    clean = {"first_name": first_name, "last_name": last_name, "email": email, "username": username}
    return clean, errors


def register_customer(conn, first_name, last_name, email, username, password, confirm_password, now):
    """Create a customer account and return the new ``user_id``.

    ``now`` is the timezone-aware UTC time injected by the caller; it becomes the
    account-creation timestamp the SAD requires. Raises ``SignupError`` (with
    per-field messages) if anything is wrong, including a username or email that
    is already taken.
    """
    clean, errors = validate_signup(first_name, last_name, email, username, password, confirm_password)

    # Friendly duplicate checks (only for fields that are otherwise well-formed),
    # so the form can show every problem at once. The UNIQUE columns in the
    # database are the real guarantee (see the DuplicateError handling below).
    if "username" not in errors and db.get_user_by_username(conn, clean["username"]) is not None:
        errors["username"] = "That username is already taken."
    if "email" not in errors and db.get_user_by_email(conn, clean["email"]) is not None:
        errors["email"] = "An account with that email already exists."
    if errors:
        raise SignupError(errors)

    stamp = now.isoformat()
    try:
        return db.insert_user(
            conn,
            first_name=clean["first_name"],
            last_name=clean["last_name"],
            email=clean["email"],
            username=clean["username"],
            password_hash=generate_password_hash(password),
            user_type="customer",
            cash_balance=Decimal("0.00"),
            is_active=1,
            created_at=stamp,
            updated_at=stamp,
        )
    except db.DuplicateError:
        # Someone else took the username/email between our check and the insert.
        raise SignupError({"username": "That username or email is already registered."})


def authenticate(conn, username, password):
    """Return the user row for a correct username + password, else raise ``LoginError``."""
    global _dummy_hash
    username = (username or "").strip()
    password = password or ""

    user = db.get_user_by_username(conn, username) if username else None
    if user is None:
        if _dummy_hash is None:
            _dummy_hash = generate_password_hash("not-a-real-password")
        check_password_hash(_dummy_hash, password)  # spend the same time as a real check
        raise LoginError(GENERIC_LOGIN_ERROR)

    if not check_password_hash(user["password_hash"], password) or not user["is_active"]:
        raise LoginError(GENERIC_LOGIN_ERROR)
    return user
