"""Webhook inbox worker, member-sync upload and notification routing."""

import asyncio
import hashlib
import hmac
import time

import aiohttp
from aiohttp import web

from .broadcast import DAILY_EVENT, DailyBroadcast
from .client import ApiError
from .protocol import notification_text, positive, qq, uuid, validate_envelope


class Service:
    def __init__(self, config, api, inbox, bot, logger):
        self.config, self.api, self.inbox, self.bot, self.logger = config, api, inbox, bot, logger
        self.groups = frozenset(map(qq, config.get('allowed_groups', [])))
        self.legacy_groups = frozenset(map(qq, config.get('legacy_groups', [])))
        if not self.legacy_groups <= self.groups:
            raise ValueError('旧通知默认群必须属于 allowed_groups。')
        self.credential_id = uuid(config['credential_id'])
        self.daily = DailyBroadcast(config, api, inbox, bot, self.groups, self.credential_id, logger)
        self.wake = asyncio.Event()
        self.worker = self.scheduler = self.runner = None

    async def start(self):
        app = web.Application(client_max_size=256 * 1024)
        app.router.add_post('/guatswr/webhook', self.webhook)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, self.config.get('webhook_host', '127.0.0.1'),
                              int(self.config.get('webhook_port', 9245))).start()
        except BaseException:
            await self.runner.cleanup()
            raise
        self.worker = asyncio.create_task(self.run(), name='guatswr-inbox')
        if self.daily.enabled:
            self.scheduler = asyncio.create_task(self.daily.run(self.wake), name='guatswr-daily-broadcast')

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
        tasks = [task for task in (self.scheduler, self.worker) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def webhook(self, request):
        expected = 'Bearer ' + self.config['webhook_token']
        if not hmac.compare_digest(request.headers.get('Authorization', '').encode(), expected.encode()):
            return web.json_response({'error': 'unauthorized'}, status=401)
        if request.content_type != 'application/json':
            return web.json_response({'error': 'json_required'}, status=415)
        try:
            payload = await request.json()
            identifier = validate_envelope(payload, self.credential_id, self.groups, self.legacy_groups)
            # A website manual retry reuses the same ID. Only exhausted failures
            # are revived; done/pending jobs keep their dedup/checkpoints.
            created = self.inbox.put(identifier, payload, revive=True)
        except (ValueError, TypeError, KeyError):
            return web.json_response({'error': 'invalid_or_conflicting_payload'}, status=400)
        except OverflowError:
            return web.json_response({'error': 'queue_full'}, status=503)
        self.wake.set()
        return web.json_response({'accepted': True, 'duplicate': not created}, status=202)

    def save(self, job):
        self.inbox.checkpoint(job['id'], job['checkpoint'])

    async def run(self):
        while True:
            self.wake.clear()
            job = self.inbox.take()
            if job is None:
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                if job['checkpoint'].get('failure_report'):
                    await self.report_failure(job)
                    if job['checkpoint'].get('private_sent'):
                        self.inbox.done(job['id'])
                    else:
                        self.inbox.fail(job, 'reported_failure', permanent=True)
                    continue
                if await self.process(job) is not False:
                    self.inbox.done(job['id'])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                permanent = isinstance(error, ValueError) or (isinstance(error, ApiError) and not error.retryable)
                # IDs and fixed codes only: never log raw gateway/API exceptions,
                # message bodies, QQ member lists, or Authorization headers.
                code = f'api_{error.status}' if isinstance(error, ApiError) else type(error).__name__
                self.logger.warning('GuatSWR job %s failed (%s)', job['id'], code)
                terminal = permanent or job['attempts'] >= 5
                if terminal and not job['checkpoint'].get('failure_report'):
                    try:
                        await self.report_failure(job)
                    except Exception:
                        # Persist report work separately so a lost callback does
                        # not cause a private message to be sent again.
                        job['checkpoint']['failure_report'] = True
                        self.save(job)
                        self.inbox.fail(job, 'failure_report_pending', permanent=False)
                        # Even after five failures retain callback retries.
                        with self.inbox.db:
                            self.inbox.db.execute("UPDATE inbox SET state='pending' WHERE id=?", (job['id'],))
                        continue
                    if job['checkpoint'].get('private_sent'):
                        self.inbox.done(job['id'])
                        continue
                elif job['checkpoint'].get('failure_report'):
                    self.inbox.fail(job, 'failure_report_pending')
                    with self.inbox.db:
                        self.inbox.db.execute("UPDATE inbox SET state='pending' WHERE id=?", (job['id'],))
                    continue
                self.inbox.fail(job, code, permanent=permanent)

    async def report_failure(self, job):
        payload = job['payload']
        if (payload.get('target') or {}).get('bot_id') != self.credential_id:
            return  # Never use replacement credentials to mutate an old job.
        try:
            if payload['event'] == 'members.profile_required':
                # A lost receipt must not turn an acknowledged send into failed.
                status = 'sent' if job['checkpoint'].get('private_sent') else 'failed'
                await self.api.request('POST', '/v1/deliveries/' + uuid(job['id']) + '/receipt',
                                       body={'status': status, **({'error_code': 'unavailable'} if status == 'failed' else {})})
            elif payload['event'] == 'members.sync.request':
                await self.api.request('POST', '/v1/member-sync/requests/' + uuid(payload['data']['request_id']) + '/fail', body={})
        except ApiError as error:
            if error.status not in (404, 409, 410):
                raise

    async def process(self, job):
        payload, checkpoint = job['payload'], job['checkpoint']
        if payload['event'] == DAILY_EVENT:
            if payload['target']['group_id'] not in self.groups:
                return
            return await self.daily.process(job)
        validate_envelope(payload, self.credential_id, self.groups, self.legacy_groups)
        name = payload['event']
        if name == 'members.sync.request':
            await self.sync_members(job)
        elif name == 'members.profile_required':
            if not checkpoint.get('private_sent'):
                await self.bot.send('private', payload['target']['qq'],
                                    text=notification_text(payload, self.api.site_url))
                checkpoint['private_sent'] = True
                self.save(job)
            await self.api.request('POST', '/v1/deliveries/' + uuid(job['id']) + '/receipt', body={'status': 'sent'})
        else:
            targets = [payload['target']['group_id']] if payload.get('target') else sorted(self.legacy_groups)
            sent = checkpoint.setdefault('groups_sent', [])
            for target in targets:
                if target not in sent:
                    await self.bot.send('group', target, text=notification_text(payload, self.api.site_url))
                    sent.append(target)
                    self.save(job)

    async def sync_members(self, job):
        data, checkpoint = job['payload']['data'], job['checkpoint']
        path = '/v1/member-sync/requests/' + uuid(data['request_id'])
        info = await self.api.request('GET', path)
        if info['group_id'] != data['group_id'] or info['credential_id'] != self.credential_id:
            raise ValueError('采集任务归属不匹配。')
        if info['status'] in ('preview', 'blocked', 'succeeded'):
            return
        expires = min(int(info['expires_at']), int(data['expires_at'])) / 1000
        if info['status'] not in ('queued', 'collecting') or time.time() >= expires:
            raise ApiError(410)
        limits = data.get('limits', {})
        batch_size = min(500, positive(limits.get('batch', 500), 500))
        if 'snapshot' not in checkpoint:
            checkpoint['snapshot'] = await self.bot.members(data['group_id'],
                                                            min(10000, positive(limits.get('members', 10000), 10000)))
            self.save(job)
        members = checkpoint['snapshot']
        batches = [members[i:i + batch_size] for i in range(0, len(members), batch_size)] or [[]]
        if len(batches) > min(100, positive(limits.get('batches', 100), 100)):
            raise ValueError('采集批次数超过任务限制。')
        for no, batch in enumerate(batches, 1):
            if no <= checkpoint.get('batches_done', 0):
                continue
            if time.time() >= expires - 5:
                raise ApiError(410)
            await self.api.request('PUT', path + f'/batches/{no}', body={'members': batch})
            checkpoint['batches_done'] = no
            self.save(job)
        if self.config.get('sync_avatars', True):
            known = {row['qq']: row['checksum'] for row in info.get('known_avatars', [])}
            for index in range(checkpoint.get('avatars_done', 0), len(members)):
                if time.time() >= expires - 30:
                    break  # Avatars are optional; reserve time for complete.
                identifier = members[index]['qq']
                try:
                    raw, mime = await self.api.download(f'https://q1.qlogo.cn/g?b=qq&nk={identifier}&s=640', avatar=True)
                    if hashlib.sha256(raw).hexdigest() != known.get(identifier):
                        await self.api.request('POST', path + '/avatars', raw=raw,
                                               headers={'X-Member-QQ': identifier, 'Content-Type': mime})
                except ApiError as error:
                    if error.status in (401, 403, 409, 410):
                        raise
                except (ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                    pass  # Preserve the previous avatar on a per-image failure.
                checkpoint['avatars_done'] = index + 1
                self.save(job)
        await self.api.request('POST', path + '/complete',
                               body={'complete': True, 'batch_count': len(batches), 'member_count': len(members)})
