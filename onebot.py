"""OneBot v11 only: no guessing between multiple QQ bot instances."""

import asyncio
import base64

from .protocol import normalize_members, qq


class OneBot:
    def __init__(self, context, platform_id):
        self.context, self.platform_id = context, platform_id

    async def call(self, action, **kwargs):
        platform = self.context.get_platform_inst(self.platform_id)
        if platform is None or platform.meta().name != 'aiocqhttp':
            raise RuntimeError('配置的 OneBot v11 平台尚未连接。')
        result = await asyncio.wait_for(platform.get_client().call_action(action, **kwargs), 30)
        # aiocqhttp normally unwraps data and raises on failure. Also tolerate
        # transports returning the raw OneBot envelope, checking its retcode.
        if isinstance(result, dict) and 'retcode' in result:
            if result['retcode'] != 0 or result.get('status', 'ok') != 'ok':
                raise RuntimeError('OneBot 操作失败。')
            return result.get('data')
        return result

    async def send(self, target_type, identifier, text=None, image=None):
        message = []
        if text is not None:
            message.append({'type': 'text', 'data': {'text': text}})
        if image is not None:
            message.append({'type': 'image', 'data': {'file': 'base64://' + base64.b64encode(image).decode()}})
        if target_type not in ('group', 'private'):
            raise ValueError('无效的消息目标。')
        key = 'group_id' if target_type == 'group' else 'user_id'
        result = await self.call('send_group_msg' if target_type == 'group' else 'send_private_msg',
                                 **{key: int(qq(identifier)), 'message': message, 'auto_escape': True})
        if not isinstance(result, dict) or result.get('message_id') is None:
            raise RuntimeError('OneBot 未返回消息发送确认。')

    async def members(self, group_id, maximum):
        group_id = qq(group_id)
        before = await self.call('get_group_info', group_id=int(group_id), no_cache=True)
        rows = await self.call('get_group_member_list', group_id=int(group_id), no_cache=True)
        after = await self.call('get_group_info', group_id=int(group_id), no_cache=True)
        members = normalize_members(rows, group_id, maximum)
        # Fail closed when a gateway omits a total or returns a partial list.
        for info in (before, after):
            if (not isinstance(info, dict) or type(info.get('member_count')) is not int
                    or info['member_count'] != len(members)):
                raise ValueError('群人数与完整成员列表不一致，请重新采集。')
        return members
