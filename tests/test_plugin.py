import asyncio
import hashlib
import io
import json
import logging
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from astrbot_plugin_guatswr.client import ApiError, WebsiteClient, png_image
from astrbot_plugin_guatswr.commands import Commands
from astrbot_plugin_guatswr.inbox import Inbox
from astrbot_plugin_guatswr.onebot import OneBot
from astrbot_plugin_guatswr.protocol import EVENTS, normalize_members, notification_text, origin, validate_envelope
from astrbot_plugin_guatswr.service import Service

CREDENTIAL = '00000000-0000-4000-8000-000000000001'
DELIVERY = '00000000-0000-4000-8000-000000000002'
REQUEST = '00000000-0000-4000-8000-000000000003'
SUBSCRIPTION = '00000000-0000-4000-8000-000000000004'
GROUP, SENDER = '123456789', '987654321'


def config(**extra):
    return {'credential_id': CREDENTIAL, 'allowed_groups': [GROUP], 'legacy_groups': [],
            'webhook_token': 'test-outbound-secret', 'sync_avatars': False, **extra}


def envelope(event='event.approved', **extra):
    return {'delivery_id': DELIVERY, 'event_id': REQUEST, 'event': event,
            'data': {'eventId': 12, 'title': '[CQ:at,qq=all]'}, 'source': 'guatswr_web',
            'target': {'bot_id': CREDENTIAL, 'group_id': GROUP}, **extra}


def sync_envelope():
    return envelope('members.sync.request', kind='command',
                    target={'bot_id': CREDENTIAL, 'type': 'service'},
                    data={'request_id': REQUEST, 'group_id': GROUP,
                          'expires_at': int((time.time() + 600) * 1000),
                          'limits': {'members': 10000, 'batch': 500, 'batches': 100}})


def private_envelope():
    return envelope('members.profile_required', kind='notification',
                    target={'bot_id': CREDENTIAL, 'type': 'private', 'qq': SENDER},
                    data={'count': 1, 'members': [{'qq': '12345', 'name': '[CQ:at,qq=all]'}]})


class FakeApi:
    site_url = 'https://site.test'

    def __init__(self):
        self.calls = []
        self.results = {}
        self.fail_once = {}
        self.download = AsyncMock(return_value=(b'\x89PNG\r\n\x1a\nfixture', 'image/png'))

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.fail_once.pop((method, path), False):
            raise ApiError(503)
        return self.results.get((method, path), {})


