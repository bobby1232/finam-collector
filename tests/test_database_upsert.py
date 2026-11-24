from app import db as db_module


class FakeCursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def executemany(self, sql, rows):
        self.calls.append((sql, rows))


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def test_upsert_deduplicates_response_and_keeps_latest_value(monkeypatch):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr(db_module.psycopg, "connect", lambda _url: connection)
    database = db_module.Database("postgresql://test")

    written = database.upsert_bars(
        "SBER@MISX",
        "TIME_FRAME_M1",
        [
            {
                "timestamp": "2026-11-24T10:00:00Z",
                "open": "1",
                "high": "2",
                "low": "1",
                "close": "1.5",
                "volume": "10",
            },
            {
                "timestamp": "2026-11-24T10:00:00Z",
                "open": "1",
                "high": "3",
                "low": "1",
                "close": "2.5",
                "volume": "20",
            },
        ],
    )

    assert written == 1
    assert connection.committed
    sql, rows = cursor.calls[0]
    assert "ON CONFLICT (ticker, timeframe, ts)" in sql
    assert len(rows) == 1
    assert str(rows[0][6]) == "2.5"
    assert "PRIMARY KEY (ticker, timeframe, ts)" in db_module.SCHEMA_SQL
