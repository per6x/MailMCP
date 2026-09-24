"""No SMTP or Apple Events: exercise the actual durable send state machine."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError
from outbound import Outbound, SendJournal


class OutboundTests(unittest.TestCase):
    def test_status_lookup_does_not_create_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'absent'
            with self.assertRaisesRegex(ToolError,'No send journal'):
                SendJournal(path).get('unknown')
            self.assertFalse(path.exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.journal = SendJournal(self.directory / 'state')
        self.create = Mock(return_value={'id': 7, 'compose_id': 20, 'draft_uuid': 'test-uuid'})
        self.transmit = Mock(return_value=True)
        self.service = Outbound(self.create, self.transmit, self.journal)

    def send(self, **kw):
        request = dict(request_id='request-1', to=['User <user@example.invalid>'], subject='Test', body='Body')
        request.update(kw)
        return self.service.send(**request)

    def test_success_and_retry_after_restart_send_exactly_once(self):
        result = self.send()
        self.assertEqual(result['status'], 'accepted_by_mail')
        self.assertFalse(result['delivery_confirmed'])
        self.assertNotIn('compose_id', result['draft'])
        self.service = Outbound(self.create, self.transmit, SendJournal(self.directory/'state'))
        self.assertTrue(self.send()['replayed'])
        self.assertEqual(self.create.call_count, 1)
        self.assertEqual(self.transmit.call_count, 1)
        self.assertFalse(self.create.call_args.kwargs['close'])

    def test_timeout_or_rejection_is_unknown_and_never_retried(self):
        self.transmit.side_effect = TimeoutError('uncertain native send')
        self.assertEqual(self.send()['status'], 'unknown')
        self.assertEqual(self.send()['status'], 'unknown')
        self.assertEqual(self.transmit.call_count, 1)
        self.transmit.side_effect = None
        self.transmit.return_value = False
        self.assertEqual(self.send(request_id='request-2')['status'], 'unknown')

    def test_process_interruption_keeps_inflight_and_does_not_repeat(self):
        self.transmit.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.send()
        self.assertEqual(self.journal.get('request-1')['status'], 'in_flight')
        self.service = Outbound(self.create, self.transmit, SendJournal(self.directory/'state'))
        self.assertEqual(self.send()['status'], 'in_flight')
        self.assertEqual(self.transmit.call_count, 1)

    def test_request_id_cannot_be_reused_for_different_content(self):
        self.send()
        with self.assertRaisesRegex(ToolError, 'different email content'):
            self.send(body='Different body')
        self.assertEqual(self.transmit.call_count, 1)

    def test_failed_draft_is_recorded_and_never_sent(self):
        self.create.side_effect = ToolError('Saved MIME does not match')
        with self.assertRaisesRegex(ToolError, 'No send was attempted'): self.send()
        self.assertEqual(self.send()['status'], 'failed_before_send')
        self.assertEqual(self.create.call_count, 1)
        self.transmit.assert_not_called()

    def test_failed_journal_commit_prevents_the_send_event(self):
        with patch.object(self.journal, 'transition', side_effect=ToolError('disk failure')):
            with self.assertRaisesRegex(ToolError, 'disk failure'): self.send()
        self.transmit.assert_not_called()
        self.assertEqual(self.journal.get('request-1')['status'], 'preparing')

    def test_staged_attachments_are_immutable_and_duplicate_names_are_preserved(self):
        sources=[]
        for index, content in enumerate((b'one', b'two')):
            path=self.directory/str(index)/'same.txt'
            path.parent.mkdir(); path.write_bytes(content); sources.append(path)
        staged=[]
        def prepare(**kw):
            sources[0].write_bytes(b'changed after snapshot')
            staged.extend(Path(p) for p in kw['attachments'])
            self.assertEqual([p.name for p in staged], ['same.txt', 'same.txt'])
            self.assertEqual([p.read_bytes() for p in staged], [b'one', b'two'])
            return {'id':7, 'compose_id':20}
        self.create.side_effect=prepare
        self.send(attachments=[str(p) for p in sources])
        self.assertTrue(all(not p.exists() for p in staged))
        with self.assertRaisesRegex(ToolError,'different email content'):
            self.send(attachments=[str(p) for p in sources])
        self.assertEqual(self.transmit.call_count,1)

    def test_invalid_requests_never_create_a_draft_or_journal(self):
        for arguments in ({'to':[]}, {'to':['user@example.invalid\nBcc: victim@example.invalid']},
                          {'subject':'hello\r\nInjected: yes'}, {'to':['one@example.invalid,two@example.invalid']},
                          {'to':['user@']}, {'request_id':'../not-a-key'}):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError): self.send(**arguments)
        self.create.assert_not_called()
        self.transmit.assert_not_called()
        self.assertFalse((self.directory/'state').exists())

    def test_existing_reservation_wins_without_starting_a_second_draft(self):
        import hashlib, json
        request=dict(to=['User <user@example.invalid>'],cc=[],bcc=[],subject='Test',body='Body',sender='',attachments=[])
        fingerprint=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
        self.journal.reserve('request-1',fingerprint)
        self.assertEqual(self.send()['status'],'preparing')
        self.create.assert_not_called()
        self.transmit.assert_not_called()


if __name__=='__main__': unittest.main()