class CoreTests(unittest.TestCase):
    def test_origin_rejects_paths_credentials_and_queries(self):
        for url in ['https://a.test/api', 'https://user:pass@a.test', 'file:///a', 'https://a.test?x=1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                origin(url)
        self.assertEqual(origin('http://127.0.0.1:3000/'), 'http://127.0.0.1:3000')

    def test_webhook_rejects_cross_credential_group_and_command_kind(self):
        for payload in [envelope(target={'bot_id': REQUEST, 'group_id': GROUP}),
                        envelope(target={'bot_id': CREDENTIAL, 'group_id': '12345'}),
                        envelope('members.sync.request'), envelope(event='plugin.on_this_day')]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_envelope(payload, CREDENTIAL, {GROUP}, set())
        self.assertEqual(validate_envelope(sync_envelope(), CREDENTIAL, {GROUP}, set()), DELIVERY)

    def test_legacy_only_routes_explicit_public_event_types(self):
        payload = envelope('history.updated', target=None)
        payload.pop('delivery_id')
        with self.assertRaises(ValueError):
            validate_envelope(payload, CREDENTIAL, {GROUP}, set())
        self.assertTrue(validate_envelope(payload, CREDENTIAL, {GROUP}, {GROUP}).startswith('legacy:'))
        with self.assertRaises(ValueError):
            validate_envelope(envelope('event.pending', target=None), CREDENTIAL, {GROUP}, {GROUP})

    def test_member_validation_rejects_duplicate_cross_group_and_partial_format(self):
        for rows in [[{'user_id': '12345'}, {'user_id': 12345}],
                     [{'user_id': '12345', 'group_id': '11111'}], {'members': []},
                     [{'user_id': '01234'}], [{'user_id': '12345', 'role': 'super_admin'}]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                normalize_members(rows, GROUP)
        rows = normalize_members([{'user_id': 12345, 'card': 'a' * 60, 'role': 'admin'}], GROUP)
        self.assertEqual(rows[0]['qq'], '12345')
        self.assertEqual(len(rows[0]['group_card']), 50)

    def test_notification_never_dumps_actor_or_internal_fields(self):
        payload = envelope(actor={'username': 'private-admin'}, data={
            'eventId': 1, 'title': '[CQ:at,qq=all]', 'qq': 'secret-qq', 'private': 'hidden'})
        text = notification_text(payload, 'https://site.test')
        self.assertIn('[CQ:at,qq=all]', text)
        for secret in ('private-admin', 'secret-qq', 'hidden'):
            self.assertNotIn(secret, text)
        self.assertNotIn('/events/1', text)

    def test_image_conversion_and_invalid_image(self):
        image = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(image, 'JPEG')
        self.assertTrue(png_image(image.getvalue()).startswith(b'\x89PNG'))
        with self.assertRaises(OSError):
            png_image(b'not an image')

    def test_event_names_match_backend_contract(self):
        shared = (Path(__file__).resolve().parents[1] / 'docs/contracts/bot.ts').read_text(encoding='utf-8')
        import re
        definition = re.search(r'export const botEventNames = \[(.*?)\] as const', shared).group(1)
        self.assertEqual(EVENTS, frozenset(re.findall(r"'([^']+)'", definition)))


class InboxTests(unittest.TestCase):
    def test_restart_dedupe_conflicts_and_payload_scrubbing(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'inbox.sqlite3'
            inbox = Inbox(path)
            self.assertTrue(inbox.put(DELIVERY, envelope()))
            job = inbox.take()
            inbox.checkpoint(DELIVERY, {'private_sent': True})
            inbox.close()
            inbox = Inbox(path)
            try:
                job = inbox.take()
                self.assertEqual(job['checkpoint'], {'private_sent': True})
                self.assertFalse(inbox.put(DELIVERY, envelope()))
                with self.assertRaises(ValueError):
                    inbox.put(DELIVERY, envelope(data={'eventId': 2}))
                inbox.done(DELIVERY)
                self.assertFalse(inbox.put(DELIVERY, envelope(), revive=True))
                self.assertIsNone(inbox.take())
                row = inbox.db.execute('SELECT payload,checkpoint FROM inbox').fetchone()
                self.assertEqual(tuple(row), ('{}', '{}'))
            finally:
                inbox.close()

    def test_website_redelivery_revives_failed_only(self):
        inbox = Inbox(':memory:')
        try:
            inbox.put(DELIVERY, envelope())
            job = inbox.take()
            inbox.fail(job, 'test', permanent=True)
            self.assertIsNone(inbox.take())
            inbox.put(DELIVERY, envelope(), revive=True)
            self.assertIsNotNone(inbox.take())
        finally:
            inbox.close()


class ServiceFixture:
    async def asyncSetUp(self):
        self.inbox = Inbox(':memory:')
        self.api = FakeApi()
        self.bot = type('Bot', (), {'send': AsyncMock(), 'members': AsyncMock()})()
        self.service = Service(config(), self.api, self.inbox, self.bot, logging.getLogger('test'))

    async def asyncTearDown(self):
        await self.service.close()
        self.inbox.close()

    def job(self, payload):
        self.inbox.put(DELIVERY, payload)
        return self.inbox.take()

    def task_info(self, **extra):
        return {'status': 'queued', 'group_id': GROUP, 'credential_id': CREDENTIAL,
                'expires_at': int((time.time() + 600) * 1000), 'known_avatars': [], **extra}


class ServiceTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):

    async def test_actual_webhook_auth_json_size_target_and_duplicate(self):
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_post('/guatswr/webhook', self.service.webhook)
        async with TestClient(TestServer(app)) as client:
            response = await client.post('/guatswr/webhook', json=envelope())
            self.assertEqual(response.status, 401)
            headers = {'Authorization': 'Bearer test-outbound-secret'}
            response = await client.post('/guatswr/webhook', json=envelope(), headers=headers)
            self.assertEqual(response.status, 202)
            response = await client.post('/guatswr/webhook', json=envelope(), headers=headers)
            self.assertTrue((await response.json())['duplicate'])
            response = await client.post('/guatswr/webhook', json=envelope(target={'bot_id': REQUEST, 'group_id': GROUP}), headers=headers)
            self.assertEqual(response.status, 400)
            response = await client.post('/guatswr/webhook', data='x' * (256 * 1024 + 1),
                                         headers={**headers, 'Content-Type': 'application/json'})
            self.assertEqual(response.status, 413)
        self.assertEqual(self.inbox.summary(), {'pending': 1})

    async def test_private_receipt_failure_and_restart_do_not_resend(self):
        job = self.job(private_envelope())
        path = f'/v1/deliveries/{DELIVERY}/receipt'
        self.api.fail_once[('POST', path)] = True
        with self.assertRaises(ApiError):
            await self.service.process(job)
        self.assertTrue(job['checkpoint']['private_sent'])
        self.inbox.fail(job, 'temporary')
        self.inbox.db.execute('UPDATE inbox SET due=0')
        resumed = self.inbox.take()
        await self.service.process(resumed)
        self.bot.send.assert_awaited_once()
        self.assertEqual(self.api.calls[-1][2]['body'], {'status': 'sent'})

    async def test_group_notification_checkpoints_and_literal_cq_text(self):
        job = self.job(envelope())
        await self.service.process(job)
        await self.service.process(job)
        self.bot.send.assert_awaited_once()
        self.assertIn('[CQ:at,qq=all]', self.bot.send.call_args.kwargs['text'])

    async def test_sync_batches_frozen_across_retry_and_no_group_broadcast(self):
        payload = sync_envelope()
        path = f'/v1/member-sync/requests/{REQUEST}'
        self.api.results[('GET', path)] = self.task_info()
        snapshot = [{'qq': str(10000 + i), 'group_card': '', 'nickname': f'n{i}', 'group_role': 'member'} for i in range(501)]
        self.bot.members.return_value = snapshot
        self.api.fail_once[('PUT', path + '/batches/2')] = True
        job = self.job(payload)
        with self.assertRaises(ApiError):
            await self.service.process(job)
        self.assertEqual(job['checkpoint']['batches_done'], 1)
        self.inbox.fail(job, 'retry')
        self.inbox.db.execute('UPDATE inbox SET due=0')
        resumed = self.inbox.take()
        await self.service.process(resumed)
        self.bot.members.assert_awaited_once()
        self.bot.send.assert_not_awaited()
        self.assertEqual(sum(path + '/batches/1' == call[1] for call in self.api.calls), 1)
        self.assertEqual(self.api.calls[-1][2]['body'], {'complete': True, 'batch_count': 2, 'member_count': 501})

    async def test_sync_expired_and_completed_do_not_collect(self):
        path = f'/v1/member-sync/requests/{REQUEST}'
        job = self.job(sync_envelope())
        self.api.results[('GET', path)] = self.task_info(status='preview')
        await self.service.process(job)
        self.api.results[('GET', path)] = self.task_info(expires_at=1)
        with self.assertRaises(ApiError):
            await self.service.process(job)
        self.bot.members.assert_not_awaited()

    async def test_queued_notifications_recheck_current_group_permissions(self):
        job = self.job(envelope())
        self.service.groups = frozenset()
        with self.assertRaises(ValueError):
            await self.service.process(job)
        self.bot.send.assert_not_awaited()

    async def test_manual_private_redelivery_clears_failed_callback_mode(self):
        payload = private_envelope()
        job = self.job(payload)
        job['checkpoint']['failure_report'] = True
        self.service.save(job)
        self.inbox.fail(job, 'reported_failure', permanent=True)
        self.inbox.put(DELIVERY, payload, revive=True)
        resumed = self.inbox.take()
        self.assertNotIn('failure_report', resumed['checkpoint'])
        await self.service.process(resumed)
        self.bot.send.assert_awaited_once()
        self.assertEqual(self.api.calls[-1][2]['body'], {'status': 'sent'})

    async def test_avatar_checksum_and_failed_download_do_not_block_complete(self):
        self.service.config['sync_avatars'] = True
        path = f'/v1/member-sync/requests/{REQUEST}'
        raw = b'\x89PNG\r\n\x1a\nfixture'
        self.api.results[('GET', path)] = self.task_info(known_avatars=[{'qq': '12345', 'checksum': hashlib.sha256(raw).hexdigest()}])
        self.bot.members.return_value = [{'qq': '12345'}, {'qq': '12346'}, {'qq': '12347'}]
        self.api.download.side_effect = [(raw, 'image/png'), ValueError('download failed'), (raw, 'image/png')]
        await self.service.process(self.job(sync_envelope()))
        uploads = [call for call in self.api.calls if call[1].endswith('/avatars')]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][2]['headers']['X-Member-QQ'], '12347')
        self.assertEqual(uploads[0][2]['raw'], raw)
        self.assertTrue(self.api.calls[-1][1].endswith('/complete'))

    async def test_empty_group_submits_empty_batch_for_website_preview(self):
        path = f'/v1/member-sync/requests/{REQUEST}'
        self.api.results[('GET', path)] = self.task_info()
        self.bot.members.return_value = []
        await self.service.process(self.job(sync_envelope()))
        self.assertEqual(self.api.calls[1][2]['body'], {'members': []})
        self.assertEqual(self.api.calls[-1][2]['body']['batch_count'], 1)

    async def test_failure_report_recovery_does_not_send_private_message(self):
        job = self.job(private_envelope())
        job['checkpoint'] = {'failure_report': True, 'private_sent': True}
        self.service.save(job)
        self.inbox.fail(job, 'retry')
        self.inbox.db.execute('UPDATE inbox SET due=0')
        worker = asyncio.create_task(self.service.run())
        try:
            for _ in range(100):
                if self.inbox.summary().get('done'):
                    break
                await asyncio.sleep(.01)
            self.assertEqual(self.inbox.summary(), {'done': 1})
            self.bot.send.assert_not_awaited()
            self.assertEqual(self.api.calls[-1][2]['body'], {'status': 'sent'})
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker

    async def test_partial_onebot_list_is_never_accepted(self):
        bot = OneBot(None, 'qq-main')
        bot.call = AsyncMock(side_effect=[{'member_count': 2}, [{'user_id': '12345'}], {'member_count': 2}])
        with self.assertRaises(ValueError):
            await bot.members(GROUP, 10000)

    async def test_onebot_uses_structured_text_and_checks_send_confirmation(self):
        bot = OneBot(None, 'qq-main')
        bot.call = AsyncMock(return_value={'message_id': 1})
        await bot.send('group', GROUP, text='[CQ:at,qq=all]')
        self.assertEqual(bot.call.call_args.kwargs['message'], [{'type': 'text', 'data': {'text': '[CQ:at,qq=all]'}}])
        bot.call.return_value = None
        with self.assertRaises(RuntimeError):
            await bot.send('private', SENDER, text='test')


