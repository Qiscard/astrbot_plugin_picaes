import os
import io
import json
import ssl
import time
import uuid
import asyncio
import aiohttp
import certifi
from PIL import Image as PILImage
from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


# ==================== 本地加解密算法 ====================

_DEFAULT_KEY = "hadsky.com"


def _get_rnd(key: str, num: int) -> int:
    idx = num % len(key)
    idx = ord(key[idx]) % len(key)
    return int(float(f"0.{idx}") * num)


def _shuffle_encrypt(blocks: list, user_key: str):
    blocks.reverse()
    for i in range(len(blocks) - 1, -1, -1):
        j = _get_rnd(_DEFAULT_KEY, i + 1)
        blocks[i], blocks[j] = blocks[j], blocks[i]
    if user_key:
        for i in range(len(blocks) - 1, -1, -1):
            j = _get_rnd(user_key, i + 1)
            blocks[i], blocks[j] = blocks[j], blocks[i]


def _shuffle_decrypt(blocks: list, user_key: str):
    if user_key:
        for i in range(len(blocks)):
            j = _get_rnd(user_key, i + 1)
            blocks[j], blocks[i] = blocks[i], blocks[j]
    for i in range(len(blocks)):
        j = _get_rnd(_DEFAULT_KEY, i + 1)
        blocks[j], blocks[i] = blocks[i], blocks[j]
    blocks.reverse()


def process_image_local(image_bytes: bytes, level: int, key: str, mode: str) -> bytes:
    """本地图片加解密，原样输出（不做格式转换）"""
    img = PILImage.open(io.BytesIO(image_bytes))
    src_format = img.format or "PNG"

    # 像素操作统一用 RGBA
    img_rgba = img.convert("RGBA")
    img_w, img_h = img_rgba.size

    n = level * 10
    block_w = img_w // n
    block_h = img_h // n

    blocks = []
    for row in range(n):
        for col in range(n):
            x0, y0 = col * block_w, row * block_h
            block = img_rgba.crop((x0, y0, x0 + block_w, y0 + block_h))
            blocks.append(block)

    if mode == "encrypt":
        _shuffle_encrypt(blocks, key)
    else:
        _shuffle_decrypt(blocks, key)

    result = PILImage.new("RGBA", (img_w, img_h))
    for i, block in enumerate(blocks):
        row, col = divmod(i, n)
        result.paste(block, (col * block_w, row * block_h))

    # 保持原始格式输出
    buf = io.BytesIO()
    if src_format == "JPEG":
        result.convert("RGB").save(buf, format="JPEG", quality=95)
    else:
        result.save(buf, format="PNG")
    return buf.getvalue()


# ==================== 保存路径 ====================

def _save_result(data: bytes) -> str:
    """保存到 plugin_data/astrbot_plugin_picaes/，原样写入"""
    save_dir = os.path.join(get_astrbot_plugin_data_path(), "astrbot_plugin_picaes")
    os.makedirs(save_dir, exist_ok=True)
    filename = f"picaes_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    path = os.path.join(save_dir, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


# ==================== 插件主体 ====================

