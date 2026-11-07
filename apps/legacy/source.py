"""
Where legacy rows come from: the legacy MySQL database, or (in tests) plain Python lists.

Both give the importer the same thing: for a table name, an iterable of rows as dicts keyed by column name.
"""

import environ


class ListSource:
    """Rows held in memory: {"users": [{...}, ...], ...}. Used by the tests."""

    def __init__(self, tables: dict):
        self.tables = tables

    def rows(self, table: str, order_by: str = "id"):
        return iter(sorted(self.tables.get(table, []), key=lambda row: row.get(order_by) or 0))

    def count(self, table: str) -> int:
        return len(self.tables.get(table, []))


class MySQLSource:
    """The legacy database, read-only. `url` looks like mysql://user:password@127.0.0.1:3306/bbms."""

    def __init__(self, url: str):
        import pymysql  # only needed when importing, so imported here

        config = environ.Env.db_url_config(url)
        self._pymysql = pymysql
        self.connection = pymysql.connect(
            host=config.get("HOST") or "127.0.0.1",
            port=int(config.get("PORT") or 3306),
            user=config["USER"],
            password=config.get("PASSWORD") or "",
            database=config["NAME"],
            charset="utf8mb4",
        )
        with self.connection.cursor() as cursor:
            # Never write to the legacy database, even if the user given is allowed to (use a read-only user anyway).
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            # Times are stored in UTC (the legacy app's timezone); keep them as naive values and let the importer
            # attach UTC. Zero dates ('0000-00-00') come back as strings, which the importer treats as empty.
            cursor.execute("SET time_zone = '+00:00'")

    def rows(self, table: str, order_by: str = "id"):
        # A server-side cursor streams big tables (the access log) instead of loading them all at once.
        with self.connection.cursor(self._pymysql.cursors.SSDictCursor) as cursor:
            cursor.execute(f"SELECT * FROM `{table}` ORDER BY `{order_by}`")
            yield from cursor

    def count(self, table: str) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
            return cursor.fetchone()[0]
