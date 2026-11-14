"""Command dispatch independent of AstrBot, so identity checks can be tested."""

import json
import re
import shlex
from datetime import date

from .broadcast import on_this_day
from .protocol import EVENTS, positive, qq, uuid

HELP = '''桂航绯想天网站助手
/桂航 活动 [页码] [类型] ｜ /桂航 详情 活动ID
/桂航 文章 [页码] ｜ /桂航 成员 [页码] ｜ /桂航 群史 [页码]
/桂航 今天 [YYYY-MM-DD] [all|articles|history] [页码]
以下指令请私聊：
/桂航 绑定 网站生成的绑定码
/桂航 报名 活动ID "常用ID" /uploads/已有头像 "机签" "来源"
/桂航 我的报名 活动ID ｜ /桂航 取消报名 活动ID
/桂航 待审 [页码] ｜ /桂航 审核 活动ID approve|reject "理由"
以下订阅指令在目标群内使用，限 AstrBot 管理员：
/桂航 订阅 事件名 [活动ID或0] [提前分钟]
/桂航 订阅列表 ｜ /桂航 退订 订阅UUID
管理员私聊：/桂航 投递 [页码] ｜ /桂航 重试 投递UUID ｜ /桂航 队列
/桂航 本地失败 [页码] ｜ /桂航 本地重试 投递ID
事件名示例：activity.published、event.approved、reminder.start
含空格的参数用英文双引号包住；报名头像须已上传到网站。'''


def page_args(args):
    return {'page': positive(args[0]) if args else 1, 'limit': 10}


def render_list(result, resource, site):
    items = result.get('items', [])
    lines = [f"第 {result.get('page', 1)} 页" + (f" · 共 {result['total']} 条" if 'total' in result else '')]
    for row in items:
        if resource == 'deliveries':
            lines.append(f"{row['id']} · {row.get('event_name', '')} · {row.get('status', '')}")
        elif resource == 'subscriptions':
            lines.append(f"{row['id']} · {row['event_name']} · 活动 {row.get('event_id') or '全部'} · 提前 {row['lead_minutes']} 分钟")
        elif resource == 'members':
            lines.append(f"#{row['id']} {row.get('name', '')} · {row.get('role', '')} · {row.get('school') or ''}")
        else:
            lines.append(f"#{row['id']} {str(row.get('title', ''))[:200]} · {row.get('date') or row.get('status') or ''}")
            if row.get('description'):
                lines.append(str(row['description'])[:300])
            if resource == 'articles':
                lines.append(site + '/activity/' + str(row['id']))
    if not items:
        lines.append('暂无记录。')
    elif len(items) == 10 and resource != 'subscriptions':
        lines.append('可指定下一页继续查询。')
    return '\n'.join(lines)