@register(
    "astrbot_plugin_picaes",
    "AstrBotUser",
    "通过API对图片进行马赛克加密/解密，支持自定义加密等级和密钥",
    "2.1.0",
    "astrbot_plugin_picaes",
)
class PicaesPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        config = config or {}
        self.api_url = config.get("api_url", "https://picace.995456.xyz/api/proxy")
        self.default_key = config.get("default_key", "tool.hadsky.com")
        self.default_level = config.get("default_level", 4)
        self.timeout = config.get("timeout", 120)
        self.process_mode = config.get("process_mode", 0)

    def _find_image_component(self, chain) -> Image | None:
        for seg in chain:
            if isinstance(seg, Image):
                return seg
        return None

    def _parse_args(self, text: str) -> tuple:
        parts = text.strip().split()
        level = self.default_level
        key = self.default_key
        if parts:
            try:
                lv = int(parts[0])
                if 1 <= lv <= 10:
                    level = lv
                    if len(parts) >= 2:
                        key = parts[1]
            except ValueError:
                if parts[0]:
                    key = parts[0]
        return level, key

    # ---------- 网络请求 ----------

    async def _request(self, method: str, url: str, **kwargs) -> tuple:
        try:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(trust_env=True, connector=connector) as s:
                async with s.request(method, url, **kwargs) as r:
                    return r.status, dict(r.headers), await r.read()
        except (aiohttp.ClientConnectorSSLError, aiohttp.ClientConnectorCertificateError):
            pass
        except Exception as e:
            logger.error(f"[Picaes] 请求失败({url}): {e}")
            return 0, {}, None
        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            async with aiohttp.ClientSession() as s:
                async with s.request(method, url, ssl=ssl_ctx, **kwargs) as r:
                    return r.status, dict(r.headers), await r.read()
        except Exception as e:
            logger.error(f"[Picaes] 回退请求失败({url}): {e}")
            return 0, {}, None

    # ---------- 图片下载 ----------

    async def _download_image_bytes(self, img_comp: Image) -> bytes | None:
        url = img_comp.url or img_comp.file
        if not url:
            return None
        if url.startswith("base64://"):
            import base64
            return base64.b64decode(url.removeprefix("base64://"))
        if url.startswith("file:///"):
            path = url[8:]
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return f.read()
            return None
        if not url.startswith("http"):
            if os.path.exists(url):
                with open(url, "rb") as f:
                    return f.read()
            return None
        status, _, body = await self._request("get", url)
        if status == 200 and body is not None:
            return body
        logger.error(f"[Picaes] 下载图片HTTP错误: {status}")
        return None

    # ---------- API 调用 ----------

    async def _call_api(self, image_bytes: bytes, level: int, key: str, mode: str) -> tuple:
        # 检测格式
        if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            ext, mime = "png", "image/png"
        elif image_bytes[:3] == b"\xff\xd8\xff":
            ext, mime = "jpg", "image/jpeg"
        else:
            ext, mime = "png", "image/png"

        logger.info(f"[Picaes] → API: {len(image_bytes)}字节, 格式={ext}")
        try:
            data = aiohttp.FormData()
            data.add_field("image", image_bytes, filename=f"image.{ext}", content_type=mime)
            data.add_field("level", str(level))
            data.add_field("key", key)
            data.add_field("mode", mode)
            status, headers, body = await self._request(
                "post", self.api_url, data=data,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
            if status == 0 or body is None:
                return None, "无法连接API服务器"
            if status == 200:
                ct = headers.get("Content-Type", "")
                if "application/json" in ct:
                    try:
                        err = json.loads(body)
                        return None, err.get("error", body.decode())
                    except Exception:
                        return None, body.decode()
                return body, None
            else:
                try:
                    err = json.loads(body)
                    return None, err.get("error", body.decode())
                except Exception:
                    return None, f"HTTP {status}"
        except Exception as e:
            logger.error(f"[Picaes] API请求异常: {e}")
            return None, str(e)

    # ---------- 主处理流程 ----------

    async def _process(self, event: AstrMessageEvent, mode: str):
        mode_name = "加密" if mode == "encrypt" else "解密"

        prompt = event.message_str.strip()
        parts = prompt.split(maxsplit=1)
        args_text = parts[1] if len(parts) > 1 else ""
        level, key = self._parse_args(args_text)

        img_comp = self._find_image_component(event.message_obj.message)
        if not img_comp:
            for seg in event.message_obj.message:
                if isinstance(seg, Reply):
                    img_comp = self._find_image_component(seg.chain)
                    if img_comp:
                        break
        if not img_comp:
            yield event.plain_result(
                f"请发送图片！用法：\n"
                f"1. 直接发送「{mode_name}」并附带图片\n"
                f"2. 回复一张图片并发送「{mode_name} [等级] [密钥]」\n\n"
                f"等级：1-10（默认4），密钥：自定义（默认 tool.hadsky.com）"
            )
            return

        yield event.plain_result(f"正在{mode_name}图片（等级:{level}，密钥:{key}），请稍候...")

        # ① 下载原始字节
        t0 = time.monotonic()
        image_bytes = await self._download_image_bytes(img_comp)
        if not image_bytes:
            yield event.plain_result("下载图片失败，请重新发送图片试试。")
            return
        t1 = time.monotonic()
        logger.info(f"[Picaes] ① 下载: {t1 - t0:.1f}s, {len(image_bytes)}字节")

        # ② 加解密
        result_bytes = None
        used = ""

        if self.process_mode == 2:
            result_bytes, error = await self._call_api(image_bytes, level, key, mode)
            used = "API"
            if result_bytes is None:
                yield event.plain_result(f"API{mode_name}失败：{error}")
                return
        elif self.process_mode == 1:
            result_bytes = await asyncio.to_thread(
                process_image_local, image_bytes, level, key, mode
            )
            used = "本地"
        else:
            try:
                result_bytes = await asyncio.to_thread(
                    process_image_local, image_bytes, level, key, mode
                )
                used = "本地"
            except Exception as e:
                logger.warning(f"[Picaes] 本地失败，回退API: {e}")
                result_bytes, error = await self._call_api(image_bytes, level, key, mode)
                used = "API"
                if result_bytes is None:
                    yield event.plain_result(f"{mode_name}失败：{error}")
                    return

        t2 = time.monotonic()
        logger.info(f"[Picaes] ② 处理: {t2 - t1:.1f}s, {len(result_bytes)}字节 [{used}]")

        # ③ 保存到 plugin_data，原样写入
        result_path = _save_result(result_bytes)
        t3 = time.monotonic()
        logger.info(f"[Picaes] ③ 保存: {t3 - t2:.1f}s → {result_path}")

        # ④ 发送
        yield event.chain_result([Image.fromFileSystem(result_path)])
        t4 = time.monotonic()
        logger.info(f"[Picaes] ④ 发送: {t4 - t3:.1f}s, 总耗时: {t4 - t0:.1f}s")

    @filter.command("加密")
    async def encrypt(self, event: AstrMessageEvent):
        async for result in self._process(event, "encrypt"):
            yield result

    @filter.command("解密")
    async def decrypt(self, event: AstrMessageEvent):
        async for result in self._process(event, "decrypt"):
            yield result
