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
from astrbot.core.utils.io import get_astrbot_temp_path


# ==================== 本地加解密算法（与 API 版本一致） ====================

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
    """本地图片加解密，与 API 算法完全一致"""
    img = PILImage.open(io.BytesIO(image_bytes))
    img = img.convert("RGBA")
    img_w, img_h = img.size
    pixels = img.load()

    n = level * 10
    block_w = img_w // n
    block_h = img_h // n

    # 提取所有像素块
    blocks = []
    for row in range(n):
        for col in range(n):
            x0, y0 = col * block_w, row * block_h
            block = img.crop((x0, y0, x0 + block_w, y0 + block_h))
            blocks.append(block)

    # 混淆 / 解混淆
    if mode == "encrypt":
        _shuffle_encrypt(blocks, key)
    else:
        _shuffle_decrypt(blocks, key)

    # 拼回图片
    result = PILImage.new("RGBA", (img_w, img_h))
    for i, block in enumerate(blocks):
        row, col = divmod(i, n)
        result.paste(block, (col * block_w, row * block_h))

    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()


# ==================== 工具函数 ====================

_FORMAT_MAP = {
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"\xff\xd8\xff":       ("jpg", "image/jpeg"),
    b"GIF87a":             ("gif", "image/gif"),
    b"GIF89a":             ("gif", "image/gif"),
    b"RIFF":               ("webp", "image/webp"),
}


def _detect_image_format(data: bytes) -> tuple:
    for magic, fmt in _FORMAT_MAP.items():
        if data[:len(magic)] == magic:
            return fmt
    return ("png", "image/png")


_MAX_SEND_SIZE = 2 * 1024 * 1024  # 2MB，超过此大小主动压缩


def _compress_for_send(data: bytes) -> tuple:
    """
    压缩图片到目标大小以下，返回 (bytes, ext)。
    策略：先尝试 PNG optimize，仍超标则缩尺寸+转 JPEG。
    """
    if len(data) <= _MAX_SEND_SIZE:
        return data, "png"

    try:
        img = PILImage.open(io.BytesIO(data))
    except Exception:
        return data, "png"

    # 先试 PNG optimize
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= _MAX_SEND_SIZE:
        logger.info(f"[Picaes] PNG优化: {len(data)}→{buf.tell()}字节")
        return buf.getvalue(), "png"

    # 仍超标：缩尺寸到长边 2048 + 转 JPEG
    w, h = img.size
    max_side = 2048
    if max(w, h) > max_side:
        ratio = max_side / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), PILImage.LANCZOS)

    # 转 RGBA→RGB（JPEG 不支持透明通道）
    if img.mode == "RGBA":
        bg = PILImage.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    logger.info(f"[Picaes] 已压缩: {len(data)}→{buf.tell()}字节 (JPEG)")
    return buf.getvalue(), "jpg"


def _save_result_to_file(data: bytes, ext: str) -> str:
    """把结果写入临时文件，自动压缩大图"""
    data, final_ext = _compress_for_send(data)
    if final_ext != ext:
        ext = final_ext

    temp_dir = get_astrbot_temp_path()
    filename = f"picaes_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"
    path = os.path.join(temp_dir, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


# ==================== 插件主体 ====================

@register(
    "astrbot_plugin_picaes",
    "AstrBotUser",
    "通过API对图片进行马赛克加密/解密，支持自定义加密等级和密钥",
    "2.0.0",
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
        # 0=本地优先(API回退), 1=仅本地, 2=仅API
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

    # ---------- 网络请求（两层 SSL 回退） ----------

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
        ext, mime = _detect_image_format(image_bytes)
        logger.info(f"[Picaes] → API: {len(image_bytes)}字节, 格式={ext}, 等级={level}, 模式={mode}")
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
            ct = headers.get("Content-Type", "")
            if status == 200:
                if "application/json" in ct:
                    try:
                        err = json.loads(body)
                        return None, err.get("error", body.decode())
                    except Exception:
                        return None, body.decode()
                logger.info(f"[Picaes] ← API: {len(body)}字节")
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
        logger.info(f"[Picaes] ① 下载图片: {t1 - t0:.1f}s, {len(image_bytes)}字节")

        # ② 加解密处理
        result_bytes = None
        used = ""

        if self.process_mode == 2:
            # 仅 API
            result_bytes, error = await self._call_api(image_bytes, level, key, mode)
            used = "API"
            if result_bytes is None:
                yield event.plain_result(f"API{mode_name}失败：{error}")
                return
        elif self.process_mode == 1:
            # 仅本地
            result_bytes = await asyncio.to_thread(
                process_image_local, image_bytes, level, key, mode
            )
            used = "本地"
        else:
            # 本地优先，失败回退 API
            try:
                result_bytes = await asyncio.to_thread(
                    process_image_local, image_bytes, level, key, mode
                )
                used = "本地"
            except Exception as e:
                logger.warning(f"[Picaes] 本地处理失败，尝试API: {e}")
                result_bytes, error = await self._call_api(image_bytes, level, key, mode)
                used = "API"
                if result_bytes is None:
                    yield event.plain_result(f"{mode_name}失败：{error}")
                    return

        t2 = time.monotonic()
        logger.info(f"[Picaes] ② {used}处理: {t2 - t1:.1f}s, 返回{len(result_bytes)}字节")

        # ③ 保存结果
        ext, _ = _detect_image_format(result_bytes)
        result_path = _save_result_to_file(result_bytes, ext)
        t3 = time.monotonic()
        logger.info(f"[Picaes] ③ 保存文件: {t3 - t2:.1f}s")

        yield event.chain_result([Image.fromFileSystem(result_path)])
        t4 = time.monotonic()
        logger.info(f"[Picaes] ④ 发送完成: {t4 - t3:.1f}s, 总耗时: {t4 - t0:.1f}s [{used}]")

    @filter.command("加密")
    async def encrypt(self, event: AstrMessageEvent):
        """加密图片命令：加密 [等级] [密钥]"""
        async for result in self._process(event, "encrypt"):
            yield result

    @filter.command("解密")
    async def decrypt(self, event: AstrMessageEvent):
        """解密图片命令：解密 [等级] [密钥]"""
        async for result in self._process(event, "decrypt"):
            yield result
