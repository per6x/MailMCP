"""Real stdio protocol checks; invalid requests never reach Mail."""

import json
from pathlib import Path
import sys
import unittest

from mcp import Client, StdioServerParameters


class StdioProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def check_client(self, mode):
        server=StdioServerParameters(command=sys.executable,
                                     args=[str(Path(__file__).with_name('server.py'))])
        async with Client(server,mode=mode,read_timeout_seconds=10) as client:
            self.assertEqual(client.protocol_version, '2025-11-25' if mode=='legacy' else '2026-07-28')
            listed=await client.list_tools()
            tools={t.name:t for t in listed.tools}
            for name in ('search_emails','list_accounts','read_email','save_email_draft',
                         'save_email_attachment','update_email','copy_email','move_email','delete_email',
                         'send_email','create_reply_draft','create_forward_draft','create_signature','update_mail_rule'):
                self.assertIn(name,tools)
                self.assertIsNotNone(tools[name].output_schema)
            self.assertTrue(tools['read_email'].annotations.read_only_hint)
            self.assertTrue(tools['delete_email'].annotations.destructive_hint)
            self.assertFalse(tools['save_email_draft'].annotations.idempotent_hint)
            self.assertNotIn('delete_mailbox',tools)
            self.assertTrue(tools['send_email'].annotations.idempotent_hint)
            self.assertTrue(tools['send_email'].annotations.destructive_hint)
            for name,arguments in [('read_email',{'message_id':-1}),
                                   ('update_email',{'message_id':1,'flag_index':7}),
                                   ('move_email',{'message_id':1,'destination':{'account_id':'local','path':[]}}),
                                   ('search_emails',{'limit':0}),
                                   ('create_reply_draft',{'message_id':-1,'body':'test'}),
                                   ('send_email',{'request_id':'test-invalid','to':[],'subject':'test','body':'test'})]:
                result=await client.call_tool(name,arguments)
                self.assertTrue(result.is_error)
            resources=await client.list_resources()
            self.assertIn('mail://capabilities',[str(r.uri) for r in resources.resources])
            resource=await client.read_resource('mail://capabilities')
            capabilities=json.loads(resource.contents[0].text)
            self.assertFalse(capabilities['gui_automation'])
            self.assertTrue(capabilities['capabilities']['send'])
            self.assertTrue(capabilities['capabilities']['reply_forward_drafts'])
            self.assertFalse(capabilities['capabilities']['mailbox_delete'])
            prompt=await client.get_prompt('review_mail',{'focus':'unread messages'})
            self.assertIn('unread messages',prompt.messages[0].content.text)

    async def test_current_protocol(self):
        await self.check_client('auto')

    async def test_legacy_protocol(self):
        await self.check_client('legacy')


if __name__=='__main__': unittest.main()
