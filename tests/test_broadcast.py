import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from astrbot_plugin_guatswr.broadcast import BEIJING, DAILY_EVENT
from astrbot_plugin_guatswr.client import ApiError
from astrbot_plugin_guatswr.inbox import Inbox
from astrbot_plugin_guatswr.service import Service
from astrbot_plugin_guatswr.tests.test_plugin import CREDENTIAL, GROUP, SENDER, FakeApi, config


def daily_config(**extra):
    return config(platform_id='qq-main', daily_broadcast_enabled=True,
                  daily_broadcast_time='09:00', daily_broadcast_groups=[GROUP], **extra)


class DailyBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.inbox = Inbox(':memory:')
        self.api = FakeApi()
        self.api.results[('GET', '/v1/on-this-day')] = {
            'broadcast_text': '历史上的今天 [CQ:at,qq=all]', 'broadcast_images': [],
            'page': 1, 'has_more': False, 'total': 1, 'items': [{'id': 1}],
        }
        self.api.download = AsyncMock(return_value=b'png-bytes')
        self.bot = type('Bot', (), {'send': AsyncMock(), 'members': AsyncMock()})()
        self.service = self.make_service()
        self.clock = patch('astrbot_plugin_guatswr.broadcast.beijing_now',
                           return_value=datetime(2026, 11, 14, 9, tzinfo=BEIJING))
        self.now = self.clock.start()

    def make_service(self, settings=None, inbox=None):
        return Service(settings or daily_config(), self.api, inbox or self.inbox,
                       self.bot, logging.getLogger('test-daily'))

    async def asyncTearDown(self):
        await self.service.close()
        self.clock.stop()
        self.inbox.close()

    async def test_beijing_boundary_daily_dedup_and_new_day(self):
        # UTC 01:00 is Beijing 09:00, independent of the host timezone.
        self.assertEqual(self.service.daily.enqueue(datetime(2026, 11, 14, 0, 59, tzinfo=timezone.utc)), 0)
        self.assertEqual(self.service.daily.enqueue(datetime(2026, 11, 14, 1, tzinfo=timezone.utc)), 1)
        self.assertEqual(self.service.daily.enqueue(), 0)
        job = self.inbox.take()
        self.assertEqual(job['payload']['data']['date'], '2026-11-14')
        await self.service.process(job)
        self.inbox.done(job['id'])
        self.assertEqual(self.service.daily.enqueue(), 0)
        self.now.return_value = datetime(2026, 11, 15, 9, tzinfo=BEIJING)
        self.assertEqual(self.service.daily.enqueue(), 1)
        self.assertNotEqual(self.inbox.take()['id'], job['id'])
        self.bot.send.assert_awaited_once_with('group', GROUP, text='历史上的今天 [CQ:at,qq=all]')
        self.assertEqual(self.api.calls[0][2]['params'],
                         {'date': '2026-11-14', 'source': 'all', 'page': 1, 'limit': 10})

    async def test_late_start_only_enqueues_current_day(self):
        self.now.return_value = datetime(2026, 11, 14, 23, 59, tzinfo=BEIJING)
        self.assertEqual(self.service.daily.enqueue(), 1)
        self.assertEqual(self.inbox.summary(), {'pending': 1})
        self.assertEqual(self.inbox.take()['payload']['data']['date'], '2026-11-14')

    async def test_config_requires_valid_time_and_authorized_nonempty_groups(self):
        for field, value in [('daily_broadcast_time', '9:00'), ('daily_broadcast_time', '24:00'),
                             ('daily_broadcast_time', '12:60'), ('daily_broadcast_time', None),
                             ('daily_broadcast_groups', []), ('daily_broadcast_groups', GROUP),
                             ('daily_broadcast_groups', [SENDER]), ('daily_broadcast_groups', ['01234'])]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.make_service({**daily_config(), field: value})
        service = self.make_service({**daily_config(), 'daily_broadcast_groups': [GROUP, GROUP]})
        self.assertEqual(service.daily.enqueue(), 1)

    async def test_disabled_scheduler_makes_no_jobs(self):
        service = self.make_service({**daily_config(), 'daily_broadcast_enabled': False,
                                     'daily_broadcast_time': 'invalid', 'daily_broadcast_groups': []})
        self.assertEqual(service.daily.enqueue(), 0)
        self.assertIsNone(self.inbox.take())
        self.assertEqual(self.api.calls, [])
        self.bot.send.assert_not_awaited()

    async def test_reload_revocation_and_stale_jobs_do_not_send(self):
        self.service.daily.enqueue()
        job = self.inbox.take()
        settings = daily_config()
        cases = [
            {**settings, 'daily_broadcast_enabled': False},
            {**settings, 'daily_broadcast_groups': [SENDER], 'allowed_groups': [SENDER]},
            {**settings, 'platform_id': 'another-bot'},
            {**settings, 'credential_id': '00000000-0000-4000-8000-000000000099'},
        ]
        for changed in cases:
            with self.subTest(changed=changed):
                await self.make_service(changed).process(job)
        self.now.return_value = datetime(2026, 11, 15, 8, tzinfo=BEIJING)
        await self.service.process(job)
        self.assertEqual(self.service.daily.enqueue(), 0)
        self.assertEqual(self.api.calls, [])
        self.bot.send.assert_not_awaited()

    async def test_later_time_defers_pending_task_without_spending_retry(self):
        self.service.daily.enqueue()
        job = self.inbox.take()
        service = self.make_service({**daily_config(), 'daily_broadcast_time': '20:30'})
        self.assertIs(await service.process(job), False)
        row = self.inbox.db.execute('SELECT state,due,attempts FROM inbox').fetchone()
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['attempts'], 0)
        self.assertEqual(row['due'], datetime(2026, 11, 14, 20, 30, tzinfo=BEIJING).timestamp())
        self.bot.send.assert_not_awaited()

    async def test_failed_group_does_not_prevent_other_groups(self):
        settings = {**daily_config(), 'allowed_groups': [GROUP, SENDER],
                    'daily_broadcast_groups': [GROUP, SENDER]}
        service = self.make_service(settings)
        self.assertEqual(service.daily.enqueue(), 2)
        first = self.inbox.take()
        self.api.fail_once[('GET', '/v1/on-this-day')] = True
        with self.assertRaises(ApiError):
            await service.process(first)
        self.inbox.fail(first, 'api_503')
        other = self.inbox.take()
        self.assertNotEqual(first['payload']['target']['group_id'], other['payload']['target']['group_id'])
        await service.process(other)
        self.inbox.done(other['id'])
        self.assertEqual(self.inbox.summary(), {'done': 1, 'pending': 1})

    async def test_earlier_time_releases_deferred_job_without_reviving_failures(self):
        self.service.daily.enqueue()
        job = self.inbox.take()
        later = self.make_service({**daily_config(), 'daily_broadcast_time': '20:30'})
        self.assertIs(await later.process(job), False)
        earlier = self.make_service({**daily_config(), 'daily_broadcast_time': '08:00'})
        self.assertEqual(earlier.daily.enqueue(), 1)
        resumed = self.inbox.take()
        self.assertEqual(resumed['id'], job['id'])
        self.inbox.fail(resumed, 'offline', permanent=True)
        self.assertEqual(earlier.daily.enqueue(), 0)
        self.assertIsNone(self.inbox.take())

    async def test_restart_resumes_chunks_images_and_frozen_content(self):
        self.api.results[('GET', '/v1/on-this-day')] = {
            'broadcast_text': 'a' * 3001, 'broadcast_images': ['bad', 'good', 'capped'],
            'page': 1, 'has_more': True, 'total': 11, 'items': [{'id': 1}],
        }
        settings = daily_config(max_broadcast_images=2)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'inbox.sqlite3'
            inbox = Inbox(path)
            try:
                service = self.make_service(settings, inbox)
                service.daily.enqueue()
                job = inbox.take()
                self.bot.send.side_effect = [None, RuntimeError('offline')]
                with self.assertRaises(RuntimeError):
                    await service.process(job)
                inbox.close()
                inbox = Inbox(path)
                service = self.make_service(settings, inbox)
                self.bot.send.side_effect = None
                self.api.download.side_effect = [ValueError('broken'), b'png-bytes']
                resumed = inbox.take()
                self.assertEqual(resumed['checkpoint']['text_done'], 1)
                await service.process(resumed)
                inbox.done(resumed['id'])
                # No refetch or repeat of the acknowledged first text chunk.
                self.assertEqual(len(self.api.calls), 1)
                self.assertEqual(sum(call.kwargs.get('text') == 'a' * 3000
                                     for call in self.bot.send.await_args_list), 1)
                last_text = self.bot.send.await_args_list[-2].kwargs['text']
                self.assertIn('第 2 页', last_text)
                self.assertIn('最多 2 张', last_text)
                self.bot.send.assert_awaited_with('group', GROUP, image=b'png-bytes')
                self.assertEqual(self.api.download.await_count, 2)
                # Completed tasks survive another reload and time change.
                inbox.close()
                inbox = Inbox(path)
                service = self.make_service({**settings, 'daily_broadcast_time': '08:00'}, inbox)
                self.assertEqual(service.daily.enqueue(), 0)
                self.assertIsNone(inbox.take())
            finally:
                inbox.close()

    async def test_zero_image_limit_sends_text_only(self):
        self.api.results[('GET', '/v1/on-this-day')]['broadcast_images'] = ['image']
        service = self.make_service(daily_config(max_broadcast_images=0))
        service.daily.enqueue()
        await service.process(self.inbox.take())
        self.api.download.assert_not_awaited()
        self.bot.send.assert_awaited_once()

    async def test_empty_history_is_completed_without_messages_or_repeated_queries(self):
        self.api.results[('GET', '/v1/on-this-day')] = {
            'broadcast_text': '历史上的今天（11月14日）：暂无往年记录。',
            'broadcast_images': [], 'page': 1, 'has_more': False, 'total': 0, 'items': [],
        }
        self.service.config.update(webhook_port=0)
        await self.service.start()
        for _ in range(100):
            if self.inbox.summary().get('done'):
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.inbox.summary(), {'done': 1})
        self.assertEqual(self.service.daily.enqueue(), 0)
        self.assertEqual(len(self.api.calls), 1)
        self.bot.send.assert_not_awaited()
        self.api.download.assert_not_awaited()

    async def test_records_are_sent_even_when_text_mentions_no_records(self):
        self.api.results[('GET', '/v1/on-this-day')]['broadcast_text'] = '活动文章：暂无往年记录功能介绍'
        self.service.daily.enqueue()
        await self.service.process(self.inbox.take())
        self.bot.send.assert_awaited_once_with('group', GROUP, text='活动文章：暂无往年记录功能介绍')

    async def test_image_send_failure_retries_image_without_repeating_text(self):
        self.api.results[('GET', '/v1/on-this-day')]['broadcast_images'] = ['image']
        self.service.daily.enqueue()
        job = self.inbox.take()
        self.bot.send.side_effect = [None, RuntimeError('offline')]
        with self.assertRaises(RuntimeError):
            await self.service.process(job)
        self.inbox.fail(job, 'offline', permanent=True)
        self.inbox.retry_notification(job['id'])
        self.bot.send.side_effect = None
        await self.service.process(self.inbox.take())
        self.assertEqual(sum('text' in call.kwargs for call in self.bot.send.await_args_list), 1)
        self.bot.send.assert_awaited_with('group', GROUP, image=b'png-bytes')
        self.assertEqual(len(self.api.calls), 1)

    async def test_internal_daily_jobs_cannot_be_submitted_by_webhook(self):
        self.service.daily.enqueue()
        payload = self.inbox.take()['payload']
        self.assertEqual(payload['event'], DAILY_EVENT)
        app = web.Application()
        app.router.add_post('/guatswr/webhook', self.service.webhook)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/guatswr/webhook', json=payload,
                headers={'Authorization': 'Bearer test-outbound-secret'})
            self.assertEqual(response.status, 400)

    async def test_service_starts_scheduler_processes_and_cancels_tasks(self):
        self.service.config.update(webhook_port=0)
        await self.service.start()
        self.assertIsNotNone(self.service.scheduler)
        for _ in range(100):
            if self.inbox.summary().get('done'):
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.inbox.summary(), {'done': 1})
        await self.service.close()
        self.assertTrue(self.service.scheduler.done())
        self.assertTrue(self.service.worker.done())

    async def test_disabled_service_does_not_start_scheduler(self):
        self.service.daily.enabled = False
        self.service.config.update(webhook_port=0)
        await self.service.start()
        self.assertIsNone(self.service.scheduler)
        self.assertIsNone(self.inbox.take())
