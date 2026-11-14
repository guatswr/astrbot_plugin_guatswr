"""Authenticated API requests and separate, bounded anonymous image downloads."""

import asyncio
import io
import json
import warnings
from urllib.parse import urlsplit

import aiohttp
from PIL import Image, ImageOps

from .protocol import origin


class ApiError(Exception):
    def __init__(self, status):
        self.status = status
        self.retryable = status in (408, 425, 429) or status >= 500
        super().__init__({400: '请求参数无效。', 401: 'Bot 凭据无效或已撤销。',
                          403: '缺少接口权限、群授权或网站身份权限。', 404: '记录不存在或尚未公开。',
                          409: '状态冲突，请到网站确认当前状态。', 410: '任务或绑定码已过期。',
                          413: '上传内容超过限制。', 429: '请求过于频繁，请稍后再试。',
                          503: 'Bot 平台未启用或服务暂不可用。'}.get(status, f'网站请求失败（HTTP {status}）。'))


async def limited_read(response, maximum):
    content = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        content.extend(chunk)
        if len(content) > maximum:
            raise ValueError('响应超过大小限制。')
    return bytes(content)


def png_image(raw):
    # Decode in a thread, cap decompression and normalise AVIF/GIF/BMP/WebP to
    # PNG for OneBot. A broken image never prevents the accompanying text.
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            if image.width * image.height > 16000000:
                raise ValueError('图片像素过大。')
            image.seek(0)
            image = ImageOps.exif_transpose(image).convert('RGBA')
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.save(output, format='PNG')
            return output.getvalue()


class WebsiteClient:
    def __init__(self, site_url, token, asset_origin, timeout=15, api_origin=None):
        self.site_url = origin(site_url)
        self.api_origin = origin(api_origin or site_url)
        self.asset_origin = origin(asset_origin or site_url)
        self.api = aiohttp.ClientSession(headers={'Authorization': 'Bearer ' + token},
                                         timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False)
        self.public = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False)

    async def request(self, method, path, *, params=None, body=None, raw=None, headers=None):
        if not path.startswith('/v1/') or '?' in path or '#' in path or '..' in path:
            raise ValueError('仅允许调用 Bot v1 接口。')
        kwargs = {'params': params, 'headers': headers, 'allow_redirects': False}
        if raw is not None:
            kwargs['data'] = raw
        elif body is not None:
            kwargs['json'] = body
        async with self.api.request(method, self.api_origin + '/api/bot' + path, **kwargs) as response:
            if not 200 <= response.status < 300:
                raise ApiError(response.status)
            result = json.loads(await limited_read(response, 4 * 1024 * 1024))
            if not isinstance(result, dict):
                raise ValueError('网站返回格式错误。')
            return result

    async def download(self, url, *, avatar=False):
        parts = urlsplit(url)
        if avatar:
            valid = (parts.scheme == 'https' and parts.netloc == 'q1.qlogo.cn'
                     and parts.path == '/g' and not parts.fragment and not parts.username)
        else:
            valid = (f'{parts.scheme}://{parts.netloc}' == self.asset_origin
                     and parts.path.startswith('/uploads/') and not parts.query and not parts.fragment
                     and not parts.username)
        if not valid:
            raise ValueError('图片地址不在配置的公共资源范围内。')
        async with self.public.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ValueError('图片下载失败。')
            raw = await limited_read(response, 2097152 if avatar else 8 * 1024 * 1024)
        if avatar:
            if raw.startswith(b'\x89PNG\r\n\x1a\n'):
                mime = 'image/png'
            elif raw.startswith(b'\xff\xd8\xff'):
                mime = 'image/jpeg'
            elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
                mime = 'image/webp'
            else:
                raise ValueError('头像格式不受支持。')
            return raw, mime
        return await asyncio.to_thread(png_image, raw)

    async def close(self):
        await self.api.close()
        await self.public.close()
