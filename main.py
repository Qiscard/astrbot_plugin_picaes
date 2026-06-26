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
    "1.0.6",
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

    async def _request(self, method: str, url: str, **kwargs) -> tuple:
        """
        统一网络请求，自带两层 SSL 回退。
        返回 (status, headers, body_bytes)，失败返回 (0, {}, None)。
        在 session 内读完 body 再返回，避免连接关闭后无法读取。
        """
        # 第一层：certifi CA 证书
        try:
            ssl_ctx = ssl.create_default_context(cafile=certifi.where())
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            async with aiohttp.ClientSession(trust_env=True, connector=connector) as session:
                async with session.request(method, url, **kwargs) as resp:
                    body = await resp.read()
                    return resp.status, dict(resp.headers), body
        except (aiohttp.ClientConnectorSSLError,
                aiohttp.ClientConnectorCertificateError):
            pass
        except Exception as e:
            logger.error(f"[Picaes] 请求失败({url}): {e}")
            return 0, {}, None

        # 第二层：关闭证书验证
        try:
            logger.warning(f"[Picaes] SSL验证失败，回退: {url}")
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, ssl=ssl_ctx, **kwargs) as resp:
                    body = await resp.read()
                    return resp.status, dict(resp.headers), body
        except Exception as e:
            logger.error(f"[Picaes] 回退请求也失败({url}): {e}")
            return 0, {}, None

    async def _download_image_bytes(self, img_comp: Image) -> bytes | None:
        """下载图片原始字节，绕过 AstrBot 的 temp 文件机制。"""
        url = img_comp.url or img_comp.file
        if not url:
            return None

        # base64 / 本地文件：直接读取
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

    async def _call_api(
        self, image_bytes: bytes, level: int, key: str, mode: str
    ) -> tuple:
        """
        调用加解密API，发送原始字节。
        返回 (result_bytes, error_msg)：成功 (bytes, None)，失败 (None, "错误描述")
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

            status, headers, body = await self._request(
                "post", self.api_url,
                data=data,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )

            if status == 0 or body is None:
                return None, "无法连接API服务器"

            ct = headers.get("Content-Type", "")

            if status == 200:
                if "application/json" in ct:
                    logger.error(f"[Picaes] API返回JSON而非图片: {body[:200]}")
                    try:
                        err = json.loads(body)
                        return None, err.get("error", body.decode("utf-8", errors="replace"))
                    except (json.JSONDecodeError, AttributeError):
                        return None, body.decode("utf-8", errors="replace")

                logger.info(f"[Picaes] ← API: {len(body)}字节, Content-Type={ct}")
                return body, None
            else:
                logger.error(f"[Picaes] API错误 {status}: {body[:200]}")
                try:
                    err = json.loads(body)
                    return None, err.get("error", body.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, AttributeError):
                    return None, f"HTTP {status}"

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
