"""Validation shared by commands, the inbox and the OneBot adapter."""

import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

EVENTS = frozenset((
    'activity.published', 'history.updated', 'member.added', 'event.pending',
    'event.published', 'event.updated', 'event.approved', 'event.rejected',
    'event.status', 'event.deleted', 'event.settled', 'event.result_set',
    'event.signup', 'event.signup_cancelled', 'reminder.deadline', 'reminder.start',
))
LABELS = {
    'activity.published': '新文章', 'history.updated': '群史更新',
    'member.added': '新成员', 'event.pending': '活动待审',
    'event.published': '活动发布', 'event.updated': '活动更新',
    'event.approved': '活动通过审核', 'event.rejected': '活动被驳回',
    'event.status': '活动状态变更', 'event.deleted': '活动删除',
    'event.settled': '活动结算', 'event.result_set': '活动赛果更新',
    'event.signup': '新增报名', 'event.signup_cancelled': '取消报名',
    'reminder.deadline': '报名截止提醒', 'reminder.start': '活动开始提醒',
}


def qq(value):
    value = str(value)
    if not re.fullmatch(r'[1-9]\d{4,11}', value):
        raise ValueError('QQ/群号须为 5–12 位非零开头的数字。')
    return value


def uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('需要有效的 UUID。') from None


def positive(value, maximum=1000000):
    if not re.fullmatch(r'[1-9]\d*', str(value)) or int(value) > maximum:
        raise ValueError(f'需要 1–{maximum} 之间的整数。')
    return int(value)


def origin(value):
    parts = urlsplit(value)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in ('', '/')):
        raise ValueError('网站地址须为 http(s) origin，不含路径、账号或查询参数。')
    return f'{parts.scheme}://{parts.netloc}'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_envelope(payload, credential_id, groups, legacy_groups):
    if not isinstance(payload, dict) or payload.get('source') != 'guatswr_web':
        raise ValueError('无效的通知来源。')
    name, data, target = payload.get('event'), payload.get('data'), payload.get('target')
    if name not in EVENTS | {'members.sync.request', 'members.profile_required'} or not isinstance(data, dict):
        raise ValueError('无效的事件或数据。')
    if target is None:
        if name not in {'activity.published', 'history.updated', 'member.added'} or not legacy_groups:
            raise ValueError('未配置旧通知的默认群。')
    else:
        if not isinstance(target, dict) or target.get('bot_id') != credential_id:
            raise ValueError('目标凭据不匹配。')
        if name == 'members.sync.request':
            if payload.get('kind') != 'command' or target.get('type') != 'service':
                raise ValueError('无效的采集命令。')
            uuid(data.get('request_id'))
            if qq(data.get('group_id')) not in groups:
                raise ValueError('采集群未授权。')
        elif name == 'members.profile_required':
            if payload.get('kind') != 'notification' or target.get('type') != 'private':
                raise ValueError('无效的私信通知。')
            qq(target.get('qq'))
        elif target.get('type', 'group') != 'group' or qq(target.get('group_id')) not in groups:
            raise ValueError('通知群未授权。')
    if name.startswith('members.') and not payload.get('delivery_id'):
        raise ValueError('同步与私信通知必须带 delivery_id。')
    # Legacy notifications have no delivery UUID. Exact-envelope hashes are a
    # compatibility fallback; durable platform deliveries always use delivery_id.
    return uuid(payload['delivery_id']) if payload.get('delivery_id') else 'legacy:' + digest(payload)


def normalize_members(rows, group_id, maximum=10000):
    if not isinstance(rows, list) or len(rows) > maximum:
        raise ValueError('成员列表不完整或超过上限。')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('成员列表格式错误。')
        identifier = qq(row.get('user_id'))
        if identifier in seen or str(row.get('group_id', group_id)) != group_id:
            raise ValueError('成员列表存在重复 QQ 或群号不符。')
        seen.add(identifier)
        role = row.get('role', 'member')
        if role not in ('owner', 'admin', 'member'):
            raise ValueError('成员角色无效。')
        result.append({'qq': identifier, 'group_card': str(row.get('card') or '').strip()[:50],
                       'nickname': str(row.get('nickname') or '').strip()[:50], 'group_role': role})
    return sorted(result, key=lambda row: int(row['qq']))


def notification_text(payload, site_url):
    name, data = payload['event'], payload['data']
    if name == 'members.profile_required':
        lines = [f"网站成员资料待完善：{data.get('count', 0)} 人"]
        for row in data.get('members', [])[:50]:
            lines.append(f"{str(row.get('name', ''))[:100]}（QQ {row.get('qq', '')}）")
        if data.get('truncated'):
            lines.append('完整名单请到网站后台查看。')
        lines.append(site_url + '/admin/bot?section=profiles')
        return '\n'.join(lines)
    lines = ['【桂航绯想天】' + LABELS[name]]
    # Explicit fields avoid forwarding actor, internal fields or arbitrary JSON.
    for key, label in [('title', '标题'), ('name', '成员'), ('date', '日期'),
                       ('status', '状态'), ('common_id', '报名 ID'), ('description', '简介')]:
        if data.get(key) is not None:
            lines.append(f'{label}：{str(data[key])[:600]}')
    event_id = data.get('eventId') or data.get('event_id')
    if event_id:
        lines.append(f'活动编号：{event_id}')
        positive(event_id)
        lines.append(site_url + '/events')
    elif name == 'activity.published':
        lines.append(site_url + '/activity/' + str(positive(data['id'])))
    elif name == 'history.updated':
        lines.append(site_url + '/history')
    else:
        lines.append(site_url)
    return '\n'.join(lines)
