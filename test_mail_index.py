"""SQLite adapter tests use an isolated fixture; never the user's Mail database."""

from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from mail_index import MailIndex, MailIndexError


class MailIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'Envelope ? Index'
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                CREATE TABLE messages (sender INTEGER, subject INTEGER, subject_prefix TEXT,
                  summary INTEGER, date_received INTEGER, date_sent INTEGER, mailbox INTEGER,
                  read INTEGER, flagged INTEGER, deleted INTEGER, size INTEGER);
                CREATE TABLE subjects (subject TEXT);
                CREATE TABLE addresses (address TEXT, comment TEXT);
                CREATE TABLE summaries (summary TEXT);
                CREATE TABLE mailboxes (url TEXT, total_count INTEGER, unread_count INTEGER);
                INSERT INTO mailboxes VALUES ('imap://account/INBOX', 3, 1);
                INSERT INTO mailboxes VALUES ('local:///On%20My%20Mac/Archive', 0, 0);
                INSERT INTO addresses VALUES ('alice@example.invalid', 'Alice');
                INSERT INTO addresses VALUES ('bob@example.invalid', 'Bob');
                INSERT INTO subjects VALUES ('100%_literal');
                INSERT INTO subjects VALUES ('A regular subject');
                INSERT INTO summaries VALUES ('Only in cached preview');
                INSERT INTO messages VALUES (1, 1, 'Re: ', 1, 1720000001, 1720000000, 1, 0, 1, 0, 500);
                INSERT INTO messages VALUES (2, 2, '', NULL, 1720000001, 1720000000, 1, 1, 0, 0, 100);
                INSERT INTO messages VALUES (1, 2, '', NULL, 1720000002, 1720000000, 1, 1, 0, 1, 100);
            ''')
        self.index = MailIndex(self.path)

    def test_pagination_is_ordered_with_id_tiebreaker_and_excludes_deleted(self):
        first = self.index.search(limit=1)
        self.assertEqual([m['id'] for m in first['messages']], [2])
        second = self.index.search(limit=1, offset=first['next_offset'])
        self.assertEqual([m['id'] for m in second['messages']], [1])
        self.assertIsNone(second['next_offset'])
        self.assertEqual(len(self.index.search(include_deleted=True)['messages']), 3)

    def test_search_literal_wildcards_and_sql_injection(self):
        self.assertEqual([m['id'] for m in self.index.search(query='%_')['messages']], [1])
        self.assertEqual(self.index.search(query="' OR 1=1 --")['messages'], [])
        self.assertEqual(self.index.search(query='preview')['messages'][0]['id'], 1)
        self.assertEqual(self.index.search(subject='Re: 100')['messages'][0]['id'], 1)
        self.assertEqual(self.index.search(sender='bob', flagged=True)['messages'], [])
        self.assertEqual(self.index.search(read=False, mailbox_id=1)['messages'][0]['id'], 1)

    def test_dates_are_timezone_aware_and_bounds_are_half_open(self):
        items = self.index.search(received_after='2024-07-03T09:46:41Z',
                                  received_before='2024-07-03T09:46:42Z')['messages']
        self.assertEqual(len(items), 2)
        self.assertEqual(self.index.search(received_before='2024-07-03T09:46:41Z')['messages'], [])
        for value in ('tomorrow', '2024-07-03T09:46:41'):
            with self.assertRaises(MailIndexError):
                self.index.search(received_after=value)

    def test_read_metadata_and_mailboxes(self):
        result = self.index.get(1)
        self.assertFalse(result['read'])
        self.assertTrue(result['flagged'])
        self.assertEqual(result['subject'], 'Re: 100%_literal')
        self.assertEqual(result['date_received'], '2024-07-03T09:46:41+00:00')
        self.assertEqual(self.index.mailboxes()['mailboxes'][1]['name'], 'Archive')
        self.assertEqual(self.index.mailboxes(limit=1)['next_offset'], 1)
        with self.assertRaisesRegex(MailIndexError, 'not found'):
            self.index.get(999)

    def test_connection_cannot_write_and_missing_db_is_not_created(self):
        with self.assertRaisesRegex(MailIndexError, 'readonly'):
            with self.index._connect() as db:
                db.execute('DELETE FROM messages')
        self.assertEqual(len(self.index.search()['messages']), 2)
        missing = Path(self.directory.name) / 'missing'
        with self.assertRaises(MailIndexError):
            MailIndex(missing).search()
        self.assertFalse(missing.exists())

    def test_schema_mismatch_and_bounds_fail_explicitly(self):
        for arguments in ({'limit': 501}, {'limit': 0}, {'offset': -1}):
            with self.assertRaises(MailIndexError):
                self.index.search(**arguments)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE subjects')
            db.commit()
        with self.assertRaisesRegex(MailIndexError, 'Unsupported Mail index schema'):
            self.index.search()


if __name__ == '__main__':
    unittest.main()
