"""Native bridge boundary and file-safety regressions. No live Mail access."""
from pathlib import Path
from email.message import EmailMessage
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from mcp.server.mcpserver.exceptions import ToolError

import mail_api as api
import mail_tools as tools


class NativeBridgeTests(unittest.TestCase):
    def test_mime_fallback_handles_unicode_and_ambiguous_duplicate_names(self):
        message=EmailMessage()
        message.set_content('body')
        message.add_attachment(b'one',maintype='text',subtype='plain',filename='å.txt')
        message.add_attachment(b'two',maintype='application',subtype='pdf',filename='same')
        message.add_attachment(b'three',maintype='text',subtype='plain',filename='same')
        attachments=[{'name':'a\u030a.txt','mime_type':None},{'name':'same','mime_type':None}]
        api._fill_mime_types(attachments,message.as_string())
        self.assertEqual(attachments[0]['mime_type'],'text/plain')
        self.assertIsNone(attachments[1]['mime_type'])

    def test_arguments_are_data_and_script_is_fixed(self):
        malicious='"; Application("Finder").quit(); //'
        response=subprocess.CompletedProcess([],0,'{"ok":true}','')
        with patch.object(api.sys,'platform','darwin'), patch.object(api.subprocess,'run',return_value=response) as run:
            self.assertEqual(api.call('create_mailbox',name=malicious),{'ok':True})
        args=run.call_args.args[0]
        self.assertEqual(json.loads(args[-1])['name'],malicious)
        self.assertEqual(run.call_args.kwargs['input'],api.SCRIPT)
        self.assertNotIn('shell',run.call_args.kwargs)

    def test_timeout_is_not_retried_and_reports_uncertain_outcome(self):
        with patch.object(api.sys,'platform','darwin'), patch.object(api.subprocess,'run',side_effect=subprocess.TimeoutExpired('osascript',60)) as run:
            with self.assertRaisesRegex(api.MailAPIError,'may already have applied'):
                api.call('move')
            self.assertEqual(run.call_count,1)

    def test_automation_denial_and_malformed_output(self):
        for response,expected in [(subprocess.CompletedProcess([],1,'','Not authorized (-1743)'),'Automation'),
                                  (subprocess.CompletedProcess([],0,'garbage',''),'invalid response'),
                                  (subprocess.CompletedProcess([],0,'[]',''),'unexpected response')]:
            with self.subTest(expected=expected), patch.object(api.sys,'platform','darwin'), patch.object(api.subprocess,'run',return_value=response):
                with self.assertRaisesRegex(api.MailAPIError,expected):
                    api.call('accounts')

    def test_scope_and_empty_updates_fail_before_any_apple_event(self):
        box=api.MailboxRef(account_id='local',path=['A'])
        with patch.object(api,'call') as call:
            for fn in (lambda:tools.read_email(0),
                       lambda:tools.read_email(1,box,'inbox'),
                       lambda:tools.update_email(1),
                       lambda:tools.update_email(1,flag_index=7),
                       lambda:tools.create_mailbox('A/B'),
                       lambda:tools.list_account_mailboxes(limit=501)):
                with self.assertRaises(ToolError): fn()
            call.assert_not_called()

    def test_import_rejects_non_mail_export_before_calling_mail(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(api,'call',return_value={'requested':True}) as call:
            path=Path(directory)/'Export.mbox';path.mkdir()
            for content in (None,b'',b'not an mbox'):
                if content is not None:(path/'mbox').write_bytes(content)
                with self.assertRaises(ToolError):tools.import_mailbox(str(path))
            call.assert_not_called()
            (path/'mbox').write_bytes(b'From author@example.invalid Fri Sep 25 00:00:00 2026\nSubject: test\n\nBody\n')
            self.assertTrue(tools.import_mailbox(str(path))['requested'])
            self.assertEqual(call.call_args.kwargs['source_path'],str(path.resolve()))

    def test_mailbox_refs_preserve_component_boundaries(self):
        box=api.MailboxRef(account_id='account',path=['Parent','Name/WithSlash'])
        self.assertEqual(box.path,['Parent','Name/WithSlash'])
        for value in ({'account_id':'','path':['Inbox']},
                      {'account_id':'local','path':[]},
                      {'account_id':'local','path':['Inbox'],'unexpected':True}):
            with self.assertRaises(ValidationError): api.MailboxRef(**value)


class AttachmentExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.destination=self.root/'export.bin'
        self.data=b'original bytes\x00\xff'

    def save(self,op,**request):
        self.assertEqual(op,'export_attachment')
        Path(request['path']).write_bytes(self.data)
        return {'id':'a','name':'../../unsafe-name.bin'}

    def export(self,overwrite=False):
        return api.export_attachment(1,'a',str(self.destination),None,'drafts',overwrite)

    def test_export_uses_exact_destination_and_cleans_staging(self):
        with patch.object(api,'call',side_effect=self.save): result=self.export()
        self.assertEqual(self.destination.read_bytes(),self.data)
        self.assertEqual(result['size'],len(self.data))
        self.assertEqual(set(self.root.iterdir()),{self.destination})
        self.assertEqual(self.destination.stat().st_mode & 0o777,0o600)

    def test_existing_file_and_symlink_are_not_followed_or_clobbered(self):
        other=self.root/'other'
        other.write_bytes(b'keep')
        self.destination.symlink_to(other)
        with patch.object(api,'call',side_effect=self.save) as call:
            with self.assertRaisesRegex(api.MailAPIError,'already exists'): self.export()
            call.assert_not_called()
            self.export(overwrite=True)
        self.assertEqual(other.read_bytes(),b'keep')
        self.assertFalse(self.destination.is_symlink())
        self.assertEqual(self.destination.read_bytes(),self.data)

    def test_racing_destination_creation_is_preserved(self):
        def race(op,**request):
            self.destination.write_bytes(b'created concurrently')
            return self.save(op,**request)
        with patch.object(api,'call',side_effect=race):
            with self.assertRaises(api.MailAPIError): self.export()
        self.assertEqual(self.destination.read_bytes(),b'created concurrently')
        self.assertEqual(set(self.root.iterdir()),{self.destination})

    def test_missing_export_does_not_replace_existing_file(self):
        self.destination.write_bytes(b'keep')
        with patch.object(api,'call',return_value={}):
            with self.assertRaisesRegex(api.MailAPIError,'did not create'): self.export(overwrite=True)
        self.assertEqual(self.destination.read_bytes(),b'keep')
        self.assertEqual(set(self.root.iterdir()),{self.destination})


if __name__=='__main__': unittest.main()
