"""Service layer package.

Business rules live here (market.py, trading.py). Services take plain arguments
(user_id, stock_id, quantity, now) and return results or raise typed
exceptions. They never import flask.request or flask.session, so they can be
unit-tested without a web server.
"""
