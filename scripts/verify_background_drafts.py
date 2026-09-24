"""Opt-in live check: creates, edits, verifies, and deletes unsent test drafts.

Run: .venv/bin/python scripts/verify_background_drafts.py --run
Uses only Mail's direct Apple Events interface, never GUI scripting.
"""
import argparse
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run', action='store_true', help='Create disposable drafts in the configured Mail account')
if not parser.parse_args().run:
    parser.error('--run is required for the live Mail check')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import tempfile
import time
import uuid
from pathlib import Path
from collections import Counter
import server

original = server._run_script
subject = 'MailMCP background verification ' + str(uuid.uuid4())
body = 'Background draft verification.\nUnicode: åäö.\n'
identities = set()
checks = 0

def run(script, args, deadline, **kw):
    global checks
    result = original(script, args, deadline, **kw)
    if script == server.DRAFT_SCRIPT and args[0] == 'snapshot':
        snap = json.loads(result)
        for item in (snap if isinstance(snap,list) else [snap] if snap else []):
            identities.add(server._draft_uuid(item['source']))
    if script == server.MAIL_SCRIPT or (script == server.COMPOSE_SCRIPT and args[0] != 'close'):
        compose_id = result if script == server.MAIL_SCRIPT else args[1]
        visible = original('function run(a) { return String(Application("Mail").outgoingMessages.byId(Number(a[0])).visible()); }', [compose_id], deadline, javascript=True)
        assert visible == 'false', 'Compose window became visible'
        checks += 1
    return result

server._run_script = run
try:
    with tempfile.TemporaryDirectory(prefix='mailmcp-hidden-') as temp:
        files = []
        expected = Counter()
        for name, data in [('first å.txt', b'First attachment\n'), ('second.txt', b'Second attachment\n')]:
            p = Path(temp) / name
            p.write_bytes(data)
            files.append(str(p))
            expected[server._fingerprint(name, data)] += 1
        print(server.save_email_draft(to=[], subject=subject, body=body, attachments=files), flush=True)
    draft_uuid = next(iter(identities))
    snap = server._wait_for_saved(expected, time.monotonic()+15, draft_uuid=draft_uuid, subject=subject)
    detail = server.read_email_draft(snap['id'])
    assert body.strip() in detail['body'].replace('\r', '\n'), 'Saved body mismatch'
    print('Attachments survived source-file removal; body verified.', flush=True)
    print(server.edit_email_draft(snap['id'], body=body+'Edited in the background.\n'), flush=True)
    new_id = next(i for i in identities if i != draft_uuid)
    snap = server._wait_for_saved(expected, time.monotonic()+15, draft_uuid=new_id, subject=subject)
    detail = server.read_email_draft(snap['id'])
    assert 'Edited in the background.' in detail['body']
    with tempfile.TemporaryDirectory(prefix='mailmcp-replace-') as temp:
        replacement=Path(temp)/'replacement.pdf'
        replacement.write_bytes(b'%PDF-1.4\n% replacement fixture\n%%EOF\n')
        before=set(identities)
        server.edit_email_draft(snap['id'],attachments=[str(replacement)])
        replacement_id=next(iter(identities-before))
        replacement_expected=Counter({server._fingerprint(replacement.name,replacement.read_bytes()):1})
    snap=server._wait_for_saved(replacement_expected,time.monotonic()+15,draft_uuid=replacement_id,subject=subject)
    before=set(identities)
    server.edit_email_draft(snap['id'],attachments=[])
    cleared_id=next(iter(identities-before))
    snap=server._wait_for_saved(Counter(),time.monotonic()+15,draft_uuid=cleared_id,subject=subject)
    assert 'Edited in the background.' in server.read_email_draft(snap['id'])['body']
    print(json.dumps({'hidden_compose_checks':checks, 'create_edit_replace_clear_attachments':'passed'}), flush=True)
finally:
    for identity in identities:
        snap = json.loads(original(server.DRAFT_SCRIPT, ['snapshot', '', '', identity, '', 'candidates'], time.monotonic()+15, javascript=True))
        for item in (snap if isinstance(snap,list) else [snap] if snap else []):
            server._draft_operation('delete', item['id'])
    print('Disposable saved verification drafts cleaned up.', flush=True)
