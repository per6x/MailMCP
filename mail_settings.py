"""Signature and message-rule management through Mail's declared Apple Events."""
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

import mail_api as api
from mail_api import HighlightColor, MailboxRef
from mail_tools import Limit, Offset, tool_errors


class RuleCondition(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    field: Literal['account','any recipient','cc header','matches every message','from header',
                   'header key','message content','message is junk mail','sender is in my contacts',
                   'sender is in my previous recipients','sender is member of group',
                   'sender is not in my contacts','sender is not in my previous recipients',
                   'sender is not member of group','sender is VIP','subject header','to header',
                   'to or cc header','attachment type']
    qualifier: Literal['begins with value','does contain value','does not contain value',
                       'ends with value','equal to value','less than value','greater than value','none']='does contain value'
    expression: str=''
    header: str=''


class RuleActions(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    mark_read: bool | None=None
    flag_index: Annotated[int,Field(ge=-1,le=6)] | None=None
    delete_message: bool | None=None
    color: HighlightColor | None=None
    highlight_text: bool | None=None
    move_to: MailboxRef | None=None
    copy_to: MailboxRef | None=None
    forward_to: str | None=None
    forward_text: str | None=None
    redirect_to: str | None=None
    reply_text: str | None=None
    play_sound: str | None=None
    stop_evaluating: bool | None=None


def _name(name: str) -> None:
    if not name.strip() or len(name)>256 or any(c in name for c in ('\x00','\r','\n')):
        raise api.MailAPIError('Name must be 1..256 characters without NUL or newlines.')


def _conditions(conditions: list[RuleCondition]) -> list[dict]:
    if not 1<=len(conditions)<=50:
        raise api.MailAPIError('Provide 1..50 rule conditions.')
    for condition in conditions:
        if condition.field=='header key' and not condition.header.strip():
            raise api.MailAPIError('A header-key condition requires a header name.')
    return [c.model_dump() for c in conditions]


def _actions(actions: RuleActions) -> dict:
    value=actions.model_dump(exclude_unset=True)
    # Null only has clear semantics for mailbox destinations. For Boolean/string
    # actions, false/-1/empty string are the explicit disabling values.
    if any(v is None and k not in ('move_to','copy_to') for k,v in value.items()):
        raise api.MailAPIError('Only move_to/copy_to accept null; use false, -1, or empty text to disable other actions.')
    return value


@tool_errors
def list_signatures(limit: Limit=100,offset: Offset=0) -> dict[str,Any]:
    """List Mail signature names and readable content. No signature is applied to a message."""
    api.page(limit,offset)
    return api.call('signature_list',limit=limit,offset=offset)


@tool_errors
def create_signature(name: str,content: str) -> dict[str,Any]:
    """Create a named plain-text signature. Does not change account defaults or send mail."""
    _name(name)
    return api.call('signature_create',name=name,content=content)


@tool_errors
def update_signature(name: str,new_name: str | None=None,content: str | None=None) -> dict[str,Any]:
    """Update one uniquely named signature; omitted fields are preserved. Content replacement is plain text."""
    _name(name)
    if new_name is not None: _name(new_name)
    if new_name is None and content is None: raise api.MailAPIError('Provide a new name or content.')
    return api.call('signature_update',name=name,new_name=new_name,content=content)


@tool_errors
def delete_signature(name: str) -> dict[str,Any]:
    """Delete one uniquely named signature. This can affect accounts using that signature."""
    _name(name)
    return api.call('signature_delete',name=name)


@tool_errors
def list_mail_rules(limit: Limit=100,offset: Offset=0) -> dict[str,Any]:
    """List Mail rule names, enabled states, and match-all mode in evaluation order."""
    api.page(limit,offset)
    return api.call('rule_list',limit=limit,offset=offset)


@tool_errors
def read_mail_rule(name: str) -> dict[str,Any]:
    """Read conditions and actions of one uniquely named rule without executing it."""
    _name(name)
    return api.call('rule_read',name=name)


@tool_errors
def create_mail_rule(name: str,conditions: list[RuleCondition],actions: RuleActions,
                     match_all: bool=True,enabled: bool=False) -> dict[str,Any]:
    """Create a Mail rule, disabled by default, and return its saved conditions/actions.

    Enabling a rule lets Mail apply it to incoming messages. Forward/redirect/reply
    actions can send mail automatically; enable those only on an explicit user
    instruction to automate sending. New rules are appended in evaluation order.
    If configuration fails, any partial rule is left disabled and named in the error.
    """
    _name(name)
    return api.call('rule_create',name=name,conditions=_conditions(conditions),actions=_actions(actions),
                    match_all=match_all,enabled=enabled)


@tool_errors
def update_mail_rule(name: str,new_name: str | None=None,conditions: list[RuleCondition] | None=None,
                     actions: RuleActions | None=None,match_all: bool | None=None,
                     enabled: bool | None=None) -> dict[str,Any]:
    """Edit a uniquely named Mail rule; omitted fields/actions are preserved.

    conditions edits existing conditions and may append more. Shortening the list
    is unsupported by this Mail version and fails before changes. An explicit null move_to/copy_to clears
    that action. The rule is temporarily disabled while changing it; failure leaves
    it disabled. Its previous enabled state is restored only after success unless
    enabled is supplied. Enabling forwarding/replies can transmit future mail.
    """
    _name(name)
    if new_name is not None: _name(new_name)
    if all(v is None for v in (new_name,conditions,actions,match_all,enabled)):
        raise api.MailAPIError('Provide at least one rule field to change.')
    return api.call('rule_update',name=name,new_name=new_name,
                    conditions=_conditions(conditions) if conditions is not None else None,
                    actions=_actions(actions) if actions is not None else None,
                    match_all=match_all,enabled=enabled)


@tool_errors
def delete_mail_rule(name: str) -> dict[str,Any]:
    """Delete one uniquely named Mail rule; messages already affected by it are unchanged."""
    _name(name)
    return api.call('rule_delete',name=name)


def register(mcp):
    for fn in (list_signatures,list_mail_rules,read_mail_rule):
        mcp.tool(annotations=ToolAnnotations(read_only_hint=True,open_world_hint=False))(fn)
    mcp.tool(annotations=ToolAnnotations(read_only_hint=False,destructive_hint=False,idempotent_hint=False))(create_signature)
    for fn in (update_signature,delete_signature,create_mail_rule,update_mail_rule,delete_mail_rule):
        mcp.tool(annotations=ToolAnnotations(read_only_hint=False,destructive_hint=True,idempotent_hint=False,open_world_hint=True))(fn)
