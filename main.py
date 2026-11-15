import asyncio
import base64
import re

import aiohttp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, StarTools, register

from .client import ApiError, WebsiteClient
from .commands import Commands, HELP
from .inbox import Inbox
from .onebot import OneBot
from .protocol import origin, uuid
from .service import Service


@register('astrbot_plugin_guatswr', 'GuatSWR', '桂航绯想天网站 Bot API 适配', '0.1.0')
class GuatSWRPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = self.api = self.inbox = None

    async def initialize(self):
        if not self.config.get('enabled', False):
            logger.info('GuatSWR 尚未启用，请在插件配置中填写 API 和 Webhook 凭据。')
            return
        origin(self.config['site_url'])
        uuid(self.config['credential_id'])
        if not re.fullmatch(r'gwb_[a-f0-9]{64}', self.config.get('api_token', '')):
            raise ValueError('请填写网站创建的 gwb_ 服务凭据。')
        if not self.config.get('webhook_token') or not self.config.get('platform_id'):
            raise ValueError('必须配置 Webhook token 和 OneBot v11 平台 ID。')
        if self.config['webhook_token'] == self.config['api_token']:
            raise ValueError('Webhook token 与网站 Bot API 凭据必须分开配置。')
        if not 0 <= int(self.config.get('max_broadcast_images', 6)) <= 20:
            raise ValueError('max_broadcast_images 须在 0–20 之间。')
        if not 1 <= int(self.config.get('webhook_port', 9245)) <= 65535:
            raise ValueError('webhook_port 须在 1–65535 之间。')
        self.inbox = Inbox(StarTools.get_data_dir('astrbot_plugin_guatswr') / 'inbox.sqlite3')
        try:
            self.api = WebsiteClient(self.config['site_url'], self.config['api_token'],
                                     self.config.get('asset_origin', ''), timeout=15,
                                     api_origin=self.config.get('api_origin', ''))
            self.service = Service(self.config, self.api, self.inbox,
                                    OneBot(self.context, self.config['platform_id']), logger)
            await self.service.start()
        except BaseException:
            await self.terminate()
            raise

    async def terminate(self):
        if self.service:
            await self.service.close()
            self.service = None
        if self.api:
            await self.api.close()
            self.api = None
        if self.inbox:
            self.inbox.close()
            self.inbox = None

    @filter.command('guatswr', alias={'桂航绯想天'})
    async def guatswr(self, event: AstrMessageEvent):
        """guatswr / 桂航绯想天 帮助：查询网站、报名、订阅及审核。"""
        event.stop_event()
        if not self.service:
            yield event.plain_result('插件尚未启用，请管理员完成插件配置。\n' + HELP)
            return
        if event.get_platform_name() != 'aiocqhttp' or event.get_platform_id() != self.config['platform_id']:
            yield event.plain_result('请通过配置的 QQ OneBot v11 机器人使用此插件。')
            return
        try:
            # AstrBot's command filter consumes its wake prefix before dispatch.
            # No formal parameters: parse the remaining text with quoted strings.
            text = re.sub(r'^/?(?:guatswr|桂航绯想天)(?:\s+|$)', '', event.get_message_str().strip(), count=1)
            output, images = await Commands(self.service).execute(
                text, sender=event.get_sender_id(), group=event.get_group_id(),
                private=event.is_private_chat(), admin=event.is_admin())
            for start in range(0, len(output), 3000):
                yield event.plain_result(output[start:start + 3000])
            for url in images:
                try:
                    raw = await self.api.download(url)
                    yield event.chain_result([Comp.Image.fromBase64(base64.b64encode(raw).decode())])
                except Exception:
                    logger.info('GuatSWR 跳过一张无法下载或解码的播报图片。')
        except (ValueError, ApiError) as error:
            yield event.plain_result(str(error))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            yield event.plain_result('网站连接失败或超时，请稍后重试。')
        except Exception as error:
            logger.warning('GuatSWR command failed (%s)', type(error).__name__)
            yield event.plain_result('操作失败，请管理员检查插件配置和服务状态。')
