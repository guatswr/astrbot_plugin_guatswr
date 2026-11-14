"""Exercise the plugin lifecycle/handler with a small AstrBot interface shim.

This is not a live AstrBot or QQ test; production adapter behaviour still needs
a test group. The shim keeps the repository's checks free of the AstrBot stack.
"""

import importlib
import logging
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from astrbot_plugin_guatswr.tests.test_plugin import FakeApi, config, GROUP, SENDER


class FakeEvent:
    def __init__(self, text, platform='qq-main'):
        self.text, self.platform, self.stopped = text, platform, False

    def stop_event(self):
        self.stopped = True

    def get_platform_name(self):
        return 'aiocqhttp'

    def get_platform_id(self):
        return self.platform

    def get_sender_id(self):
        return SENDER

    def get_group_id(self):
        return GROUP

    def is_private_chat(self):
        return False

    def is_admin(self):
        return False

    def get_message_str(self):
        return self.text

    def plain_result(self, text):
        return ('text', text)

    def chain_result(self, chain):
        return ('chain', chain)


class EntryPointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        api_module = types.ModuleType('astrbot.api')
        api_module.AstrBotConfig = dict
        api_module.logger = logging.getLogger('test-entrypoint')
        event_module = types.ModuleType('astrbot.api.event')
        event_module.AstrMessageEvent = FakeEvent
        event_module.filter = types.SimpleNamespace(command=lambda name: lambda function: function)
        components = types.ModuleType('astrbot.api.message_components')
        components.Image = types.SimpleNamespace(fromBase64=lambda value: ('image', value))
        star_module = types.ModuleType('astrbot.api.star')

        class Star:
            def __init__(self, context):
                self.context = context

        star_module.Context = object
        star_module.Star = Star
        star_module.StarTools = object
        star_module.register = lambda *args: lambda cls: cls
        self.module_patch = patch.dict(sys.modules, {
            'astrbot': types.ModuleType('astrbot'), 'astrbot.api': api_module,
            'astrbot.api.event': event_module, 'astrbot.api.message_components': components,
            'astrbot.api.star': star_module,
        })
        self.module_patch.start()
        self.main = importlib.import_module('astrbot_plugin_guatswr.main')
        self.plugin = self.main.GuatSWRPlugin(object(), {'enabled': False, 'platform_id': 'qq-main'})

    async def asyncTearDown(self):
        sys.modules.pop('astrbot_plugin_guatswr.main', None)
        self.module_patch.stop()

    async def test_disabled_lifecycle_opens_no_connections(self):
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.api)
        event = FakeEvent('桂航 帮助')
        output = [item async for item in self.plugin.guatswr(event)]
        self.assertIn('尚未启用', output[0][1])
        self.assertTrue(event.stopped)
        await self.plugin.terminate()

    async def test_handler_continues_after_individual_image_failure(self):
        api = FakeApi()
        api.results[('GET', '/v1/on-this-day')] = {'broadcast_text': 'history [CQ:at,qq=all]',
            'broadcast_images': ['bad', 'good'], 'has_more': False}
        api.download = AsyncMock(side_effect=[ValueError('broken image'), b'png-bytes'])
        self.plugin.service = types.SimpleNamespace(api=api, groups={GROUP}, config=config())
        self.plugin.api = api
        result = [item async for item in self.plugin.guatswr(FakeEvent('桂航 今天'))]
        self.assertEqual(result[0], ('text', 'history [CQ:at,qq=all]'))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1][0], 'chain')
        self.assertEqual(api.download.await_count, 2)

    async def test_handler_rejects_other_bot_instance(self):
        self.plugin.service = object()
        result = [item async for item in self.plugin.guatswr(FakeEvent('桂航 今天', platform='another-bot'))]
        self.assertIn('配置的 QQ', result[0][1])
