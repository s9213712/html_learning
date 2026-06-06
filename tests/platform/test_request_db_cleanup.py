import sqlite3

from flask import Flask

from services.server.database import close_request_db_connections, get_db


def test_request_db_cleanup_rolls_back_and_closes_leaked_connection(tmp_path):
    db_path = tmp_path / "request-cleanup.db"
    setup = get_db(db_path)
    try:
        setup.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        setup.commit()
    finally:
        setup.close()

    app = Flask(__name__)
    with app.test_request_context("/leak"):
        leaked = get_db(db_path)
        leaked.execute("INSERT INTO sample (value) VALUES ('leaked')")
        close_request_db_connections()

    verifier = sqlite3.connect(db_path)
    try:
        rows = verifier.execute("SELECT value FROM sample").fetchall()
        assert rows == []
        verifier.execute("INSERT INTO sample (value) VALUES ('ok')")
        verifier.commit()
        assert verifier.execute("SELECT value FROM sample").fetchone()[0] == "ok"
    finally:
        verifier.close()


def test_request_db_cleanup_tolerates_route_owned_close(tmp_path):
    db_path = tmp_path / "request-cleanup-double-close.db"
    app = Flask(__name__)
    with app.test_request_context("/closed"):
        conn = get_db(db_path)
        conn.close()
        close_request_db_connections()
