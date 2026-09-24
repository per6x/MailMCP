"""Read-only access to Apple Mail's local Envelope Index.

The schema is private. Validate it on each connection, use ordinary SQLite WAL
locking, and fail explicitly if Mail changes it. Never write to Mail's database.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time
from urllib.parse import unquote, urlsplit


class MailIndexError(Exception):
    """An unavailable or incompatible local Mail index."""


_REQUIRED = {
    "messages": {"sender", "subject", "subject_prefix", "summary", "date_received",
                 "date_sent", "mailbox", "read", "flagged", "deleted", "size"},
    "subjects": {"subject"},
    "addresses": {"address", "comment"},
    "summaries": {"summary"},
    "mailboxes": {"url", "total_count", "unread_count"},
}
_SELECT = """
    SELECT m.ROWID AS id, COALESCE(m.subject_prefix, '') || COALESCE(s.subject, '') AS subject,
           a.address AS sender, a.comment AS sender_name,
           m.date_received, m.date_sent, m.read, m.flagged, m.deleted, m.size,
           m.mailbox AS mailbox_id, b.url AS mailbox_url,
           substr(sn.summary, 1, 500) AS preview
    FROM messages m
    LEFT JOIN subjects s ON s.ROWID = m.subject
    LEFT JOIN addresses a ON a.ROWID = m.sender
    LEFT JOIN summaries sn ON sn.ROWID = m.summary
    JOIN mailboxes b ON b.ROWID = m.mailbox
"""


def _like(text: str) -> str:
    # User input is a literal substring, not SQL or a LIKE pattern.
    return '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _date(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone required')
        return int(parsed.timestamp())
    except (ValueError, OverflowError) as error:
        raise MailIndexError('Dates must be ISO 8601 timestamps with a timezone, e.g. 2026-09-01T00:00:00Z.') from error


def _iso(value: int | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _message(row: sqlite3.Row) -> dict:
    result = dict(row)
    for key in ('read', 'flagged', 'deleted'):
        result[key] = bool(result[key])
    for key in ('date_sent', 'date_received'):
        result[key] = _iso(result[key])
    return result


def _page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 500:
        raise MailIndexError('limit must be between 1 and 500.')
    if isinstance(offset, bool) or not 0 <= offset <= 1_000_000:
        raise MailIndexError('offset must be between 0 and 1000000.')


class MailIndex:
    def __init__(self, path: Path | None = None):
        self.path = path

    def _path(self) -> Path:
        if self.path is not None:
            return self.path.expanduser().resolve()
        root = Path.home() / 'Library' / 'Mail'
        try:
            versions = sorted((p for p in root.iterdir() if p.name.startswith('V') and p.name[1:].isdigit()),
                              key=lambda p: int(p.name[1:]), reverse=True)
            if versions:
                return versions[0] / 'MailData' / 'Envelope Index'
        except OSError as error:
            raise MailIndexError('Cannot read Mail storage. Grant Full Disk Access to the MCP host, then restart it.') from error
        raise MailIndexError('No local Mail index found. Configure Apple Mail and download mail first.')

    @contextmanager
    def _connect(self):
        connection = None
        try:
            connection = sqlite3.connect(self._path().as_uri() + '?mode=ro', uri=True, timeout=3)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            deadline = time.monotonic() + 5
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            for table, required in _REQUIRED.items():
                # Identifiers come only from the fixed schema contract above.
                found = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
                if not required <= found:
                    raise MailIndexError(f'Unsupported Mail index schema: {table} is missing required columns.')
            yield connection
        except sqlite3.Error as error:
            raise MailIndexError('Cannot query Mail index: ' + str(error) +
                                 '. If access is denied, grant Full Disk Access to the MCP host and restart it.') from error
        finally:
            if connection is not None:
                connection.close()

    def mailboxes(self, limit: int = 100, offset: int = 0) -> dict:
        _page(limit, offset)
        with self._connect() as db:
            rows = db.execute('SELECT ROWID AS id, url, total_count, unread_count FROM mailboxes ORDER BY ROWID LIMIT ? OFFSET ?',
                              (limit + 1, offset)).fetchall()
        items = []
        for row in rows[:limit]:
            entry = dict(row)
            entry['name'] = unquote(urlsplit(entry['url']).path.rstrip('/').rsplit('/', 1)[-1])
            items.append(entry)
        return {'mailboxes': items, 'next_offset': offset + limit if len(rows) > limit else None,
                'backend': 'mail_index'}

    def search(self, query: str = '', *, subject: str = '', sender: str = '',
               mailbox_id: int | None = None, read: bool | None = None,
               flagged: bool | None = None, include_deleted: bool = False,
               received_after: str | None = None, received_before: str | None = None,
               limit: int = 50, offset: int = 0) -> dict:
        _page(limit, offset)
        after, before = _date(received_after), _date(received_before)
        if after is not None and before is not None and after >= before:
            raise MailIndexError('received_after must precede received_before.')
        clauses, params = [], []
        if not include_deleted:
            clauses.append('m.deleted = 0')
        if query:
            clauses.append("(s.subject LIKE ? ESCAPE '\\' OR a.address LIKE ? ESCAPE '\\' OR a.comment LIKE ? ESCAPE '\\' OR sn.summary LIKE ? ESCAPE '\\')")
            params.extend([_like(query)] * 4)
        for value, column in ((subject, "COALESCE(m.subject_prefix, '') || COALESCE(s.subject, '')"),
                              (sender, 'a.address')):
            if value:
                clauses.append(f"({column}) LIKE ? ESCAPE '\\'")
                params.append(_like(value))
        for value, column in ((mailbox_id, 'm.mailbox'), (read, 'm.read'), (flagged, 'm.flagged')):
            if value is not None:
                clauses.append(f'{column} = ?')
                params.append(value)
        for value, comparison in ((after, '>='), (before, '<')):
            if value is not None:
                clauses.append(f'm.date_received {comparison} ?')
                params.append(value)
        sql = _SELECT + ' WHERE ' + (' AND '.join(clauses) or '1')
        sql += ' ORDER BY m.date_received DESC, m.ROWID DESC LIMIT ? OFFSET ?'
        with self._connect() as db:
            rows = db.execute(sql, [*params, limit + 1, offset]).fetchall()
        return {'messages': [_message(row) for row in rows[:limit]],
                'next_offset': offset + limit if len(rows) > limit else None,
                'backend': 'mail_index', 'search_scope': 'subject_sender_cached_preview'}

    def get(self, message_id: int) -> dict:
        if isinstance(message_id, bool) or message_id < 1:
            raise MailIndexError('message_id must be a positive integer.')
        with self._connect() as db:
            row = db.execute(_SELECT + ' WHERE m.ROWID = ?', (message_id,)).fetchone()
        if row is None:
            raise MailIndexError('Message not found in the local index; search again after Mail syncs.')
        return _message(row)
