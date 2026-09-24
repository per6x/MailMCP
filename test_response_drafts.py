"""Response-specific invariants at the shared durable send boundary; no Mail calls."""
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError
from outbound import Outbound, SendJournal
from response_drafts import send_response, verify_redirect_headers
import server


def prepared():
    return {'id':7,'compose_id':22,'to':['recipient@example.invalid'],'cc':[],'bcc':[],
            'subject':'Re: test','sender':'sender@example.invalid','sent':False,
            'original_message_id':'original@example.invalid'}


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transmit=Mock(return_value=True)
        self.service=Outbound(Mock(),self.transmit,SendJournal(Path(self.temp.name)/'journal'))
        self.backend=SimpleNamespace(outbound=self.service)

    def test_reply_replay_does_not_need_original_to_still_exist(self):
        with patch('response_drafts.create_response',return_value=prepared()) as create:
            result=send_response(self.backend,'reply-1','reply',10,'Answer',reply_all=True,system_mailbox='inbox')
            self.assertEqual(result['status'],'accepted_by_mail')
            self.assertFalse(create.call_args.kwargs['close'])
            self.assertTrue(create.call_args.kwargs['reply_all'])
            create.side_effect=ToolError('Original was moved')
            self.assertTrue(send_response(self.backend,'reply-1','reply',10,'Answer',reply_all=True,system_mailbox='inbox')['replayed'])
            self.assertEqual(create.call_count,1)
            self.transmit.assert_called_once()
            self.assertNotIn('sent',result['draft'])
            self.assertNotIn('compose_id',result['draft'])

    def test_failed_threading_check_cannot_transmit(self):
        with patch('response_drafts.create_response',side_effect=ToolError('Reply threading mismatch')):
            with self.assertRaisesRegex(ToolError,'No send was attempted'):
                send_response(self.backend,'reply-2','reply',10,'Answer')
        self.transmit.assert_not_called()
        self.assertEqual(self.service.journal.get('reply-2')['status'],'failed_before_send')

    def test_empty_native_reply_envelope_cannot_transmit(self):
        with patch('response_drafts.create_response',return_value={**prepared(),'to':[]}):
            with self.assertRaisesRegex(ToolError,'At least one'):
                send_response(self.backend,'reply-3','reply',10,'Answer')
        self.transmit.assert_not_called()

    def test_forward_snapshots_attachments_and_binds_recipient_changes(self):
        file=Path(self.temp.name)/'file.bin';file.write_bytes(b'original')
        staged=[]
        def create(*a,**kw):
            self.assertEqual(kw['to'],['recipient@example.invalid'])
            staged.extend(Path(p) for p in kw['attachments'])
            file.write_bytes(b'modified')
            self.assertEqual(staged[0].read_bytes(),b'original')
            return prepared()
        with patch('response_drafts.create_response',side_effect=create):
            send_response(self.backend,'forward-1','forward',10,'Intro',to=['recipient@example.invalid'],attachments=[str(file)])
        self.assertFalse(staged[0].exists())
        with self.assertRaisesRegex(ToolError,'different email content'):
            send_response(self.backend,'forward-1','forward',10,'Intro',to=['other@example.invalid'],attachments=[str(file)])
        self.transmit.assert_called_once()

    def test_forward_invalid_address_is_rejected_before_preparation(self):
        with patch('response_drafts.create_response') as create:
            with self.assertRaises(ToolError):
                send_response(self.backend,'forward-2','forward',10,'',to=['bad\nBcc: other@example.invalid'])
            create.assert_not_called()
        self.assertFalse((Path(self.temp.name)/'journal').exists())

    def test_redirect_checks_original_routing_and_resent_recipients_separately(self):
        original=EmailMessage()
        original['From']='author@example.invalid';original['To']='original@example.invalid'
        original['Reply-To']='reply@example.invalid';original['Message-ID']='<original@example.invalid>'
        original.set_content('Original body')
        redirected=EmailMessage()
        for key,value in original.items():redirected[key]=value
        redirected.set_payload(original.get_payload())
        redirected['Resent-From']='resender@example.invalid'
        redirected['Resent-To']='new@example.invalid';redirected['Resent-Date']='Fri, 25 Sep 2026 00:00:00 +0000'
        verify_redirect_headers(server,original.as_string(),redirected.as_string())
        fields={'body':'Original body','to':['new@example.invalid'],'recipient_header_prefix':'Resent-'}
        self.assertTrue(server._fields_match(redirected.as_string(),fields))
        self.assertFalse(server._fields_match(redirected.as_string(),{**fields,'to':['original@example.invalid']}))
        redirected.replace_header('Reply-To','changed@example.invalid')
        with self.assertRaisesRegex(ToolError,'Reply-To'):
            verify_redirect_headers(server,original.as_string(),redirected.as_string())


if __name__=='__main__':unittest.main()