class Commands:
    def __init__(self, service):
        self.service, self.api = service, service.api

    async def execute(self, text, *, sender, group, private, admin):
        args = shlex.split(text)
        if not args or args[0] in ('帮助', 'help'):
            return HELP, []
        command, args = args[0], args[1:]
        sender = qq(sender)
        group = qq(group) if group else None
        if group and group not in self.service.groups:
            raise ValueError('此群未在插件 allowed_groups 中授权。')
        private_commands = {'绑定', '报名', '我的报名', '取消报名', '待审', '审核', '投递', '重试', '队列', '本地失败', '本地重试'}
        if command in private_commands and not private:
            raise ValueError('此操作请私聊机器人，避免在群中公开绑定码或个人资料。')
        if command in {'订阅', '订阅列表', '退订', '投递', '重试', '队列', '本地失败', '本地重试'} and not admin:
            raise ValueError('此指令仅允许 AstrBot 管理员使用。')
        if command in {'订阅', '订阅列表', '退订'} and not group:
            raise ValueError('请在要管理订阅的目标群内使用。')
        if command in ('活动', '文章', '成员', '群史'):
            if len(args) > (2 if command == '活动' else 1):
                raise ValueError('参数过多，请查看 /桂航 帮助。')
            resource = {'活动': 'events', '文章': 'articles', '成员': 'members', '群史': 'history'}[command]
            params = page_args(args)
            if len(args) > 1:
                params['type'] = args[1]
            result = await self.api.request('GET', '/v1/' + resource, params=params)
            return render_list(result, resource, self.api.site_url), []
        if command == '详情' and len(args) == 1:
            result = await self.api.request('GET', f'/v1/events/{positive(args[0])}')
            lines = [f"#{result['id']} {result.get('title', '')}", f"类型：{result.get('type', '')} · 状态：{result.get('status', '')}"]
            lines.append(json.dumps(result.get('content', {}), ensure_ascii=False, indent=2)[:10000])
            lines.append(self.api.site_url + '/events')
            return '\n'.join(lines), []
        if command == '今天' and len(args) <= 3:
            params = {'limit': 10}
            if args and re.fullmatch(r'\d{4}-\d{2}-\d{2}', args[0]):
                params['date'] = date.fromisoformat(args.pop(0)).isoformat()
            if args and args[0] in ('all', 'articles', 'history'):
                params['source'] = args.pop(0)
            if args:
                params['page'] = positive(args.pop(0))
            if args:
                raise ValueError('用法：今天 [YYYY-MM-DD] [all|articles|history] [页码]')
            return await on_this_day(self.api, self.service.config, params)
        if command == '绑定' and len(args) == 1:
            if not re.fullmatch(r'[a-f0-9]{32}', args[0]):
                raise ValueError('请从网站取得有效的 32 位绑定码。')
            await self.api.request('POST', '/v1/identity/bind', body={'code': args[0], 'qq': sender})
            return '已绑定当前 QQ 与网站身份。', []
        if command == '报名' and 3 <= len(args) <= 5:
            identifier = positive(args[0])
            if not args[1].strip() or len(args[1]) > 100 or not re.fullmatch(r'/uploads/[^\s]{1,490}', args[2]):
                raise ValueError('请提供常用 ID 和网站上已存在的 /uploads/ 头像路径。')
            await self.api.request('POST', f'/v1/events/{identifier}/signup', body={
                'qq': sender, 'common_id': args[1], 'avatar': args[2],
                'machine_sign': args[3] if len(args) > 3 else '', 'pilot_source': args[4] if len(args) > 4 else '',
            })
            return '报名成功。', []
        if command in ('我的报名', '取消报名') and len(args) == 1:
            result = await self.api.request('GET' if command == '我的报名' else 'DELETE',
                                            f'/v1/events/{positive(args[0])}/signup', params={'qq': sender})
            if command == '取消报名':
                return '已取消报名。', []
            row = result.get('data')
            if not row:
                return '你尚未报名此活动。', []
            # Only display fields relevant to the owner's registration.
            return '\n'.join(f'{key}：{row[key]}' for key in ('common_id', 'machine_sign', 'pilot_source', 'created_at') if key in row), []
        if command == '待审' and len(args) <= 1:
            result = await self.api.request('GET', '/v1/reviews', params={**page_args(args), 'qq': sender})
            return render_list(result, 'reviews', self.api.site_url), []
        if command == '审核' and len(args) == 3:
            if args[1] not in ('approve', 'reject') or not 1 <= len(args[2].strip()) <= 500:
                raise ValueError('审核动作须为 approve/reject，并填写 1–500 字理由。')
            await self.api.request('POST', f'/v1/events/{positive(args[0])}/review',
                                   body={'qq': sender, 'action': args[1], 'reason': args[2]})
            return '已提交审核结果。', []
        if command == '订阅' and 1 <= len(args) <= 3:
            if args[0] not in EVENTS:
                raise ValueError('事件名可选：' + '、'.join(sorted(EVENTS)))
            body = {'group_id': group, 'event': args[0], 'lead_minutes': positive(args[2], 10080) if len(args) > 2 else 60}
            if len(args) > 1 and args[1] != '0':
                body['event_id'] = positive(args[1])
            result = await self.api.request('POST', '/v1/subscriptions', body=body)
            return '订阅已保存：' + result['id'], []
        if command in ('订阅列表', '退订') and len(args) == (0 if command == '订阅列表' else 1):
            result = await self.api.request('GET', '/v1/subscriptions')
            items = [row for row in result['items'] if row['group_id'] == group]
            if command == '订阅列表':
                return render_list({'items': items}, 'subscriptions', self.api.site_url), []
            identifier = uuid(args[0])
            if identifier not in {row['id'] for row in items}:
                raise ValueError('只能退订当前群的订阅。')
            await self.api.request('DELETE', '/v1/subscriptions/' + identifier)
            return '已退订。', []
        if command == '投递' and len(args) <= 1:
            result = await self.api.request('GET', '/v1/deliveries', params=page_args(args))
            return render_list(result, 'deliveries', self.api.site_url), []
        if command == '重试' and len(args) == 1:
            await self.api.request('POST', '/v1/deliveries/' + uuid(args[0]) + '/retry', body={})
            return '网站已重新排队投递。', []
        if command == '队列' and not args:
            return '本地接收队列：' + json.dumps(self.service.inbox.summary(), ensure_ascii=False), []
        if command == '本地失败' and len(args) <= 1:
            rows = self.service.inbox.failed(positive(args[0]) if args else 1)
            return '\n'.join(f"{row['id']} · {row['error']} · 尝试 {row['attempts']} 次" for row in rows) or '暂无本地失败任务。', []
        if command == '本地重试' and len(args) == 1:
            identifier = args[0]
            if not re.fullmatch(r'legacy:[a-f0-9]{64}', identifier):
                identifier = uuid(identifier)
            self.service.inbox.retry_notification(identifier)
            self.service.wake.set()
            return '本地群通知已重新排队。', []
        raise ValueError('未知指令或参数数量不符，请查看 /桂航 帮助。')
