"""Opening a brand-new database from several threads at once must not fail.

Seen as a flaky test: the web server and a scan thread both found no
`subscriptions` table and both tried to create it ("table subscriptions
already exists")."""
import threading

from clawback import tracker


def test_new_database_opened_by_many_threads_at_once(tmp_path):
    for round_ in range(5):
        path = tmp_path / f"db{round_}.sqlite3"
        start = threading.Barrier(16)
        errors = []

        def open_it():
            start.wait()
            try:
                tracker.connect(path).close()
            except Exception as exc:  # noqa: BLE001 - any error fails the test
                errors.append(repr(exc))

        threads = [threading.Thread(target=open_it) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == []
        db = tracker.connect(path)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        db.close()
        assert {"subscriptions", "scans", "senders", "messages"} <= names
