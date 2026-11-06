# -*- coding: utf-8 -*-
"""
iconfont.cn 登录管理器
使用 Playwright 浏览器自动化完成登录，管理 EGG_SESS_ICONFONT Cookie。

设计说明：
- 该模块负责在进程内维护登录态，避免每次调用都重复登录。
- Cookie 只保存在内存中，适合本地 MCP Server 的短连接会话场景。
- 登录成功后，后续 API 请求可直接从模块变量读取 Cookie。
"""

import os
import time
import logging
import asyncio
from typing import Optional

logger = logging.getLogger(__name__)

# 模块级 Cookie 存储 (进程生命周期)
# 说明：这些变量在当前 Python 进程中持续保留，适合单机 MCP Server 使用。
# 如果进程重启，登录状态会丢失，需要重新登录或从环境变量读取 Cookie。
_cookie: Optional[str] = None
_username: Optional[str] = None
_login_time: Optional[float] = None


def get_cookie() -> Optional[str]:
    """获取当前存储的 Cookie。"""
    return _cookie


def set_cookie(cookie: str, username: str = "") -> None:
    """设置 Cookie 和登录信息。

    参数:
        cookie: iconfont 的 EGG_SESS_ICONFONT 值
        username: 登录时使用的手机号/账号，便于排查问题
    """
    global _cookie, _username, _login_time
    _cookie = cookie
    _username = username
    _login_time = time.time()


def clear_cookie() -> None:
    """清除 Cookie 和登录信息。

    适用场景：
    - 用户主动退出登录
    - 登录失败时重置状态
    - 需要更换账号时清理旧 Cookie
    """
    global _cookie, _username, _login_time
    _cookie = None
    _username = None
    _login_time = None


def is_logged_in() -> bool:
    """检查是否已登录 (Cookie 是否存在)。"""
    return _cookie is not None


def get_login_info() -> dict:
    """获取当前登录状态信息。

    返回示例：
        {
            "logged_in": True,
            "username": "13800138000",
            "login_time": 1700000000.0,
        }
    """
    return {
        "logged_in": _cookie is not None,
        "username": _username,
        "login_time": _login_time,
    }


def extract_cookie_from_page(cookies: list) -> Optional[str]:
    """从浏览器 cookies 列表中提取 EGG_SESS_ICONFONT。

    Args:
        cookies: Playwright 返回的 cookie 列表，元素通常包含 name/value 等字段

    Returns:
        提取到的 EGG_SESS_ICONFONT 值；未找到时返回 None
    """
    for c in cookies:
        if c.get("name") == "EGG_SESS_ICONFONT":
            return c.get("value")
    return None


async def login_with_playwright(phone: str, password: str) -> dict:
    """
    使用 Playwright 无头浏览器自动登录 iconfont.cn。

    处理完整登录流程: 填充表单 → 勾选隐私协议 → 提交登录 → 提取 Cookie

    Args:
        phone: 手机号
        password: 密码

    Returns:
        dict: {success, message, cookie?, need_manual?}
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "success": False,
            "message": "playwright 未安装，请运行: pip install playwright && python -m playwright install chromium",
        }

    # 处理前先去掉首尾空白，避免环境变量中含有空格导致登录失败。
    phone = phone.strip()
    password = password.strip()

    if not phone or not password:
        return {
            "success": False,
            "message": "手机号或密码为空，请检查环境变量 ICONFONT_PHONE 和 ICONFONT_PASSWORD",
        }

    logger.info("启动无头浏览器...")
    browser = None

    try:
        # 1) 创建 Playwright 实例并启动 Chromium 浏览器
        # 这里使用 headless=True，避免弹出 GUI 窗口，适合服务器端自动化脚本。
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)

        # 2) 创建新的浏览器上下文，设置视口和 UA，尽量模拟真实浏览器环境
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        logger.info("打开 iconfont.cn 登录页...")
        await page.goto("https://www.iconfont.cn/login", wait_until="networkidle", timeout=30000)

        # 等待表单加载，确保登录元素已经出现，避免直接填充时元素不存在。
        await page.wait_for_selector("#userid", timeout=10000)
        await page.wait_for_selector("#password", timeout=10000)

        # 3) 填写手机号和密码
        await page.fill("#userid", phone)
        logger.info("已填充手机号")
        await page.click("#password")
        await page.fill("#password", password)
        logger.info("已填充密码")

        # 4) 处理隐私协议勾选框
        # iconfont 登录页可能会在点击登录时弹出协议确认框，必须先勾选后才可提交。
        checkbox = await page.query_selector("#register-agreement")
        if checkbox:
            is_visible = await checkbox.is_visible()
            if not is_visible:
                # 协议框可能被隐藏，需要先点击一次登录按钮触发它
                logger.info("点击登录按钮以触发协议勾选...")
                await page.click(".mx-btn-submit")
                await page.wait_for_timeout(1500)

            # 勾选协议
            await page.check("#register-agreement")
            logger.info("已勾选隐私协议")
        else:
            logger.info("未找到协议勾选框，可能不需要")

        # 5) 检查是否需要验证码
        # 这类页面常见极验/滑块验证码，若出现则无法通过程序自动验证，须人工处理。
        captcha_el = await page.query_selector(".nc_wrapper, .captcha, .geetest_panel, .verify-code, .slider, #nc_1_wrapper")
        if captcha_el:
            is_visible = await captcha_el.is_visible()
            if is_visible:
                await browser.close()
                return {
                    "success": False,
                    "need_manual": True,
                    "message": (
                        "iconfont.cn 登录出现滑块验证码，无法自动完成。\n"
                        "请在 MCP 配置中设置 ICONFONT_COOKIE 环境变量代替:\n"
                        "1. 浏览器访问 https://www.iconfont.cn/ 并登录\n"
                        "2. 按 F12 → Application → Cookies\n"
                        "3. 复制 EGG_SESS_ICONFONT 的值"
                    ),
                }

        # 6) 提交登录表单
        await page.click(".mx-btn-submit")
        logger.info("已点击登录按钮")

        # 7) 轮询等待 Cookie，最多等待 30 秒
        # 登录成功后，iconfont 会写入 EGG_SESS_ICONFONT Cookie，用于后续 API 请求鉴权。
        cookie_value = None
        for i in range(30):
            await asyncio.sleep(1)
            cookies = await context.cookies()
            cookie_value = extract_cookie_from_page(cookies)
            if cookie_value:
                logger.info(f"Cookie 获取成功 (等待 {i + 1}s)")
                break

        if cookie_value:
            # 成功获取 Cookie 后写入全局状态，后续直接复用，不必重复登录。
            set_cookie(cookie_value, username=phone)
            await browser.close()
            return {
                "success": True,
                "message": "登录成功",
            }

        # 8) 如果没有 cookie，则检查登录报错信息，区分密码错误或其他错误
        await page.wait_for_timeout(2000)
        error_el = await page.query_selector(".error-msg, .mx-form-item-error, .login-error, #register-agreement-error")
        if error_el:
            try:
                error_text = await error_el.inner_text()
            except Exception:
                error_text = "未知错误"

            await browser.close()
            return {
                "success": False,
                "message": f"登录失败: {error_text}",
            }

        await browser.close()
        return {
            "success": False,
            "message": "登录后未获取到 Cookie，请检查账号密码是否正确，或改用 ICONFONT_COOKIE 方式",
        }

    except Exception as e:
        logger.error(f"登录异常: {e}")
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        return {
            "success": False,
            "message": f"登录异常: {str(e)}",
        }