class CommandTests(ServiceFixture, unittest.IsolatedAsyncioTestCase):
    async def execute(self, text, **extra):
        return await Commands(self.service).execute(text, sender=SENDER, group=None, private=True, admin=False, **extra)

    async def test_identity_is_from_message_context_and_never_an_argument(self):
        await self.execute('绑定 ' + 'a' * 32)
        self.assertEqual(self.api.calls[-1][2]['body']['qq'], SENDER)
        with self.assertRaises(ValueError):
            await self.execute('绑定 ' + 'a' * 32 + ' 111111')
        await self.execute('报名 12 "Player One" /uploads/avatar.png "A B" local')
        self.assertEqual(self.api.calls[-1][2]['body'], {'qq': SENDER, 'common_id': 'Player One',
            'avatar': '/uploads/avatar.png', 'machine_sign': 'A B', 'pilot_source': 'local'})

    async def test_sensitive_commands_reject_group_before_api_call(self):
        for text in ['绑定 ' + 'a' * 32, '待审', '我的报名 12', '审核 12 approve reason']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                await Commands(self.service).execute(text, sender=SENDER, group=GROUP, private=False, admin=True)
        self.assertEqual(self.api.calls, [])

    async def test_subscription_requires_bot_admin_and_current_group(self):
        with self.assertRaises(ValueError):
            await Commands(self.service).execute('订阅 event.approved', sender=SENDER, group=GROUP, private=False, admin=False)
        self.api.results[('POST', '/v1/subscriptions')] = {'id': SUBSCRIPTION}
        await Commands(self.service).execute('订阅 reminder.start 12 60', sender=SENDER, group=GROUP, private=False, admin=True)
        self.assertEqual(self.api.calls[-1][2]['body'], {'group_id': GROUP, 'event': 'reminder.start', 'event_id': 12, 'lead_minutes': 60})
        self.api.results[('GET', '/v1/subscriptions')] = {'items': [{'id': SUBSCRIPTION, 'group_id': '11111'}]}
        with self.assertRaises(ValueError):
            await Commands(self.service).execute('退订 ' + SUBSCRIPTION, sender=SENDER, group=GROUP, private=False, admin=True)
        self.assertFalse(any(call[0] == 'DELETE' for call in self.api.calls))

    async def test_on_this_day_pagination_images_and_invalid_dates(self):
        self.api.results[('GET', '/v1/on-this-day')] = {'broadcast_text': '历史上的今天',
            'broadcast_images': [f'https://site.test/uploads/{i}.png' for i in range(9)], 'has_more': True, 'page': 2}
        text, images = await self.execute('今天 2026-10-25 history 2')
        self.assertEqual(self.api.calls[-1][2]['params'], {'date': '2026-10-25', 'source': 'history', 'page': 2, 'limit': 10})
        self.assertEqual(len(images), 6)
        self.assertIn('第 3 页', text)
        with self.assertRaises(ValueError):
            await self.execute('今天 2025-02-29')

    async def test_manual_on_this_day_without_records_returns_empty_notice(self):
        self.api.results[('GET', '/v1/on-this-day')] = {
            'broadcast_text': '历史上的今天：暂无往年记录。', 'broadcast_images': [],
            'page': 1, 'has_more': False, 'total': 0, 'items': [],
        }
        text, images = await self.execute('今天')
        self.assertIn('暂无往年记录', text)
        self.assertEqual(images, [])

    async def test_review_uses_bound_qq_and_requires_reason(self):
        await self.execute('审核 12 reject "缺少活动规则"')
        self.assertEqual(self.api.calls[-1][2]['body'], {'qq': SENDER, 'action': 'reject', 'reason': '缺少活动规则'})
        with self.assertRaises(ValueError):
            await self.execute('审核 12 approve ""')

    async def test_local_group_retry_and_private_retry_boundary(self):
        job = self.job(envelope())
        self.inbox.fail(job, 'unavailable', permanent=True)
        rows = self.inbox.failed()
        self.assertEqual(rows[0]['id'], DELIVERY)
        self.inbox.retry_notification(DELIVERY)
        self.assertIsNotNone(self.inbox.take())
        self.inbox.done(DELIVERY)
        with self.assertRaises(ValueError):
            self.inbox.retry_notification(DELIVERY)
        payload = private_envelope()
        payload['delivery_id'] = REQUEST
        self.inbox.put(REQUEST, payload)
        self.inbox.fail(self.inbox.take(), 'unavailable', permanent=True)
        with self.assertRaises(ValueError):
            self.inbox.retry_notification(REQUEST)


class HttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_api_origin_is_separate_from_public_links_and_assets(self):
        captured = []

        async def backend(request):
            captured.append((request.path, request.headers.get('Authorization')))
            return web.json_response({'items': []})

        app = web.Application()
        app.router.add_get('/api/bot/v1/events', backend)
        async with TestServer(app) as server:
            internal = str(server.make_url('/')).rstrip('/')
            api = WebsiteClient('https://public.test', 'gwb_private', 'https://assets.test', api_origin=internal)
            try:
                await api.request('GET', '/v1/events')
                self.assertEqual(api.site_url, 'https://public.test')
                self.assertEqual(api.asset_origin, 'https://assets.test')
                self.assertEqual(captured, [('/api/bot/v1/events', 'Bearer gwb_private')])
            finally:
                await api.close()

    async def test_auth_is_confined_to_bot_api_and_redirects_are_rejected(self):
        captured = []
        output = io.BytesIO()
        Image.new('RGB', (2, 2)).save(output, 'PNG')

        async def handler(request):
            captured.append((request.path, request.headers.get('Authorization')))
            if request.path.endswith('/redirect'):
                raise web.HTTPFound('/uploads/image.png')
            if request.path.startswith('/uploads/'):
                return web.Response(body=output.getvalue(), content_type='image/png')
            return web.json_response({'items': []})

        app = web.Application()
        app.router.add_get('/{tail:.*}', handler)
        async with TestServer(app) as server:
            url = str(server.make_url('/')).rstrip('/')
            api = WebsiteClient(url, 'gwb_test-only', url)
            try:
                await api.request('GET', '/v1/events')
                self.assertTrue((await api.download(url + '/uploads/image.png')).startswith(b'\x89PNG'))
                with self.assertRaises(ApiError):
                    await api.request('GET', '/v1/redirect')
                with self.assertRaises(ValueError):
                    await api.download('https://other.test/uploads/a.png')
                with self.assertRaises(ValueError):
                    await api.request('GET', '/admin/credentials')
                self.assertEqual(captured, [('/api/bot/v1/events', 'Bearer gwb_test-only'),
                                            ('/uploads/image.png', None), ('/api/bot/v1/redirect', 'Bearer gwb_test-only')])
            finally:
                await api.close()


if __name__ == '__main__':
    unittest.main()
