// Direct Apple Events only. Arguments are JSON data, never executable source.
function run(argv) {
    const p = JSON.parse(argv[0]);
    const mail = Application('com.apple.mail');
    const systemBoxes = {inbox:'inbox', drafts:'draftsMailbox', sent:'sentMailbox',
                         junk:'junkMailbox', trash:'trashMailbox', outbox:'outbox'};
    function fail(message) { throw new Error(message); }
    function account(id) {
        const a = mail.accounts.whose({id:id})();
        if (a.length !== 1) fail('Account not found. List accounts again.');
        return a[0];
    }
    function owner(id) { return id === 'local' ? mail : account(id); }
    function box(ref) {
        let current = owner(ref.account_id);
        for (const name of ref.path) {
            const found = current.mailboxes.whose({name:name})();
            if (found.length !== 1) fail('Mailbox path not found or ambiguous. List account mailboxes again.');
            current = found[0];
        }
        return current;
    }
    function parent(ref) {
        return ref.path.length === 1 ? owner(ref.account_id)
            : box({account_id:ref.account_id, path:ref.path.slice(0, -1)});
    }
    function descriptor(b, accountID, path) {
        return {account_id:accountID, path:path, name:b.name(), unread_count:b.unreadCount(),
                message_count:b.messages.length};
    }
    function walk(collection, accountID, path, visit) {
        for (const b of collection()) {
            const childPath = path.concat([b.name()]);
            if (visit(b, accountID, childPath)) return true;
            if (walk(b.mailboxes, accountID, childPath, visit)) return true;
        }
        return false;
    }
    function locate(id, ref, system) {
        let found = null;
        function test(b, accountID, path) {
            const rows = b.messages.whose({id:id})();
            if (rows.length > 1) fail('Message ID is ambiguous.');
            if (rows.length) {
                found = {message:rows[0], box:b, mailbox:accountID === null ? null : {account_id:accountID, path:path}};
                return true;
            }
            return false;
        }
        if (ref) test(box(ref), ref.account_id, ref.path);
        else if (system) {
            if (!systemBoxes[system]) fail('Unknown system mailbox.');
            test(mail[systemBoxes[system]](), null, []);
        } else {
            walk(mail.mailboxes, 'local', [], test);
            if (!found) for (const a of mail.accounts()) {
                if (walk(a.mailboxes, a.id(), [], test)) break;
            }
        }
        if (!found) fail('Message not found in this scope. Search again; IDs may change after saves or moves.');
        return found;
    }
    function recipients(collection) {
        return collection().map(r => ({name:r.name(), address:r.address()}));
    }
    function attachment(a) {
        // Some Mail builds expose this property but reject its getter (-10000).
        // Python can recover it from the saved MIME; never invent a content type.
        let type=null;
        try { type=a.mimeType(); } catch (_) {}
        return {id:a.id(), name:a.name(), mime_type:type, size:a.fileSize(), downloaded:a.downloaded()};
    }
    function state(m) {
        return {id:m.id(), message_id:m.messageId(), read:m.readStatus(), flagged:m.flaggedStatus(),
                flag_index:m.flagIndex(), junk:m.junkMailStatus(), deleted:m.deletedStatus(),
                background_color:String(m.backgroundColor())};
    }
    function unique(collection,name,kind) {
        const matches=collection.whose({name:name})() || [];
        if (matches.length!==1) fail(kind+' name not found or ambiguous. List names again.');
        return matches[0];
    }
    function signature(s) { return {name:s.name(),content:s.content()}; }
    const ruleFields={mark_read:'markRead',delete_message:'deleteMessage',color:'colorMessage',
        highlight_text:'highlightTextUsingColor',forward_to:'forwardMessage',forward_text:'forwardText',
        redirect_to:'redirectMessage',reply_text:'replyText',play_sound:'playSound',stop_evaluating:'stopEvaluatingRules'};
    function ruleMailbox(b) {
        let id='local';
        try { id=b.account().id(); } catch (_) {}
        const path=[b.name()];
        let current=b;
        for(let i=0;i<29;i++) {
            try {
                current=current.container();
                const name=current.name();
                if(!name) break;
                path.unshift(name);
            } catch(_) { break; }
        }
        return {account_id:id,path:path};
    }
    function readRule(r) {
        const actions={},unavailable=[];
        for(const key of Object.keys(ruleFields)) {
            try {actions[key]=r[ruleFields[key]]();} catch(_){actions[key]=null;unavailable.push(key);}
        }
        actions.flag_index=r.markFlagged()?r.markFlagIndex():-1;
        actions.move_to=r.shouldMoveMessage()?ruleMailbox(r.moveMessage()):null;
        actions.copy_to=r.shouldCopyMessage()?ruleMailbox(r.copyMessage()):null;
        return {name:r.name(),enabled:r.enabled(),match_all:r.allConditionsMustBeMet(),actions:actions,
                unavailable_action_fields:unavailable,
                conditions:(r.ruleConditions() || []).map(c=>({field:String(c.ruleType()),qualifier:String(c.qualifier()),
                                                        expression:c.expression(),header:c.header()}))};
    }
    function configureRule(r) {
        if(p.conditions!==null && p.conditions.length<(r.ruleConditions()||[]).length)
            fail('Mail cannot remove individual rule conditions on this version. Create a replacement disabled rule and review it before deleting the original.');
        let stage="read enabled";
        const wasEnabled=r.enabled();
        r.enabled=false;
        try {
            if(p.conditions!==null) {
                stage="update conditions";
                const old=r.ruleConditions() || [];
                for(let i=0;i<p.conditions.length;i++) {
                    const c=p.conditions[i];
                    if(i<old.length) {
                        const item=r.ruleConditions.at(i);
                        item.ruleType=c.field; item.qualifier=c.qualifier;
                        item.expression=c.expression; item.header=c.header;
                    } else r.ruleConditions.push(mail.RuleCondition({
                        ruleType:c.field,qualifier:c.qualifier,expression:c.expression,header:c.header}));
                }
            }
            if(p.actions!==null) for(const key of Object.keys(p.actions)) {
                stage="action "+key;
                const value=p.actions[key];
                if(key==='move_to'||key==='copy_to') {
                    const enabled=key==='move_to'?'shouldMoveMessage':'shouldCopyMessage';
                    const destination=key==='move_to'?'moveMessage':'copyMessage';
                    if(value!==null) r[destination]=box(value);
                    r[enabled]=value!==null;
                } else if(key==='flag_index') {
                    r.markFlagIndex=value; r.markFlagged=value>=0;
                } else if(ruleFields[key]) r[ruleFields[key]]=value;
                else fail('Unknown rule action.');
            }
            stage="match mode";
            if(p.match_all!==null) r.allConditionsMustBeMet=p.match_all;
            stage="rename";
            if(p.new_name) { r.name=p.new_name; r=unique(mail.rules,p.new_name,'Rule'); }
            stage="restore enabled";
            r.enabled=p.enabled===null?wasEnabled:p.enabled;
            stage="readback";
            return readRule(r);
        } catch(e) {
            try {r.enabled=false;} catch(_) {}
            fail('Rule configuration failed; the partial rule is retained disabled (stage: '+stage+'). '+String(e));
        }
    }
    function read(m, ref) {
        const body = m.content();
        const result = Object.assign(state(m), {mailbox:ref, subject:m.subject(), sender:m.sender(),
            reply_to:m.replyTo(), to:recipients(m.toRecipients), cc:recipients(m.ccRecipients),
            bcc:recipients(m.bccRecipients), body:body.slice(0,p.max_body_chars),
            body_truncated:body.length > p.max_body_chars,
            date_received:m.dateReceived(), date_sent:m.dateSent(), size:m.messageSize(),
            attachments:m.mailAttachments().map(attachment)});
        if (p.include_headers) result.headers = m.allHeaders();
        if (p.include_source) {
            const source = m.source();
            if (source.length > p.max_source_chars) fail('MIME source exceeds max_source_chars; raise the limit or export an attachment separately.');
            result.source = source;
        }
        return result;
    }
    function output(value) {
        if (['read','attachments','export_attachment'].indexOf(p.operation)>=0) {
            const attachments=value.attachments || [value];
            if (attachments.some(a=>!a.mime_type)) {
                const source=value.source || m.source();
                if (source.length<=50000000) value._mime_source=source;
            }
        }
        return JSON.stringify(value);
    }
    if (p.operation === 'status') return output({version:mail.version(), running:mail.running(), backend:'apple_events'});
    if (p.operation === 'accounts') return output({accounts:mail.accounts().map(a => ({
        id:a.id(), name:a.name(), type:String(a.accountType()), enabled:a.enabled(),
        email_addresses:a.emailAddresses(), full_name:a.fullName(), server_name:a.serverName()
    }))});
    if (p.operation === 'mailboxes') {
        const items=[];
        const ids = p.account_id ? [p.account_id] : ['local'].concat(mail.accounts().map(a=>a.id()));
        let count=0;
        for (const id of ids) {
            const stopped=walk(owner(id).mailboxes, id, [], (b,a,path) => {
                if (count++ >= p.offset) items.push(descriptor(b,a,path));
                return items.length > p.limit;
            });
            if (stopped) break;
        }
        return output({mailboxes:items.slice(0,p.limit),next_offset:items.length>p.limit ? p.offset+p.limit : null});
    }
    if (p.operation === 'check_mail') {
        if (p.account_id) mail.checkForNewMail({for:account(p.account_id)});
        else mail.checkForNewMail();
        return output({requested:true, completed:false});
    }
    if (p.operation === 'synchronize') {
        mail.synchronize({with:account(p.account_id)});
        return output({requested:true, completed:false});
    }
    if(p.operation==='import_mailbox') {
        mail.importMailMailbox({at:Path(p.source_path)});
        return output({requested:true,completed:false,source_path:p.source_path,destination_account_id:'local'});
    }
    if (p.operation === 'create_mailbox') {
        const container = p.parent ? box(p.parent) : owner(p.account_id);
        if (container.mailboxes.whose({name:p.name})().length) fail('A mailbox with this name already exists.');
        container.mailboxes.push(mail.Mailbox({name:p.name}));
        const ref={account_id:p.parent ? p.parent.account_id : p.account_id,
                   path:(p.parent ? p.parent.path : []).concat([p.name])};
        return output({mailbox:descriptor(box(ref),ref.account_id,ref.path)});
    }
    if (p.operation === 'rename_mailbox') {
        const b=box(p.mailbox), container=parent(p.mailbox);
        if (p.name !== b.name() && container.mailboxes.whose({name:p.name})().length) fail('A mailbox with this name already exists.');
        b.name=p.name;
        const ref={account_id:p.mailbox.account_id, path:p.mailbox.path.slice(0,-1).concat([p.name])};
        return output({mailbox:descriptor(box(ref),ref.account_id,ref.path)});
    }
    if(p.operation==='signature_list') {
        const rows=(mail.signatures() || []).slice(p.offset,p.offset+p.limit+1);
        return output({signatures:rows.slice(0,p.limit).map(signature),next_offset:rows.length>p.limit?p.offset+p.limit:null});
    }
    if(p.operation==='signature_create') {
        if(mail.signatures.whose({name:p.name})().length) fail('Signature name already exists.');
        mail.signatures.push(mail.Signature({name:p.name,content:p.content}));
        return output(signature(unique(mail.signatures,p.name,'Signature')));
    }
    if(p.operation==='signature_update'||p.operation==='signature_delete') {
        const s=unique(mail.signatures,p.name,'Signature');
        if(p.operation==='signature_delete') {
            mail.delete(s);
            return output({deleted:mail.signatures.whose({name:p.name})().length===0});
        }
        if(p.new_name && p.new_name!==p.name && mail.signatures.whose({name:p.new_name})().length) fail('Signature name already exists.');
        if(p.content!==null) s.content=p.content;
        if(p.new_name!==null) s.name=p.new_name;
        return output(signature(unique(mail.signatures,p.new_name||p.name,'Signature')));
    }
    if(p.operation==='rule_list') {
        const rows=(mail.rules() || []).slice(p.offset,p.offset+p.limit+1);
        return output({rules:rows.slice(0,p.limit).map(r=>({name:r.name(),enabled:r.enabled(),match_all:r.allConditionsMustBeMet()})),
                       next_offset:rows.length>p.limit?p.offset+p.limit:null});
    }
    if(p.operation==='rule_read') return output(readRule(unique(mail.rules,p.name,'Rule')));
    if(p.operation==='rule_create') {
        if(mail.rules.whose({name:p.name})().length) fail('Rule name already exists.');
        mail.rules.push(mail.Rule({name:p.name,enabled:false}));
        return output(configureRule(unique(mail.rules,p.name,'Rule')));
    }
    if(p.operation==='rule_update'||p.operation==='rule_delete') {
        const r=unique(mail.rules,p.name,'Rule');
        if(p.operation==='rule_delete') {
            r.enabled=false;
            mail.delete(r);
            return output({deleted:mail.rules.whose({name:p.name})().length===0});
        }
        if(p.new_name && p.new_name!==p.name && mail.rules.whose({name:p.new_name})().length) fail('Rule name already exists.');
        return output(configureRule(r));
    }
    if(p.operation==='compose_recipients') {
        const outgoing=mail.outgoingMessages.byId(p.compose_id);
        if(outgoing.visible()) fail('Compose object is visible; close the user-edited draft before changing recipients.');
        for(const kind of ['to','cc','bcc']) {
            const collection=outgoing[kind+'Recipients'];
            const old=collection() || [];
            for(let i=old.length-1;i>=0;i--) mail.delete(old[i]);
            const constructor={to:'ToRecipient',cc:'CcRecipient',bcc:'BccRecipient'}[kind];
            for(const address of p[kind]) collection.push(mail[constructor]({address:address}));
        }
        mail.save(outgoing);
        return output({saved:true});
    }
    const found=locate(p.message_id,p.mailbox,p.system_mailbox), m=found.message;
    if(p.operation==='compose_response') {
        let outgoing;
        if(p.mode==='reply') outgoing=mail.reply(m,{openingWindow:false,replyToAll:p.reply_all});
        else if(p.mode==='forward') outgoing=mail.forward(m,{openingWindow:false});
        else if(p.mode==='redirect') outgoing=mail.redirect(m,{openingWindow:false});
        else fail('Unknown response composition mode.');
        outgoing.visible=false;
        const subject=outgoing.subject();
        outgoing.subject=p.marker;
        mail.save(outgoing);
        return output({compose_id:outgoing.id(),subject:subject,original_message_id:m.messageId()});
    }
    if (p.operation === 'read') return output(read(m,found.mailbox));
    if (p.operation === 'state') return output(state(m));
    if (p.operation === 'attachments') return output({id:m.id(),attachments:m.mailAttachments().map(attachment)});
    if (p.operation === 'export_attachment') {
        const matches=m.mailAttachments.whose({id:p.attachment_id})();
        if (matches.length !== 1) fail('Attachment not found. List attachments again.');
        const a=matches[0];
        if (!a.downloaded()) fail('Attachment is not cached. Synchronize this account and retry after Mail downloads it.');
        mail.save(a,{in:Path(p.path)});
        return output(attachment(a));
    }
    if (p.operation === 'update') {
        // Validate in Python first; only these named, documented properties are writable.
        if (p.read !== null) m.readStatus=p.read;
        if (p.flag_index !== null) m.flagIndex=p.flag_index;
        if (p.junk !== null) m.junkMailStatus=p.junk;
        if (p.background_color !== null) m.backgroundColor=p.background_color;
        return output(state(m));
    }
    if (p.operation === 'move' || p.operation === 'copy') {
        const destination=box(p.destination), rfcID=m.messageId(), oldID=m.id();
        if (!rfcID) fail('Message lacks a Message-ID; cannot verify relocation safely.');
        const before=destination.messages.whose({messageId:rfcID})().map(x=>x.id());
        if (p.operation === 'move' && before.indexOf(oldID)>=0)
            return output({status:'already_in_destination',id:oldID,message_id:rfcID,destination:p.destination});
        if (p.operation === 'move') mail.move(m,{to:destination});
        else mail.duplicate(m,{to:destination});
        const after=destination.messages.whose({messageId:rfcID})().filter(x=>before.indexOf(x.id())<0);
        return output({status:after.length===1?'verified':'requested',id:after.length===1?after[0].id():null,
                       message_id:rfcID,destination:p.destination,previous_id:oldID});
    }
    if (p.operation === 'delete') {
        const oldID=m.id();
        let commandError=null;
        try { mail.delete(m); } catch(e) { commandError=String(e); }
        // Resolve the mailbox afresh after deletion: the previously captured
        // collection can raise -1728 once Mail invalidates its message objects.
        let deleted;
        try {
            const freshBox=found.mailbox ? box(found.mailbox) : mail[systemBoxes[p.system_mailbox]]();
            const ids=freshBox.messages.id();
            deleted=ids.indexOf(oldID)<0 || freshBox.messages.whose({id:oldID})()[0].deletedStatus();
        } catch(e) {
            return output({requested:true,deleted:null,previous_id:oldID,
                           verification_error:String(e),command_error:commandError});
        }
        if (!deleted) fail('Mail accepted delete but the message is still present and not marked deleted. Inspect state before retrying.');
        return output({deleted:true,previous_id:oldID});
    }
    fail('Unknown Mail operation.');
}
