import os
import io
import json
import ssl
import time
import uuid
import asyncio
import tempfile
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


def _detect_format(data: bytes) -> str:
    """通过魔数检测图片格式"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[:2] == b"BM":
        return "BMP"
    return "PNG"


# ==================== Gilbert 曲线算法（singularpoint.cn） ====================

def _gilbert2d(width: int, height: int) -> list:
    """广义希尔伯特（Gilbert）空间填充曲线，返回 (x,y) 坐标序列"""
    coords = []
    if width >= height:
        _gilbert_gen(0, 0, width, 0, 0, height, coords)
    else:
        _gilbert_gen(0, 0, 0, height, width, 0, coords)
    return coords


def _gilbert_gen(x, y, ax, ay, bx, by, coords):
    w = abs(ax + ay)
    h = abs(bx + by)
    dax = (1 if ax > 0 else -1) if ax != 0 else 0
    day = (1 if ay > 0 else -1) if ay != 0 else 0
    dbx = (1 if bx > 0 else -1) if bx != 0 else 0
    dby = (1 if by > 0 else -1) if by != 0 else 0

    if h == 1:
        for _ in range(w):
            coords.append((x, y))
            x += dax; y += day
        return
    if w == 1:
        for _ in range(h):
            coords.append((x, y))
            x += dbx; y += dby
        return

    ax2, ay2 = ax // 2, ay // 2
    bx2, by2 = bx // 2, by // 2
    w2 = abs(ax2 + ay2)
    h2 = abs(bx2 + by2)

    if 2 * w > 3 * h:
        if w2 % 2 and w > 2:
            ax2 += dax; ay2 += day
        _gilbert_gen(x, y, ax2, ay2, bx, by, coords)
        _gilbert_gen(x + ax2, y + ay2, ax - ax2, ay - ay2, bx, by, coords)
    else:
        if h2 % 2 and h > 2:
            bx2 += dbx; by2 += dby
        _gilbert_gen(x, y, bx2, by2, ax2, ay2, coords)
        _gilbert_gen(x + bx2, y + by2, ax, ay, bx - bx2, by - by2, coords)
        _gilbert_gen(x + (ax - dax) + (bx2 - dbx), y + (ay - day) + (by2 - dby),
                     -bx2, -by2, -(ax - ax2), -(ay - ay2), coords)


def process_image_curve(image_bytes: bytes, mode: str) -> bytes:
    """
    Gilbert 曲线加密/解密（与 singularpoint.cn 算法一致）。
    沿空间填充曲线遍历像素，用黄金比例偏移重排。
    压缩抗性强，JPEG 压缩后仍可还原。
    """
    import numpy as np

    img = PILImage.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size
    pixels = np.array(img)  # (h, w, 4)

    curve = _gilbert2d(w, h)
    total = w * h
    offset = round(((5 ** 0.5) - 1) / 2 * total)  # 黄金比例偏移

    result = np.zeros_like(pixels)

    if mode == "encrypt":
        for i in range(total):
            ox, oy = curve[i]
            nx, ny = curve[(i + offset) % total]
            result[ny, nx] = pixels[oy, ox]
    else:
        for i in range(total):
            ox, oy = curve[i]
            nx, ny = curve[(i + offset) % total]
            result[oy, ox] = pixels[ny, nx]

    out = PILImage.fromarray(result, "RGBA")
    buf = io.BytesIO()
    if mode == "encrypt":
        # 加密：JPEG quality 1（极小体积，抗压缩）
        out.convert("RGB").save(buf, format="JPEG", quality=1)
    else:
        # 解密：PNG 无损输出（保留完整画质）
        out.save(buf, format="PNG")
    return buf.getvalue()


def process_image_local(image_bytes: bytes, level: int, key: str, mode: str) -> bytes:
    """本地图片加解密，输出 JPEG"""
    img = PILImage.open(io.BytesIO(image_bytes))
    img_rgba = img.convert("RGBA")
    img_w, img_h = img_rgba.size

    n = level * 10
    canvas_w = (img_w // n) * n
    canvas_h = (img_h // n) * n
    block_w = canvas_w // n
    block_h = canvas_h // n

    if canvas_w != img_w or canvas_h != img_h:
        img_rgba = img_rgba.crop((0, 0, canvas_w, canvas_h))

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

    result = PILImage.new("RGBA", (canvas_w, canvas_h))
    for i, block in enumerate(blocks):
        row, col = divmod(i, n)
        result.paste(block, (col * block_w, row * block_h))

    # RGBA 直接输出 PNG（无损），保留完整画质
    buf = io.BytesIO()
    result.save(buf, format="PNG")
    return buf.getvalue()


# ==================== 文件保存 / 压缩 ====================

def _save_file(data: bytes, ext: str) -> str:
    """保存到 plugin_data，返回路径"""
    save_dir = os.path.join(get_astrbot_plugin_data_path(), "astrbot_plugin_picaes")
    os.makedirs(save_dir, exist_ok=True)
    filename = f"picaes_{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"
    path = os.path.join(save_dir, filename)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _compress_for_send(data: bytes, max_dim: int = 1280, quality: int = 80) -> bytes:
    """压缩图片：限制最大边 + JPEG 压缩"""
    try:
        img = PILImage.open(io.BytesIO(data))
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)
        if img.mode == "RGBA":
            bg = PILImage.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception as e:
        logger.warning(f"[Picaes] 压缩失败: {e}")
        return data


def _make_zip(image_bytes: bytes, password: str, filename: str = "result.jpg") -> bytes:
    """创建 ZIP（有密码则 AES-256 加密，无密码则普通压缩）"""
    import pyzipper
    buf = io.BytesIO()
    if password:
        with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(password.encode("utf-8"))
            zf.writestr(filename, image_bytes)
    else:
        with pyzipper.AESZipFile(buf, "w", compression=pyzipper.ZIP_DEFLATED) as zf:
            zf.writestr(filename, image_bytes)
    return buf.getvalue()


def _make_pdf(image_bytes: bytes, password: str) -> bytes:
    """创建加密 PDF（嵌入图片），返回 pdf 字节"""
    import fitz  # pymupdf
    img = PILImage.open(io.BytesIO(image_bytes))
    if img.mode == "RGBA":
        bg = PILImage.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    # 临时保存图片供 pymupdf 读取
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        img.save(tmp, format="JPEG", quality=90)
        tmp_path = tmp.name
    try:
        img_doc = fitz.open(tmp_path)
        pdf_bytes = img_doc.convert_to_pdf()
        img_doc.close()
        pdf_doc = fitz.open("pdf", pdf_bytes)
        if password:
            result = pdf_doc.tobytes(
                encryption=fitz.PDF_ENCRYPT_AES_256,
                owner_pw=password,
                user_pw=password,
                permissions=fitz.PDF_PERM_ACCESSIBILITY,
            )
        else:
            result = pdf_doc.tobytes()
        pdf_doc.close()
        return result
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ==================== 插件主体 ====================

@register(
    "astrbot_plugin_picaes",
    "qiscard",
    "图片马赛克加解密，支持自定义等级/密钥，支持图片/链接/PDF/ZIP多种发送方式",
    "3.1.0",
    "astrbot_plugin_picaes",
)
class PicaesPlugin(Star):

    # label → value 映射（WebUI 存储的是 label 文本）
    _FORMAT_MAP = {"直接发送图片": "image", "返回图床链接": "link", "加密PDF文件": "pdf", "加密ZIP文件": "zip"}
    _MODE_MAP = {"本地优先+API回退": 0, "仅本地处理": 1, "仅API上传处理": 2, "API URL模式(推荐)": 3}

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        config = config or {}
        fmt_raw = str(config.get("send_format", "image") or "image")
        self.send_format = self._FORMAT_MAP[fmt_raw] if fmt_raw in self._FORMAT_MAP else fmt_raw
        self.enable_encryption = config.get("enable_encryption", True)
        self.file_password = config.get("file_password", "123")
        self.send_password = config.get("send_password", True)
        self.enable_compress = config.get("enable_compress", True)
        raw_mode = config.get("process_mode", "0")
        self.process_mode = self._MODE_MAP.get(raw_mode, int(raw_mode) if str(raw_mode).isdigit() else 0)
        self.api_url = config.get("api_url", "https://picace.995456.xyz/api/process")
        self.default_key = config.get("default_key", "picaes")
        self.default_level = config.get("default_level", 4)
        self.timeout = config.get("timeout", 120)

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
            logger.warning("[Picaes] 图片组件无url也无file字段")
            return None
        logger.info(f"[Picaes] 下载: {url[:120]}")
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
            logger.info(f"[Picaes] HTTP下载: {len(body)}字节")
            return body
        logger.warning(f"[Picaes] 下载失败: HTTP {status}")
        return None

    # ---------- API ----------

    async def _call_api_url(self, image_url: str, level: int, key: str, mode: str) -> tuple:
        """URL模式：传图片URL给API，API处理后上传图床返回链接"""
        from urllib.parse import quote
        api_base = self.api_url.replace("/api/proxy", "/api/process")
        url = f"{api_base}?url={quote(image_url, safe='')}&level={level}&key={quote(key, safe='')}&mode={mode}"
        logger.info(f"[Picaes] API(URL): {url[:150]}")
        try:
            status, _, body = await self._request("get", url, timeout=aiohttp.ClientTimeout(total=self.timeout))
            if status == 0 or body is None:
                return None, "无法连接API"
            if status == 200:
                result = json.loads(body)
                if result.get("success") and result.get("url"):
                    return result["url"], None
                return None, result.get("error", "API返回异常")
            return None, f"HTTP {status}"
        except Exception as e:
            return None, str(e)

    async def _call_api(self, image_bytes: bytes, level: int, key: str, mode: str) -> tuple:
        """上传模式：发送图片文件给API处理"""
        fmt = _detect_format(image_bytes)
        ext = {"PNG": "png", "JPEG": "jpg"}.get(fmt, "png")
        mime = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(fmt, "image/png")
        logger.info(f"[Picaes] API(上传): {len(image_bytes)}字节 {ext}")
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
                return None, "无法连接API"
            if status == 200:
                ct = headers.get("Content-Type", "")
                if "application/json" in ct:
                    err = json.loads(body)
                    return None, err.get("error", body.decode())
                return body, None
            return None, f"HTTP {status}"
        except Exception as e:
            return None, str(e)

    # ---------- 结果发送 ----------

    async def _deliver_result(self, event: AstrMessageEvent, result_bytes: bytes,
                              mode_name: str, send_fmt: str, is_encrypt: bool = False):
        """
        根据配置发送结果。
        send_fmt: "image"/"pdf"/"zip"/"link"
        is_encrypt: 加密模式下图片不二次压缩（保护像素块完整性）
        """
        encrypted = self.enable_encryption
        password = self.file_password if encrypted else ""

        # --- 图片直发 ---
        if send_fmt == "image":
            # 加密数据不压缩（保护像素块）；解密时按配置决定是否压缩
            if is_encrypt or not self.enable_compress:
                send_bytes = result_bytes
            else:
                send_bytes = await asyncio.to_thread(_compress_for_send, result_bytes)
            send_path = _save_file(send_bytes, "jpg")
            logger.info(f"[Picaes] 发送图片: {send_path} ({len(send_bytes)}字节)")
            yield event.chain_result([Image.fromFileSystem(send_path)])
            return

        # --- 图床链接 ---
        if send_fmt == "link":
            yield event.plain_result("链接模式需配合 process_mode=3(URL模式) 使用。")
            return

        # --- PDF ---
        if send_fmt == "pdf":
            try:
                pdf_bytes = await asyncio.to_thread(_make_pdf, result_bytes, password)
            except ImportError:
                yield event.plain_result("pymupdf 未安装，请执行: pip install pymupdf")
                return
            except Exception as e:
                logger.error(f"[Picaes] PDF生成失败: {e}", exc_info=True)
                yield event.plain_result(f"PDF生成失败：{e}")
                return
            suffix = f"_encrypted_{password}" if encrypted else ""
            pdf_path = _save_file(pdf_bytes, "pdf")
            fname = f"{mode_name}_result{suffix}.pdf"
            logger.info(f"[Picaes] 发送PDF: {fname} ({len(pdf_bytes)}字节)")
            yield event.chain_result([File(name=fname, file=pdf_path)])
            if encrypted:
                if self.send_password:
                    yield event.plain_result(f"密码：{password}")
                else:
                    yield event.plain_result("文件已加密，请联系发送者获取密码。")
            return

        # --- ZIP ---
        if send_fmt == "zip":
            try:
                zip_bytes = await asyncio.to_thread(_make_zip, result_bytes, password)
            except ImportError:
                yield event.plain_result("pyzipper 未安装，请执行: pip install pyzipper")
                return
            except Exception as e:
                logger.error(f"[Picaes] ZIP生成失败: {e}", exc_info=True)
                yield event.plain_result(f"ZIP生成失败：{e}")
                return
            suffix = f"_encrypted_{password}" if encrypted else ""
            zip_path = _save_file(zip_bytes, "zip")
            fname = f"{mode_name}_result{suffix}.zip"
            logger.info(f"[Picaes] 发送ZIP: {fname} ({len(zip_bytes)}字节)")
            yield event.chain_result([File(name=fname, file=zip_path)])
            if encrypted:
                if self.send_password:
                    yield event.plain_result(f"密码：{password}")
                else:
                    yield event.plain_result("文件已加密，请联系发送者获取密码。")
            return

    # ---------- 主处理流程 ----------

    async def _process(self, event: AstrMessageEvent, mode: str, force_format: str | None = None):
        """
        mode: "encrypt" / "decrypt"
        force_format: None(加密默认图片/解密用配置) / "image" / "pdf" / "zip"
        """
        mode_name = "加密" if mode == "encrypt" else "解密"
        is_encrypt = (mode == "encrypt")
        # 加密默认发图片（加密数据不适合转PDF/ZIP），解密按配置
        send_fmt = force_format if force_format else ("image" if is_encrypt else (self.send_format or "image"))
        try:
            prompt = event.message_str.strip()
            parts = prompt.split(maxsplit=1)
            args_text = parts[1] if len(parts) > 1 else ""
            level, key = self._parse_args(args_text)
            fmt_label = {"image": "图片", "pdf": "PDF", "zip": "ZIP", "link": "链接"}.get(send_fmt, send_fmt)
            logger.info(f"[Picaes] === {mode_name}({fmt_label}) === 等级:{level} 密钥:{key}")

            # 查找图片组件
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
                    f"「{mode_name} [等级] [密钥]」并附带图片或回复图片\n"
                    f"等级：1-10（默认{self.default_level}），密钥：默认 {self.default_key}"
                )
                return

            yield event.plain_result(f"正在{mode_name}（等级:{level}，密钥:{key}，格式:{fmt_label}），请稍候...")
            t0 = time.monotonic()

            # URL 模式（process_mode=3）
            if self.process_mode == 3:
                img_url = img_comp.url or img_comp.file
                if not img_url or not img_url.startswith("http"):
                    yield event.plain_result("URL模式需要图片为HTTP链接。")
                    return
                link, error = await self._call_api_url(img_url, level, key, mode)
                if link is None:
                    yield event.plain_result(f"API{mode_name}失败：{error}")
                    return
                yield event.plain_result(f"{mode_name}完成：{link}")
                return

            # 下载
            image_bytes = await self._download_image_bytes(img_comp)
            if not image_bytes:
                yield event.plain_result("下载图片失败，请重试。")
                return
            logger.info(f"[Picaes] 下载: {time.monotonic() - t0:.1f}s, {len(image_bytes)}字节")

            # 处理
            t1 = time.monotonic()
            result_bytes = None
            used = ""

            if self.process_mode == 2:
                result_bytes, error = await self._call_api(image_bytes, level, key, mode)
                used = "API"
                if result_bytes is None:
                    yield event.plain_result(f"API{mode_name}失败：{error}")
                    return
            elif self.process_mode == 1:
                result_bytes = await asyncio.to_thread(process_image_local, image_bytes, level, key, mode)
                used = "本地"
            else:
                try:
                    result_bytes = await asyncio.to_thread(process_image_local, image_bytes, level, key, mode)
                    used = "本地"
                except Exception as e:
                    logger.warning(f"[Picaes] 本地失败，回退API: {e}")
                    result_bytes, error = await self._call_api(image_bytes, level, key, mode)
                    used = "API"
                    if result_bytes is None:
                        yield event.plain_result(f"{mode_name}失败：{error}")
                        return

            logger.info(f"[Picaes] 处理: {time.monotonic() - t1:.1f}s, {len(result_bytes)}字节 [{used}]")

            # 保存完整结果（调试用）
            result_ext = _detect_format(result_bytes).lower()
            _save_file(result_bytes, result_ext if result_ext in ("jpg", "png") else "jpg")

            # 临时覆盖 send_format 发送
            # 发送
            async for r in self._deliver_result(event, result_bytes, mode_name, send_fmt, is_encrypt=is_encrypt):
                yield r

            logger.info(f"[Picaes] === {mode_name}结束 === {time.monotonic() - t0:.1f}s")

        except Exception as e:
            logger.error(f"[Picaes] {mode_name}异常: {e}", exc_info=True)
            yield event.plain_result(f"{mode_name}出错：{e}")

    # ---------- 指令注册 ----------

    @filter.command("加密1")
    async def encrypt(self, event: AstrMessageEvent):
        async for r in self._process(event, "encrypt"):
            yield r

    @filter.command("解密1")
    async def decrypt(self, event: AstrMessageEvent):
        async for r in self._process(event, "decrypt"):
            yield r

    @filter.command("解密1pdf")
    async def decrypt_pdf(self, event: AstrMessageEvent):
        async for r in self._process(event, "decrypt", force_format="pdf"):
            yield r

    @filter.command("解密1zip")
    async def decrypt_zip(self, event: AstrMessageEvent):
        async for r in self._process(event, "decrypt", force_format="zip"):
            yield r

    @filter.command("图解帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(
            "📖 图片加解密工具 v3.1 帮助\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔒 算法1（像素块打乱）：\n"
            "  加密1 [等级] [密钥] — 加密图片\n"
            "  解密1 [等级] [密钥] — 解密图片\n"
            "  解密1pdf / 解密1zip — 解密后转格式\n\n"
            "🌀 算法2（Gilbert曲线，抗压缩）：\n"
            "  加密2 — 曲线混淆，输出极小JPEG\n"
            "  解密2 — 曲线还原，输出PNG无损\n"
            "  解密2pdf / 解密2zip — 解密后转格式\n\n"
            "⚙️ 参数：\n"
            f"  等级：1-10（默认{self.default_level}）\n"
            f"  密钥：自定义（默认 {self.default_key}）\n\n"
            "💡 发送指令附带图片，或回复图片后发送指令"
        )

    # ---------- 算法2：Gilbert 曲线命令 ----------

    async def _process_curve(self, event: AstrMessageEvent, mode: str, force_format: str | None = None):
        """Gilbert 曲线加密/解密，复用算法1的发送逻辑"""
        mode_name = "加密2" if mode == "encrypt" else "解密2"
        is_encrypt = (mode == "encrypt")
        send_fmt = force_format if force_format else ("image" if is_encrypt else (self.send_format or "image"))
        fmt_label = {"image": "图片", "pdf": "PDF", "zip": "ZIP"}.get(send_fmt, send_fmt)

        img_comp = self._find_image_component(event.message_obj.message)
        if not img_comp:
            for seg in event.message_obj.message:
                if isinstance(seg, Reply):
                    img_comp = self._find_image_component(seg.chain)
                    if img_comp:
                        break
        if not img_comp:
            yield event.plain_result(f"请发送图片！用法：回复图片或附带图片发送「{mode_name}」")
            return

        yield event.plain_result(f"正在{mode_name}（{fmt_label}），请稍候...")
        t0 = time.monotonic()

        image_bytes = await self._download_image_bytes(img_comp)
        if not image_bytes:
            yield event.plain_result("下载图片失败，请重试。")
            return
        logger.info(f"[Picaes] {mode_name} 下载: {time.monotonic() - t0:.1f}s, {len(image_bytes)}字节")

        t1 = time.monotonic()
        result_bytes = await asyncio.to_thread(process_image_curve, image_bytes, mode)
        logger.info(f"[Picaes] {mode_name} 处理: {time.monotonic() - t1:.1f}s, {len(result_bytes)}字节")

        # 复用算法1的发送逻辑（图片/PDF/ZIP）
        async for r in self._deliver_result(event, result_bytes, mode_name, send_fmt, is_encrypt=is_encrypt):
            yield r

        logger.info(f"[Picaes] {mode_name} 完成: {time.monotonic() - t0:.1f}s")

    @filter.command("加密2")
    async def obfuscate(self, event: AstrMessageEvent):
        async for r in self._process_curve(event, "encrypt"):
            yield r

    @filter.command("解密2")
    async def restore(self, event: AstrMessageEvent):
        async for r in self._process_curve(event, "decrypt"):
            yield r

    @filter.command("解密2pdf")
    async def restore_pdf(self, event: AstrMessageEvent):
        async for r in self._process_curve(event, "decrypt", force_format="pdf"):
            yield r

    @filter.command("解密2zip")
    async def restore_zip(self, event: AstrMessageEvent):
        async for r in self._process_curve(event, "decrypt", force_format="zip"):
            yield r
