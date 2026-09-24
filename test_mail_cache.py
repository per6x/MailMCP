"""Real SQLite/emlx fixtures, including detached MIME and stale identities. No Apple Events."""
from email.message import EmailMessage
from contextlib import closing
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mail_cache import MailCache
from mail_index import MailIndex,MailIndexError

ACCOUNT='11111111-2222-4333-8444-555555555555'


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'V10'
        self.db=self.root/'MailData'/'Envelope Index';self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript('''CREATE TABLE messages(sender,subject,subject_prefix,summary,date_received,date_sent,mailbox,read,flagged,deleted,size,remote_id);
            CREATE TABLE subjects(subject); CREATE TABLE addresses(address,comment);
            CREATE TABLE summaries(summary); CREATE TABLE mailboxes(url,total_count,unread_count);
            INSERT INTO subjects VALUES('Cached test'); INSERT INTO addresses VALUES('author@example.invalid','Author');
            INSERT INTO messages VALUES(1,1,'',NULL,100,99,1,0,0,0,500,'remote-1');''')
            db.execute('INSERT INTO mailboxes VALUES(?,1,1)',('ews://'+ACCOUNT+'/Inbox',));db.commit()
        self.folder=self.root/ACCOUNT/'Inbox.mbox'/'internal-id'/'Data'/'0'/'Messages'
        self.folder.mkdir(parents=True)
        self.path=self.folder/'1.emlx'
        self.message=EmailMessage();self.message['From']='Author <author@example.invalid>'
        self.message['To']='reader@example.invalid';self.message['Subject']='Cached test'
        self.message['Message-ID']='<cached@example.invalid>'
        self.message.set_content('Downloaded body.\nSecond line.')
        self.message.add_attachment(b'\x00\xffbinary',maintype='application',subtype='octet-stream',filename='file.bin')
        self.save()
        self.cache=MailCache(MailIndex(self.db))

    def save(self,trailer=None):
        raw=self.message.as_bytes()
        self.path.write_bytes(f'{len(raw):<10}'.encode()+b'\n'+raw+plistlib.dumps(trailer or {'remote-id':'remote-1'}))

    def test_complete_read_and_truncation_do_not_mark_read_or_launch_mail(self):
        with patch('subprocess.run',side_effect=AssertionError('Mail must never launch')):
            result=self.cache.read(1,max_body_chars=10)
        self.assertTrue(result['source_complete']);self.assertTrue(result['body_complete'])
        self.assertTrue(result['body_truncated']);self.assertEqual(result['body'],'Downloaded')
        self.assertEqual(result['attachments'][0]['size'],8)
        self.assertTrue(result['attachments'][0]['cached_in_mime'])
        self.assertEqual(result['message_id'],'cached@example.invalid')
        with closing(sqlite3.connect(self.db)) as db:self.assertEqual(db.execute('SELECT read FROM messages').fetchone()[0],0)

    def test_detached_attachment_is_not_reported_as_empty_complete_file(self):
        part=self.message.get_payload()[1];part['X-Apple-Content-Length']='128';part.set_payload('')
        self.path.unlink();self.path=self.folder/'1.partial.emlx';self.save()
        result=self.cache.read(1)
        self.assertFalse(result['source_complete']);self.assertTrue(result['body_complete'])
        self.assertFalse(result['attachments'][0]['cached_in_mime'])
        self.assertIsNone(result['attachments'][0]['sha256']);self.assertIsNone(result['attachments'][0]['size'])

    def test_missing_body_and_corrupt_length_are_explicit(self):
        body=self.message.get_payload()[0];body['X-Apple-Content-Length']='100';body.set_payload('')
        self.save();self.assertFalse(self.cache.read(1)['body_complete'])
        self.path.write_bytes(b'10000\nnot enough data')
        with self.assertRaisesRegex(MailIndexError,'truncated'):self.cache.read(1)
        self.path.write_bytes(b'not a length\n')
        with self.assertRaisesRegex(MailIndexError,'length header'):self.cache.read(1)

    def test_remote_id_sender_and_subject_must_match_index(self):
        self.save({'remote-id':'different'})
        with self.assertRaisesRegex(MailIndexError,'remote identity'):self.cache.read(1)
        self.message.replace_header('Subject','Other subject');self.save()
        with self.assertRaisesRegex(MailIndexError,'subject'):self.cache.read(1)
        self.message.replace_header('Subject','Cached test');self.message.replace_header('From','other@example.invalid');self.save()
        with self.assertRaisesRegex(MailIndexError,'sender'):self.cache.read(1)

    def test_ambiguous_cache_and_symlinks_fail_closed(self):
        other=self.folder/'1.partial.emlx';other.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(MailIndexError,'Multiple'):self.cache.read(1)
        other.unlink()
        outside=Path(self.temp.name)/'outside';outside.write_bytes(self.path.read_bytes())
        self.path.unlink();self.path.symlink_to(outside)
        with self.assertRaises(MailIndexError):self.cache.read(1)

    def test_path_traversal_and_missing_message_fail_explicitly(self):
        for url in ('ews://'+ACCOUNT+'/%2E%2E','ews://'+ACCOUNT+'/Inbox%2F../../outside','file:///etc/passwd'):
            with self.subTest(url=url),self.assertRaises(MailIndexError):self.cache._mailbox_path(url)
        self.path.unlink()
        with self.assertRaisesRegex(MailIndexError,'not cached'):self.cache.read(1)

    def test_reindex_during_read_is_detected(self):
        original=self.cache._load
        def change(*args):
            result=original(*args)
            with closing(sqlite3.connect(self.db)) as db:db.execute("UPDATE messages SET remote_id='reindexed'");db.commit()
            return result
        with patch.object(self.cache,'_load',side_effect=change):
            with self.assertRaisesRegex(MailIndexError,'identity changed'):self.cache.read(1)

    def test_size_limit_rejects_before_decoding(self):
        with self.assertRaisesRegex(MailIndexError,'byte limit'):self.cache.read(1,max_mime_bytes=10)


if __name__=='__main__':unittest.main()
