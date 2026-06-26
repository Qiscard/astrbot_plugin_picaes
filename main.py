import os
import json
import ssl
import time
import uuid
import aiohttp
import certifi
from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.utils.io import get_astrbot_temp_path


# 图片格式魔数（文件头前几个字节）→ 对应扩展名和 MIME
_FORMAT_MAP = {
    b"\x89PNG\r\n\x1a\n":  ("png",  "image/png"),
    b"\xff\xd8\xff":        ("jpg",  "image/jpeg"),
    b"GIF87a":              ("gif",  "image/gif"),
    b"GIF89a":              ("gif",  "image/gif"),
    b"RIFF":                ("webp", "image/webp"),   # RIFF....WEBP
}


def _detect_image_format(data: bytes) -> tuple:
    """从文件头检测真实图片格式，返回 (扩展名, MIME类型)"""
    for magic, fmt in _FORMAT_MAP.items():
        if data[:len(magic)] == magic:
            return fmt
    # 默认当 PNG 处理（无损，不会丢数据）
    return ("png", "image/png")


def _save_result_to_file(data: bytes, ext: str) -> str:
    """把结果字节直接写入临时文件，跳过 base64 编码，返回文件路径"""
    temp_dir = get_astrbot_temp_path()
    filename = f"picaes_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"
    path = os.path.join(temp_dir, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


@register(
    "astrbot_plugin_picaes",
    "AstrBotUser",
    "通过API对图片进行马赛克加密/解密，支持自定义加密等级和密钥",
    "1.0.4",
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

    def _find_image_component(self, chain) -> Image | None:
        """从消息链中查找图片组件"""
        for seg in chain:
            if isinstance(seg, Image):
                return seg
        return None

    def _parse_args(self, text: str) -> tuple:
        """解析命令参数，返回 (level, key)"""
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

    async def _download_image_bytes(self, img_comp: Image) -> bytes | None:
        """
        直接下载图片原始字节，绕过 AstrBot 的 temp 文件机制。
        用与 AstrBot 相同的 SSL 配置，保证网络兼容性。
        """
        url = img_comp.url or img_comp.file
        if not url:
            return None

        # 对 base64 类型，直接解码（数据已经在本地，零损耗）
        if url.startswith("base64://"):
            import base64
            return base64.b64decode(url.removeprefix("base64://"))

        # 对本地文件，直接读
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

        # HTTP/HTTPS 下载，使用 AstrBot 相同的 SSL 配置
        try:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(
                trust_env=True, connector=connector
            ) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    logger.error(f"[Picaes] 下载图片HTTP错误: {resp.status}")
                    return None
        except (aiohttp.ClientConnectorSSLError,
                aiohttp.ClientConnectorCertificateError):
            # SSL 证书验证失败时回退（与 AstrBot 行为一致）
            logger.warning("[Picaes] SSL验证失败，回退到不验证模式")
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            async with aiohttp.ClientSession() as session:
                async with session.get(url, ssl=ssl_context) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    return None
        except Exception as e:
            logger.error(f"[Picaes] 下载图片异常: {e}")
            return None

    async def _call_api(
        self, image_bytes: bytes, level: int, key: str, mode: str
    ) -> tuple:
        """
        调用加解密API，发送原始字节。
        返回 (result_bytes, error_msg)：
          成功: (bytes, None)
          失败: (None, "错误描述")
        """
        ext, mime = _detect_image_format(image_bytes)
        filename = f"image.{ext}"

        logger.info(
            f"[Picaes] → API: {len(image_bytes)}字节, 格式={ext}, "
            f"等级={level}, 模式={mode}, 密钥={key}"
        )

        try:
            data = aiohttp.FormData()
            data.add_field("image", image_bytes, filename=filename, content_type=mime)
            data.add_field("level", str(level))
            data.add_field("key", key)
            data.add_field("mode", mode)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as resp:
                    ct = resp.headers.get("Content-Type", "")

                    if resp.status == 200:
                        # 成功：返回的是图片
                        if "application/json" in ct:
                            # 200 但返回 JSON，说明其实是错误
                            body = await resp.text()
                            logger.error(f"[Picaes] API返回JSON而非图片: {body}")
                            try:
                                err = json.loads(body)
                                return None, err.get("error", body)
                            except (json.JSONDecodeError, AttributeError):
                                return None, body

                        result = await resp.read()
                        logger.info(
                            f"[Picaes] ← API: {len(result)}字节, Content-Type={ct}"
                        )
                        return result, None

                    else:
                        # 非 200：尝试解析 JSON 错误
                        body = await resp.text()
                        logger.error(f"[Picaes] API错误 {resp.status}: {body}")
                        try:
                            err = json.loads(body)
                            return None, err.get("error", body)
                        except (json.JSONDecodeError, AttributeError):
                            return None, f"HTTP {resp.status}"

        except Exception as e:
            logger.error(f"[Picaes] API请求异常: {e}")
            return None, str(e)

    async def _process(self, event: AstrMessageEvent, mode: str):
        """主处理流程"""
        mode_name = "加密" if mode == "encrypt" else "解密"

        # 解析参数
        prompt = event.message_str.strip()
        parts = prompt.split(maxsplit=1)
        args_text = parts[1] if len(parts) > 1 else ""
        level, key = self._parse_args(args_text)

        # 查找图片：当前消息 → 回复消息
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
                f"等级：1-10（默认4），密钥：自定义（默认 tool.hadsky.com）\n\n"
                f"⚠️ 请以「文件」形式发送图片（不要选相册/预览），防止平台压缩。"
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

        # ② 调用API
        result_bytes, error = await self._call_api(image_bytes, level, key, mode)
        if result_bytes is None:
            yield event.plain_result(f"图片{mode_name}失败：{error or '未知错误'}")
            return
        t2 = time.monotonic()
        logger.info(f"[Picaes] ② API处理: {t2 - t1:.1f}s, 返回{len(result_bytes)}字节")

        # ③ 保存结果到临时文件，用 fromFileSystem 直传（跳过 base64 编解码）
        ext, _ = _detect_image_format(result_bytes)
        result_path = _save_result_to_file(result_bytes, ext)
        t3 = time.monotonic()
        logger.info(f"[Picaes] ③ 保存文件: {t3 - t2:.1f}s → {result_path}")

        yield event.chain_result([Image.fromFileSystem(result_path)])
        t4 = time.monotonic()
        logger.info(f"[Picaes] ④ 发送完成: {t4 - t3:.1f}s, 总耗时: {t4 - t0:.1f}s")

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
