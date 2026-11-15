"""On-this-day formatting and durable daily group broadcasts (Beijing time)."""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from .protocol import qq

DAILY_EVENT = 'plugin.on_this_day'
BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')


def beijing_now():
    return datetime.now(BEIJING)


async def on_this_day(api, config, params, *, skip_empty=False):
    result = await api.request('GET', '/v1/on-this-day', params=params)
    if skip_empty and not result['items']:
        return '', []
    output = result['broadcast_text']
    if result.get('has_more'):
        output += f"\n还有更多记录，请查询第 {result['page'] + 1} 页。"
    images = result.get('broadcast_images', [])
    cap = int(config.get('max_broadcast_images', 6))
    if len(images) > cap:
        output += f'\n本次配图最多 {cap} 张，完整记录请查看网站。'
    return output, images[:cap]


class DailyBroadcast:
    def __init__(self, config, api, inbox, bot, allowed_groups, credential_id, logger):
        self.api, self.inbox, self.bot, self.logger = api, inbox, bot, logger
        self.config, self.credential_id = config, credential_id
        self.platform_id = config.get('platform_id', '')
        self.enabled = config.get('daily_broadcast_enabled', False)
        self.groups = frozenset()
        self.hour = self.minute = 0
        if not self.enabled:
            return
        clock = config.get('daily_broadcast_time', '09:00')
        if not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock):
            raise ValueError('daily_broadcast_time 须为北京时间 HH:MM，例如 09:00。')
        self.hour, self.minute = map(int, clock.split(':'))
        groups = config.get('daily_broadcast_groups', [])
        if not isinstance(groups, list) or not groups:
            raise ValueError('开启每日播报时，daily_broadcast_groups 至少配置一个目标群。')
        self.groups = frozenset(map(qq, groups))
        if not self.groups <= allowed_groups:
            raise ValueError('每日播报目标群必须属于 allowed_groups。')

    def enqueue(self, now=None):
        if not self.enabled:
            return 0
        now = (now or beijing_now()).astimezone(BEIJING)
        if (now.hour, now.minute) < (self.hour, self.minute):
            return 0
        day, created = now.date().isoformat(), 0
        for group in sorted(self.groups):
            # The time is deliberately excluded: editing it or reloading the
            # plugin must not produce a second broadcast for a group that day.
            identifier = str(uuid5(NAMESPACE_URL,
                f'guatswr:daily:{self.platform_id}:{self.credential_id}:{day}:{group}'))
            payload = {'source': 'guatswr_plugin', 'event': DAILY_EVENT,
                       'data': {'date': day},
                       'target': {'bot_id': self.credential_id, 'platform_id': self.platform_id,
                                  'group_id': group}}
            created += self.inbox.put(identifier, payload) or self.inbox.release_deferred(identifier)
        return created

    async def run(self, wake):
        while True:
            try:
                if self.enqueue():
                    wake.set()
            except Exception as error:
                self.logger.warning('GuatSWR daily scheduler failed (%s)', type(error).__name__)
            await asyncio.sleep(30)

    async def process(self, job):
        payload, checkpoint = job['payload'], job['checkpoint']
        target, day = payload['target'], payload['data']['date']
        if payload.get('source') != 'guatswr_plugin':
            raise ValueError('无效的本地播报来源。')
        # Reloads can disable the feature, revoke a group or replace the bot.
        # Old-day jobs are discarded rather than broadcasting stale history.
        if (not self.enabled or target.get('bot_id') != self.credential_id
                or target.get('platform_id') != self.platform_id
                or target.get('group_id') not in self.groups
                or day != beijing_now().date().isoformat()):
            return
        now = beijing_now()
        scheduled = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if now < scheduled:
            # Honour a later time selected while an old task was still queued.
            self.inbox.defer(job['id'], scheduled.timestamp())
            return False
        if 'broadcast' not in checkpoint:
            text, images = await on_this_day(self.api, self.config,
                {'date': day, 'source': 'all', 'page': 1, 'limit': 10}, skip_empty=True)
            checkpoint['broadcast'] = {'text': text, 'images': images}
            self.inbox.checkpoint(job['id'], checkpoint)
        broadcast = checkpoint['broadcast']
        parts = [broadcast['text'][i:i + 3000] for i in range(0, len(broadcast['text']), 3000)]
        for index in range(checkpoint.get('text_done', 0), len(parts)):
            if day != beijing_now().date().isoformat():
                return
            await self.bot.send('group', target['group_id'], text=parts[index])
            checkpoint['text_done'] = index + 1
            self.inbox.checkpoint(job['id'], checkpoint)
        for index in range(checkpoint.get('images_done', 0), len(broadcast['images'])):
            if day != beijing_now().date().isoformat():
                return
            try:
                raw = await self.api.download(broadcast['images'][index])
            except Exception:
                self.logger.info('GuatSWR 跳过一张无法下载或解码的每日播报图片。')
            else:
                if day != beijing_now().date().isoformat():
                    return
                await self.bot.send('group', target['group_id'], image=raw)
            checkpoint['images_done'] = index + 1
            self.inbox.checkpoint(job['id'], checkpoint)
