import asyncio
import json
import os
import queue
import threading
import time
import sys
import re
import socket
from urllib.parse import urlparse
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, colorchooser
import tkinter.font as tkfont
import html

try:
    from PIL import ImageGrab, ImageTk
except Exception:
    ImageGrab = None
    ImageTk = None

try:
    import qrcode
except Exception:
    qrcode = None

try:
    import winreg
except Exception:
    winreg = None

from telethon import TelegramClient, events, utils
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
)

API_CREDENTIALS: List[Tuple[int, str]] = [
    (25257271, "9b687adaef7580a65b19fcafb786d111"),
    (24109791, "d59dd55463b6c7196a30c639b8a89e9e"),
    (27485546, "1c30a5174ca5fcd29b3f007dafd9affd"),
    (24031458, "7c96284a780197bad250af15816d40de"),
]


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = get_base_dir()
APP_DIR = BASE_DIR / "session"
APP_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_SESSION_BASENAME = "user"


def ensure_app_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_session_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return DEFAULT_SESSION_BASENAME
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name)
    name = name.strip("._")
    return name or DEFAULT_SESSION_BASENAME


def get_session_base_path(name: str) -> Path:
    ensure_app_dir()
    return APP_DIR / sanitize_session_name(name)


def get_session_file(name: str) -> Path:
    return Path(str(get_session_base_path(name)) + ".session")


def list_session_names() -> List[str]:
    ensure_app_dir()
    names = sorted({fp.stem for fp in APP_DIR.glob("*.session")}, key=lambda x: x.lower())
    if DEFAULT_SESSION_BASENAME not in names:
        names.insert(0, DEFAULT_SESSION_BASENAME)
    return names


def detect_system_proxy_settings() -> dict:
    result = {
        "enabled": False,
        "host": "",
        "port": "",
        "username": "",
        "password": "",
        "proxy_type": "auto",
        "source": "",
        "pac": "",
    }

    def parse_proxy_value(raw_value: str, source: str, default_type: str = "http"):
        if not raw_value:
            return None
        raw_value = raw_value.strip()
        if not raw_value:
            return None
        candidates = [x.strip() for x in raw_value.split(";") if x.strip()]
        chosen = raw_value
        for item in candidates:
            if "=" in item:
                k, v = item.split("=", 1)
                if k.strip().lower() in ("http", "https", "socks", "socks5"):
                    chosen = v.strip()
                    if k.strip().lower() in ("socks", "socks5"):
                        default_type = "socks5"
                    break
            else:
                chosen = item
                break
        if "://" not in chosen:
            scheme = "socks5" if default_type == "socks5" else "http"
            chosen_for_parse = f"{scheme}://{chosen}"
        else:
            chosen_for_parse = chosen
        parsed = urlparse(chosen_for_parse)
        host = parsed.hostname or ""
        port = parsed.port or ""
        username = parsed.username or ""
        password = parsed.password or ""
        ptype = parsed.scheme.lower() if parsed.scheme else default_type
        if ptype == "socks":
            ptype = "socks5"
        if ptype not in ("http", "socks5"):
            ptype = default_type
        if not host or not port:
            return None
        return {
            "enabled": True,
            "host": host,
            "port": str(port),
            "username": username,
            "password": password,
            "proxy_type": ptype,
            "source": source,
            "pac": "",
        }

    for key in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        parsed = parse_proxy_value(os.environ.get(key, ""), f"环境变量 {key}")
        if parsed:
            return parsed

    if winreg is not None:
        try:
            reg = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            )
            proxy_enable, _ = winreg.QueryValueEx(reg, "ProxyEnable")
            try:
                auto_url, _ = winreg.QueryValueEx(reg, "AutoConfigURL")
            except Exception:
                auto_url = ""
            if auto_url:
                result["enabled"] = True
                result["proxy_type"] = "auto"
                result["source"] = "Windows PAC"
                result["pac"] = str(auto_url)
                return result
            if int(proxy_enable):
                proxy_server, _ = winreg.QueryValueEx(reg, "ProxyServer")
                parsed = parse_proxy_value(str(proxy_server), "Windows 系统代理")
                if parsed:
                    return parsed
        except Exception:
            pass

    return result

DEFAULT_THEME = {
    "window_bg": "#F3F6FB",
    "card_bg": "#FFFFFF",
    "primary": "#4F8CFF",
    "secondary": "#E8F0FF",
    "text": "#1F2328",
    "title": "#0F6CBD",
    "input_bg": "#FFFFFF",
    "border": "#D0D7E2",
    "highlight": "#7AA7FF",
    "log_bg": "#F8FBFF",
}

DEFAULT_CONFIG = {
    "rule": {
        "keyword_mode": "fuzzy",
        "keywords": "",
        "notify_target": "me",
        "only_selected_dialogs": True,
        "case_sensitive": False,
        "selected_dialog_ids": [],
    },
    "last_phone": "",
    "api_index": 0,
    "proxy": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": "1080",
        "username": "",
        "password": "",
        "proxy_type": "auto",
        "pac": "",
    },
    "selected_session": DEFAULT_SESSION_BASENAME,
    "theme": DEFAULT_THEME.copy(),
}


@dataclass
class MonitorRule:
    keyword_mode: str = "fuzzy"
    keywords: str = ""
    notify_target: str = "me"
    only_selected_dialogs: bool = True
    case_sensitive: bool = False
    selected_dialog_ids: Optional[List[int]] = None

    def __post_init__(self):
        if self.selected_dialog_ids is None:
            self.selected_dialog_ids = []
        self.selected_dialog_ids = [int(x) for x in self.selected_dialog_ids if str(x).strip()]


class ConfigStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    def load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                if isinstance(raw, dict):
                    self._deep_update(self.data, raw)
            except Exception:
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")

    def _deep_update(self, dst, src):
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                self._deep_update(dst[k], v)
            else:
                dst[k] = v


class TelethonWorker(threading.Thread):
    def __init__(self, ui_queue: queue.Queue, config: ConfigStore):
        super().__init__(daemon=True)
        self.ui_queue = ui_queue
        self.config = config
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Optional[TelegramClient] = None
        self.dialog_cache: Dict[int, dict] = {}
        self.monitor_enabled = False
        self.rule = MonitorRule(**self.config.data.get("rule", {}))
        self.login_phone: str = ""
        self.phone_code_hash: str = ""
        self.login_state: str = "idle"
        self._registered_handlers = False
        self.current_session_name: str = sanitize_session_name(self.config.data.get("selected_session", DEFAULT_SESSION_BASENAME))

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro):
        if not self.loop:
            raise RuntimeError("后台事件循环未启动")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def log(self, text: str):
        self.ui_queue.put(("log", text))

    def emit(self, event: str, payload=None):
        self.ui_queue.put((event, payload))

    def get_current_session_name(self) -> str:
        self.current_session_name = sanitize_session_name(self.current_session_name or self.config.data.get("selected_session", DEFAULT_SESSION_BASENAME))
        return self.current_session_name

    def get_current_session_base(self) -> Path:
        return get_session_base_path(self.get_current_session_name())

    def get_current_session_file(self) -> Path:
        return get_session_file(self.get_current_session_name())

    def set_current_session(self, session_name: str):
        session_name = sanitize_session_name(session_name)
        self.current_session_name = session_name
        self.config.data["selected_session"] = session_name
        self.config.save()
        self.log(f"当前会话槽位已切换：{session_name}")

    def list_local_sessions(self) -> List[str]:
        return list_session_names()

    def _normalize_phone(self, phone: str) -> str:
        phone = (phone or "").strip()
        phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        return phone

    def _get_api_credential(self) -> Tuple[int, str]:
        idx = int(self.config.data.get("api_index", 0) or 0)
        if idx < 0 or idx >= len(API_CREDENTIALS):
            idx = 0
        return API_CREDENTIALS[idx]

    def _build_proxy(self):
        proxy_conf = self.config.data.get("proxy", {})
        host = str(proxy_conf.get("host", "")).strip()
        port_raw = str(proxy_conf.get("port", "")).strip()
        username = str(proxy_conf.get("username", "")).strip() or None
        password = str(proxy_conf.get("password", "")).strip() or None
        proxy_type = str(proxy_conf.get("proxy_type", "auto") or "auto").strip().lower()
        pac = str(proxy_conf.get("pac", "")).strip()

        if proxy_type == "auto":
            detected = detect_system_proxy_settings()
            if detected.get("host") and detected.get("port"):
                host = str(detected.get("host", "")).strip()
                port_raw = str(detected.get("port", "")).strip()
                username = str(detected.get("username", "")).strip() or username
                password = str(detected.get("password", "")).strip() or password
                proxy_type = str(detected.get("proxy_type", "http") or "http").lower()
            elif host and port_raw:
                proxy_type = "http"
            elif pac:
                raise ValueError(f"检测到 PAC 自动代理：{pac}\nPAC 无法直接提取固定 IP 和端口，请切换到固定代理后再试。")
            else:
                raise ValueError("代理类型为 auto，但未检测到可用的系统代理。")

        if not host:
            raise ValueError("已启用代理，但代理地址不能为空")
        if not port_raw.isdigit():
            raise ValueError("已启用代理，但代理端口格式不正确")

        port = int(port_raw)
        if proxy_type == "socks5":
            return ("socks5", host, port, True, username, password)
        return ("http", host, port, username, password)

    async def _ensure_client(self):
        api_id, api_hash = self._get_api_credential()
        proxy_conf = self.config.data.get("proxy", {})
        proxy_enabled = bool(proxy_conf.get("enabled"))

        if not self.client:
            kwargs = {}
            if proxy_enabled:
                kwargs["proxy"] = self._build_proxy()

            self.client = TelegramClient(
                str(self.get_current_session_base()),
                api_id,
                api_hash,
                connection_retries=5,
                retry_delay=2,
                auto_reconnect=True,
                sequential_updates=True,
                **kwargs
            )

        if not self.client.is_connected():
            try:
                await self.client.connect()
            except Exception as e:
                if proxy_enabled:
                    raise RuntimeError(
                        "连接 Telegram 失败。当前已启用代理，但仍未连通。\n"
                        "请检查代理类型、地址、端口是否正确，或更换可用代理。\n\n"
                        f"原始错误: {e}"
                    )
                raise RuntimeError(
                    "连接 Telegram 失败。\n"
                    "请先确认本机网络能访问 Telegram；如当前网络受限，请开启代理后再试。\n\n"
                    f"原始错误: {e}"
                )

        return self.client

    async def refresh_client(self):
        if self.client:
            try:
                await self._unregister_monitor_handlers()
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        await self._ensure_client()
        self.log("连接配置已刷新。")

    async def check_existing_session(self):
        session_names = self.list_local_sessions()
        self.emit("session_list", {
            "sessions": session_names,
            "selected": self.get_current_session_name(),
        })

        ordered = []
        current = self.get_current_session_name()
        if current in session_names:
            ordered.append(current)
        for name in session_names:
            if name not in ordered:
                ordered.append(name)

        for session_name in ordered:
            self.current_session_name = sanitize_session_name(session_name)
            try:
                if self.client:
                    try:
                        await self._unregister_monitor_handlers()
                    except Exception:
                        pass
                    try:
                        await self.client.disconnect()
                    except Exception:
                        pass
                    self.client = None

                client = await self._ensure_client()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    self.login_state = "logged_in"
                    self.config.data["selected_session"] = self.current_session_name
                    self.config.save()
                    self.emit("session_selected", self.current_session_name)
                    self.emit("login_ok", {
                        "name": getattr(me, "first_name", "") or getattr(me, "username", "") or str(me.id),
                        "session_name": self.current_session_name,
                    })
                    self.log(f"已检测到本地 session，自动登录成功。当前会话：{self.current_session_name}，session路径：{self.get_current_session_file()}")
                    return
            except Exception as e:
                self.log(f"检查本地 session 失败（{self.current_session_name}）：{e}")

        self.emit("logged_out", None)

    async def apply_session_selection(self, session_name: str):
        if self.client:
            try:
                await self._unregister_monitor_handlers()
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        self.set_current_session(session_name)
        self.emit("session_selected", self.current_session_name)
        await self.check_existing_session()

    async def test_current_connection(self):
        if self.client:
            try:
                await self._unregister_monitor_handlers()
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        client = await self._ensure_client()
        await client.connect()
        me_text = "未登录"
        if await client.is_user_authorized():
            me = await client.get_me()
            me_text = getattr(me, "first_name", "") or getattr(me, "username", "") or str(me.id)
        proxy_conf = self.config.data.get("proxy", {})
        ptype = str(proxy_conf.get("proxy_type", "auto") or "auto").lower()
        self.log(f"连接测试成功。当前会话：{self.get_current_session_name()} | 代理类型：{ptype} | 登录状态：{me_text}")
        self.emit("connection_test_ok", {
            "session_name": self.get_current_session_name(),
            "proxy_type": ptype,
            "login_name": me_text,
        })

    async def send_code(self, phone: str):
        phone = self._normalize_phone(phone)
        if not phone:
            raise ValueError("请输入手机号，格式例如：+85244839944")

        client = await self._ensure_client()

        if await client.is_user_authorized():
            me = await client.get_me()
            self.login_state = "logged_in"
            self.emit("login_ok", {
                "name": getattr(me, "first_name", "") or getattr(me, "username", "") or str(me.id)
            })
            self.log("检测到已有会话，无需重新登录。")
            return

        try:
            result = await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            raise ValueError("手机号格式无效，请使用国际格式，例如：+85244839944")
        except FloodWaitError as e:
            raise ValueError(f"请求过于频繁，请等待 {e.seconds} 秒后再试")
        except Exception as e:
            raise RuntimeError(f"发送验证码失败：{e}")

        self.login_phone = phone
        self.phone_code_hash = result.phone_code_hash
        self.login_state = "code_sent"

        self.config.data["last_phone"] = phone
        self.config.save()

        self.emit("code_sent", {"phone": phone})
        self.log(f"验证码已发送，请去 Telegram 客户端查看。手机号：{phone}")

    async def sign_in_with_code(self, code: str):
        code = (code or "").strip()
        if not self.login_phone or not self.phone_code_hash:
            raise ValueError("请先发送验证码")
        if not code:
            raise ValueError("请输入验证码")

        client = await self._ensure_client()

        try:
            await client.sign_in(phone=self.login_phone, code=code, phone_code_hash=self.phone_code_hash)
        except SessionPasswordNeededError:
            self.login_state = "wait_password"
            self.emit("need_password", None)
            self.log("该账号开启了两步验证，请输入 2FA 密码。")
            return
        except PhoneCodeInvalidError:
            raise ValueError("验证码错误，请重新输入")
        except PhoneCodeExpiredError:
            raise ValueError("验证码已过期，请重新发送")
        except FloodWaitError as e:
            raise ValueError(f"操作过于频繁，请等待 {e.seconds} 秒后再试")
        except Exception as e:
            raise RuntimeError(f"验证码登录失败：{e}")

        me = await client.get_me()
        self.login_state = "logged_in"
        self.emit("login_ok", {
            "name": getattr(me, "first_name", "") or getattr(me, "username", "") or str(me.id)
        })
        self.log(f"登录成功，session 已生成。保存位置：{self.get_current_session_file()}")

    async def sign_in_with_password(self, password: str):
        password = (password or "").strip()
        if not password:
            raise ValueError("请输入 2FA 密码")

        client = await self._ensure_client()

        try:
            await client.sign_in(password=password)
        except FloodWaitError as e:
            raise ValueError(f"操作过于频繁，请等待 {e.seconds} 秒后再试")
        except Exception as e:
            raise RuntimeError(f"2FA 登录失败：{e}")

        me = await client.get_me()
        self.login_state = "logged_in"
        self.emit("login_ok", {
            "name": getattr(me, "first_name", "") or getattr(me, "username", "") or str(me.id)
        })
        self.log(f"2FA 验证通过，登录成功，session 已生成。保存位置：{self.get_current_session_file()}")

    async def logout(self):
        if self.client:
            try:
                await self._unregister_monitor_handlers()
            except Exception:
                pass
            try:
                await self.client.log_out()
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None

        session_file = self.get_current_session_file()
        if session_file.exists():
            try:
                session_file.unlink()
            except Exception:
                pass

        self.login_phone = ""
        self.phone_code_hash = ""
        self.login_state = "idle"
        self.monitor_enabled = False

        self.emit("logged_out", None)
        self.emit("monitor_state", False)
        self.log(f"已退出当前账号，并清除本地 session：{session_file}")

    async def load_dialogs(self):
        client = await self._ensure_client()
        if not await client.is_user_authorized():
            raise ValueError("请先登录 Telegram")

        items = []
        self.dialog_cache.clear()

        async for dialog in client.iter_dialogs():
            ent = dialog.entity

            if getattr(ent, "broadcast", False):
                dialog_type = "频道"
            elif getattr(ent, "megagroup", False):
                dialog_type = "群组"
            elif getattr(ent, "first_name", None) or getattr(ent, "last_name", None) or getattr(ent, "bot", False):
                dialog_type = "私聊"
            else:
                dialog_type = "群组"

            peer_id = int(utils.get_peer_id(ent))
            item = {
                "id": peer_id,
                "name": dialog.name or str(peer_id),
                "type": dialog_type,
                "username": getattr(ent, "username", "") or "",
            }
            self.dialog_cache[item["id"]] = item
            items.append(item)

        items.sort(key=lambda x: (x["type"], x["name"].lower()))
        self.emit("dialogs", items)
        self.log(f"已加载 {len(items)} 个会话。")

    def _get_match_result(self, text: str) -> Optional[dict]:
        text = text or ""
        rule = self.rule
        keywords = [x.strip() for x in rule.keywords.splitlines() if x.strip()]

        if not keywords:
            return None

        src = text if rule.case_sensitive else text.lower()

        if rule.keyword_mode == "exact":
            for raw in keywords:
                target = raw if rule.case_sensitive else raw.lower()
                if src == target:
                    return {"matched": raw, "match_type": "精准"}
            return None

        for raw in keywords:
            target = raw if rule.case_sensitive else raw.lower()
            if target in src:
                return {"matched": raw, "match_type": "泛匹配"}
        return None

    async def _send_notify(self, content: str):
        if not self.client:
            raise RuntimeError("客户端未连接")

        raw_target = (self.rule.notify_target or "").strip()
        kwargs = {
            "parse_mode": "html",
            "link_preview": False,
        }

        if not raw_target or raw_target.lower() == "me":
            await self.client.send_message("me", content, **kwargs)
            return

        if raw_target.lstrip("-").isdigit():
            await self.client.send_message(int(raw_target), content, **kwargs)
            return

        await self.client.send_message(raw_target, content, **kwargs)

    def _build_message_link(self, event, chat) -> str:
        try:
            username = getattr(chat, "username", None)
            if username:
                return f"https://t.me/{username}/{event.message.id}"

            chat_id = int(utils.get_peer_id(chat))
            chat_id_str = str(chat_id)

            if chat_id_str.startswith("-100"):
                internal_id = chat_id_str[4:]
                return f"https://t.me/c/{internal_id}/{event.message.id}"

            return "私聊或普通群无公开链接"
        except Exception:
            return "链接生成失败"

    def _get_chat_type(self, chat) -> str:
        if getattr(chat, "broadcast", False):
            return "频道"
        if getattr(chat, "megagroup", False):
            return "群组"
        if getattr(chat, "first_name", None) or getattr(chat, "last_name", None) or getattr(chat, "bot", False):
            return "私聊"
        return "群组"

    def _extract_chat_id_from_event(self, event, chat) -> int:
        if getattr(event, "chat_id", None) is not None:
            return int(event.chat_id)
        if chat is not None:
            return int(utils.get_peer_id(chat))
        if getattr(event, "message", None) and getattr(event.message, "peer_id", None):
            return int(utils.get_peer_id(event.message.peer_id))
        raise RuntimeError("无法识别当前消息的会话 ID")

    async def _register_monitor_handlers(self):
        client = await self._ensure_client()
        await self._unregister_monitor_handlers()
        client.add_event_handler(self._on_new_message, events.NewMessage())
        client.add_event_handler(self._on_message_edited, events.MessageEdited())
        self._registered_handlers = True
        self.log("监听处理器已注册：NewMessage + MessageEdited")

    async def _unregister_monitor_handlers(self):
        if self.client:
            try:
                self.client.remove_event_handler(self._on_new_message, events.NewMessage)
            except Exception:
                pass
            try:
                self.client.remove_event_handler(self._on_message_edited, events.MessageEdited)
            except Exception:
                pass
        self._registered_handlers = False

    async def start_monitor(self, rule_dict: dict):
        client = await self._ensure_client()
        if not await client.is_user_authorized():
            raise ValueError("请先登录 Telegram")

        self.rule = MonitorRule(**rule_dict)
        self.config.data["rule"] = asdict(self.rule)
        self.config.save()

        await self._register_monitor_handlers()

        self.monitor_enabled = True
        self.emit("monitor_state", True)
        self.log("消息监听已启动。")
        self.log(
            f"当前监听配置 | 模式:{'精准' if self.rule.keyword_mode == 'exact' else '泛匹配'} | "
            f"范围:{'所选会话' if self.rule.only_selected_dialogs else '全部会话'} | "
            f"已选数量:{len(self.rule.selected_dialog_ids)} | 通知目标:{self.rule.notify_target or 'me'}"
        )

        selected_count = len(self.rule.selected_dialog_ids or [])
        mode_text = "精准" if self.rule.keyword_mode == "exact" else "泛匹配"
        target_text = (self.rule.notify_target or "me").strip() or "me"

        safe_mode_text = html.escape(mode_text)
        safe_target_text = html.escape(target_text)
        safe_scope_text = html.escape("所选会话" if self.rule.only_selected_dialogs else "全部会话")
        safe_selected_count = html.escape(str(selected_count))

        notify_text = (
            f"✅ <b>监听已开启</b>\n\n"
            f"<b>匹配模式：</b><code>{safe_mode_text}</code>\n"
            f"<b>监听范围：</b><code>{safe_scope_text}</code>\n"
            f"<b>会话数量：</b><code>{safe_selected_count}</code>\n"
            f"<b>通知目标：</b><code>{safe_target_text}</code>"
        )

        try:
            await self._send_notify(notify_text)
        except Exception as e:
            self.log(f"监听启动通知发送失败: {e}")

    async def stop_monitor(self):
        await self._unregister_monitor_handlers()
        self.monitor_enabled = False
        self.emit("monitor_state", False)
        self.log("消息监听已停止。")

    async def toggle_monitor(self, rule_dict: dict):
        if self.monitor_enabled:
            await self.stop_monitor()
        else:
            await self.start_monitor(rule_dict)

    async def _on_message_edited(self, event):
        await self._handle_message_event(event, event_name="消息编辑")

    async def _on_new_message(self, event):
        await self._handle_message_event(event, event_name="新消息")

    async def _handle_message_event(self, event, event_name="消息事件"):
        if not self.monitor_enabled:
            return

        try:
            if getattr(event, "out", False):
                return

            chat = None
            sender = None

            try:
                chat = await event.get_chat()
            except Exception as e:
                self.log(f"{event_name} 获取 chat 失败: {e}")

            try:
                sender = await event.get_sender()
            except Exception as e:
                self.log(f"{event_name} 获取 sender 失败: {e}")

            chat_id = self._extract_chat_id_from_event(event, chat)

            if self.rule.only_selected_dialogs:
                selected = set(int(x) for x in (self.rule.selected_dialog_ids or []))
                if selected and chat_id not in selected:
                    self.log(f"{event_name} 已忽略：会话 {chat_id} 不在所选列表中")
                    return

            text = event.raw_text or ""
            if not text.strip() and getattr(event, "message", None):
                if event.message.media:
                    text = "[媒体消息/无纯文本]"
                else:
                    text = "[无文本内容]"

            chat_type = self._get_chat_type(chat) if chat is not None else (
                "频道" if getattr(event, "is_channel", False) and not getattr(event, "is_group", False)
                else "群组" if getattr(event, "is_group", False)
                else "私聊"
            )

            chat_name = (
                getattr(chat, "title", None)
                or getattr(chat, "first_name", None)
                or getattr(chat, "username", None)
                or self.dialog_cache.get(chat_id, {}).get("name")
                or str(chat_id)
            )

            sender_username = getattr(sender, "username", None) or ""
            sender_name = (
                getattr(sender, "first_name", None)
                or getattr(sender, "title", None)
                or getattr(sender, "last_name", None)
                or getattr(event.message, "post_author", None)
                or (chat_name if chat_type == "频道" else None)
                or "未知发送者"
            )
            sender_text = f"@{sender_username}" if sender_username else sender_name

            try:
                msg_time = event.message.date.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                msg_time = str(getattr(event.message, "date", ""))

            self.log(
                f"{event_name} 收到 | 类型:{chat_type} | 会话:{chat_name} | 会话ID:{chat_id} | "
                f"发送人:{sender_text} | 内容:{text[:120]}"
            )

            match_result = self._get_match_result(text)
            if not match_result:
                self.log(f"{event_name} 未命中关键词 | 会话:{chat_name} | 内容:{text[:80]}")
                return

            message_link = self._build_message_link(event, chat)
            content_text = text if text.strip() else "[无文本内容]"

            safe_keyword = html.escape(str(match_result["matched"]))
            safe_event_name = html.escape(str(event_name))
            safe_chat_type = html.escape(str(chat_type))
            safe_chat_name = html.escape(str(chat_name))
            safe_chat_id = html.escape(str(chat_id))
            safe_sender_text = html.escape(str(sender_text))
            safe_content_text = html.escape(str(content_text))
            safe_msg_time = html.escape(str(msg_time))
            safe_message_link = html.escape(str(message_link))

            if message_link.startswith("http://") or message_link.startswith("https://"):
                link_html = f'<a href="{safe_message_link}">点击查看原文</a>'
            else:
                link_html = f"<code>{safe_message_link}</code>"

            notify_text = (
                f"🔔 <b>关键词命中通知</b>\n\n"
                f"<b>关键词：</b> <code>{safe_keyword}</code>\n"
                f"<b>消息类型：</b> {safe_event_name}\n"
                f"<b>会话类型：</b> {safe_chat_type}\n"
                f"<b>会话名称：</b> <code>{safe_chat_name}</code>\n"
                f"<b>会话ID：</b> <code>{safe_chat_id}</code>\n"
                f"<b>发送人：</b> <code>{safe_sender_text}</code>\n"
                f"<b>发送时间：</b> <code>{safe_msg_time}</code>\n\n"
                f"<b>发送内容：</b>\n"
                f"<pre>{safe_content_text}</pre>\n"
                f"<b>原文链接：</b> {link_html}"
            )

            await self._send_notify(notify_text)

            self.emit("hit", {
                "chat_type": chat_type,
                "chat_name": chat_name,
                "match_type": match_result["match_type"],
                "keyword": match_result["matched"],
                "sender": sender_text,
                "text": content_text[:200],
                "time": msg_time,
            })

        except Exception as e:
            self.log(f"监听处理失败: {e}")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = ConfigStore(CONFIG_PATH)
        self.theme = self._build_theme()

        self.root.title("Telegram 消息监听器")
        self.root.geometry("1280x860")
        self.root.minsize(900, 620)
        self.root.configure(bg=self.theme["window_bg"])

        self.ui_queue: queue.Queue = queue.Queue()
        self.worker = TelethonWorker(self.ui_queue, self.config)
        self.worker.start()

        self.dialog_vars: Dict[int, tk.BooleanVar] = {}
        self.dialog_items: List[dict] = []

        self.dialog_canvas = None
        self.dialog_wrap = None
        self.log_text = None
        self.keyword_text = None
        self.monitor_toggle_btn = None
        self.password_row = None
        self.password_entry = None
        self.main_paned = None
        self.left_card = None
        self.left_canvas = None
        self.left_scroll = None
        self.left_inner = None
        self.left_canvas_window = None
        self.mid_card = None
        self.right_card = None
        self.settings_btn = None
        self.settings_menu = None
        self.session_listbox = None
        self.session_name_var = None
        self.proxy_type_var = None
        self.system_proxy_status_var = None
        self.is_compact_layout = False
        self.current_paned_orient = tk.HORIZONTAL
        self.top_fields_wrap = None
        self.top_actions_wrap = None
        self.top_actions_row1 = None
        self.top_actions_row2 = None
        self.left_host = None
        self.mid_host = None
        self.right_host = None
        self.sponsor_qr_image = None

        self._init_style()
        self._build_ui()
        self._load_saved_config_to_form()

        self.root.after(300, self._check_session_after_start)
        self.root.after(200, self._pump_ui_queue)
        self.root.bind("<Configure>", self._on_window_resize)

    def _is_valid_hex_color(self, value: str) -> bool:
        if not value:
            return False
        return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()))

    def _normalize_hex_color(self, value: str, fallback: str) -> str:
        value = (value or "").strip()
        if self._is_valid_hex_color(value):
            return value.upper()
        return fallback

    def _rgb_to_hex(self, rgb) -> str:
        if not rgb:
            return "#000000"
        r, g, b = rgb[:3]
        return f"#{int(r):02X}{int(g):02X}{int(b):02X}"

    def _build_theme(self):
        theme = DEFAULT_THEME.copy()
        saved = self.config.data.get("theme", {})
        if isinstance(saved, dict):
            theme.update(saved)
        for k, v in DEFAULT_THEME.items():
            theme[k] = self._normalize_hex_color(theme.get(k, v), v)
        return theme

    def _save_theme(self):
        normalized = {}
        for k, v in DEFAULT_THEME.items():
            normalized[k] = self._normalize_hex_color(self.theme.get(k, v), v)
        self.theme = normalized
        self.config.data["theme"] = self.theme.copy()
        self.config.save()

    def _init_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        t = self.theme

        style.configure(".", font=("Microsoft YaHei UI", 10), background=t["window_bg"], foreground=t["text"])
        style.configure("Root.TFrame", background=t["window_bg"])
        style.configure("Card.TFrame", background=t["card_bg"])
        style.configure("Surface.TFrame", background=t["secondary"])
        style.configure("TLabel", background=t["window_bg"], foreground=t["text"])
        style.configure("Card.TLabel", background=t["card_bg"], foreground=t["text"])
        style.configure("Soft.TLabel", background=t["secondary"], foreground=t["text"])
        style.configure("Title.TLabel", background=t["window_bg"], foreground=t["title"], font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Section.TLabel", background=t["card_bg"], foreground=t["title"], font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Menu.TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7), background=t["secondary"], foreground=t["text"], borderwidth=0)
        style.map("Menu.TButton", background=[("active", t["primary"]), ("pressed", t["primary"])], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(11, 7), background=t["secondary"], foreground=t["text"], borderwidth=0)
        style.map("TButton", background=[("active", t["primary"]), ("pressed", t["primary"])], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(12, 8), background=t["primary"], foreground="#FFFFFF", borderwidth=0)
        style.map("Primary.TButton", background=[("active", t["title"]), ("pressed", t["title"])], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("TEntry", fieldbackground=t["input_bg"], foreground=t["text"], bordercolor=t["border"], lightcolor=t["border"], darkcolor=t["border"], padding=7)
        style.configure("TCombobox", fieldbackground=t["input_bg"], foreground=t["text"], bordercolor=t["border"], lightcolor=t["border"], darkcolor=t["border"], padding=5)
        style.configure("TCheckbutton", background=t["card_bg"], foreground=t["text"])
        style.map("TCheckbutton", background=[("active", t["card_bg"])])
        style.configure("TPanedwindow", background=t["window_bg"])
        style.configure("Vertical.TScrollbar", background=t["secondary"], troughcolor=t["window_bg"], bordercolor=t["window_bg"], arrowcolor=t["text"])

    def _apply_theme_runtime(self):
        self.root.configure(bg=self.theme["window_bg"])
        self._init_style()
        self._rebuild_text_widgets_theme()
        self._refresh_menu_theme()

    def _refresh_menu_theme(self):
        if self.settings_menu:
            self.settings_menu.configure(
                bg=self.theme["card_bg"],
                fg=self.theme["text"],
                activebackground=self.theme["primary"],
                activeforeground="#FFFFFF",
                bd=0,
                relief="flat"
            )

    def _rebuild_text_widgets_theme(self):
        if self.keyword_text:
            self.keyword_text.configure(
                bg=self.theme["input_bg"],
                fg=self.theme["text"],
                insertbackground=self.theme["text"],
                highlightbackground=self.theme["border"],
                highlightcolor=self.theme["highlight"]
            )
        if self.log_text:
            self.log_text.configure(
                bg=self.theme["log_bg"],
                fg=self.theme["text"],
                insertbackground=self.theme["text"],
                highlightbackground=self.theme["border"],
                highlightcolor=self.theme["highlight"]
            )
        if self.dialog_canvas:
            self.dialog_canvas.configure(bg=self.theme["card_bg"])
        if self.session_listbox:
            self.session_listbox.configure(
                bg=self.theme["input_bg"],
                fg=self.theme["text"],
                selectbackground=self.theme["primary"],
                selectforeground="#FFFFFF",
                highlightbackground=self.theme["border"],
                highlightcolor=self.theme["highlight"]
            )
        if self.left_canvas:
            self.left_canvas.configure(bg=self.theme["card_bg"])

    def _bind_left_mousewheel(self):
        if self.left_canvas is None:
            return

        def _on_mousewheel(event):
            if self.left_canvas is None:
                return
            if getattr(event, "delta", 0):
                self.left_canvas.yview_scroll(int(-event.delta / 120), "units")
            elif getattr(event, "num", None) == 4:
                self.left_canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                self.left_canvas.yview_scroll(1, "units")

        def _bind(_event=None):
            self.left_canvas.bind_all("<MouseWheel>", _on_mousewheel)
            self.left_canvas.bind_all("<Button-4>", _on_mousewheel)
            self.left_canvas.bind_all("<Button-5>", _on_mousewheel)

        def _unbind(_event=None):
            self.left_canvas.unbind_all("<MouseWheel>")
            self.left_canvas.unbind_all("<Button-4>")
            self.left_canvas.unbind_all("<Button-5>")

        self.left_canvas.bind("<Enter>", _bind)
        self.left_canvas.bind("<Leave>", _unbind)

    def _build_card(self, parent):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=14)
        return frame

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top_wrap = ttk.Frame(self.root, style="Root.TFrame", padding=12)
        top_wrap.grid(row=0, column=0, sticky="nsew")

        top = self._build_card(top_wrap)
        top.pack(fill="x")

        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=0)

        self.top_fields_wrap = ttk.Frame(top, style="Card.TFrame")
        self.top_fields_wrap.grid(row=0, column=0, sticky="ew")

        self.top_actions_wrap = ttk.Frame(top, style="Card.TFrame")
        self.top_actions_wrap.grid(row=0, column=1, sticky="e", padx=(12, 0))

        fields_row1 = ttk.Frame(self.top_fields_wrap, style="Card.TFrame")
        fields_row1.pack(fill="x", anchor="w")

        ttk.Label(fields_row1, text="手机号", style="Card.TLabel").pack(side="left")
        self.phone_var = tk.StringVar()
        ttk.Entry(fields_row1, textvariable=self.phone_var, width=22).pack(side="left", padx=(6, 14))

        ttk.Label(fields_row1, text="验证码", style="Card.TLabel").pack(side="left")
        self.code_var = tk.StringVar()
        ttk.Entry(fields_row1, textvariable=self.code_var, width=16).pack(side="left", padx=(6, 14))

        self.password_row = ttk.Frame(fields_row1, style="Card.TFrame")
        self.password_row.pack(side="left", padx=(6, 0))
        ttk.Label(self.password_row, text="2FA密码", style="Card.TLabel").pack(side="left")
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(self.password_row, textvariable=self.password_var, show="*", width=18)
        self.password_entry.pack(side="left", padx=(6, 0))
        self.password_row.pack_forget()

        self.top_actions_row1 = ttk.Frame(self.top_actions_wrap, style="Card.TFrame")
        self.top_actions_row1.pack(anchor="e")

        self.top_actions_row2 = ttk.Frame(self.top_actions_wrap, style="Card.TFrame")
        self.top_actions_row2.pack(anchor="e", pady=(6, 0))

        self._build_settings_menu(self.top_actions_row1)

        ttk.Button(self.top_actions_row1, text="发送验证码", command=self.send_code).pack(side="left", padx=4)
        ttk.Button(self.top_actions_row1, text="验证码登录", command=self.login_by_code).pack(side="left", padx=4)
        ttk.Button(self.top_actions_row1, text="2FA登录", command=self.login_by_password).pack(side="left", padx=4)
        ttk.Button(self.top_actions_row1, text="退出登录", command=self.logout).pack(side="left", padx=4)

        ttk.Button(self.top_actions_row2, text="刷新连接", command=self.refresh_connection).pack(side="left", padx=4)
        ttk.Button(self.top_actions_row2, text="刷新会话列表", command=self.load_dialogs).pack(side="left", padx=4)
        ttk.Button(self.top_actions_row2, text="赞助", command=self.show_sponsor_popup).pack(side="left", padx=4)

        self.main_paned = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        self.main_paned.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.current_paned_orient = tk.HORIZONTAL

        self.left_host = ttk.Frame(self.main_paned, style="Root.TFrame")
        self.mid_host = ttk.Frame(self.main_paned, style="Root.TFrame")
        self.right_host = ttk.Frame(self.main_paned, style="Root.TFrame")

        left_shell = self._build_card(self.left_host)
        left_shell.pack(fill="both", expand=True)

        self.left_canvas = tk.Canvas(left_shell, highlightthickness=0, bg=self.theme["card_bg"])
        self.left_scroll = ttk.Scrollbar(left_shell, orient="vertical", command=self.left_canvas.yview)
        self.left_inner = ttk.Frame(self.left_canvas, style="Card.TFrame")
        self.left_inner.bind("<Configure>", lambda e: self.left_canvas.configure(scrollregion=self.left_canvas.bbox("all")))
        self.left_canvas_window = self.left_canvas.create_window((0, 0), window=self.left_inner, anchor="nw")
        self.left_canvas.configure(yscrollcommand=self.left_scroll.set)
        self.left_canvas.pack(side="left", fill="both", expand=True)
        self.left_scroll.pack(side="right", fill="y")
        self.left_canvas.bind("<Configure>", lambda e: self.left_canvas.itemconfigure(self.left_canvas_window, width=e.width))

        self.left_card = self.left_inner
        self.mid_card = self._build_card(self.mid_host)
        self.right_card = self._build_card(self.right_host)

        self.mid_card.pack(fill="both", expand=True)
        self.right_card.pack(fill="both", expand=True)

        self.main_paned.add(self.left_host, weight=2)
        self.main_paned.add(self.mid_host, weight=3)
        self.main_paned.add(self.right_host, weight=3)

        ttk.Label(self.left_card, text="账号状态", style="Section.TLabel").pack(anchor="w")
        self.login_state_var = tk.StringVar(value="未登录")
        ttk.Label(self.left_card, textvariable=self.login_state_var, style="Card.TLabel").pack(anchor="w", pady=(8, 12))

        ttk.Label(self.left_card, text="监听状态", style="Section.TLabel").pack(anchor="w")
        self.monitor_state_var = tk.StringVar(value="未开启")
        ttk.Label(self.left_card, textvariable=self.monitor_state_var, style="Card.TLabel").pack(anchor="w", pady=(8, 12))

        ttk.Label(self.left_card, text="本地路径", style="Section.TLabel").pack(anchor="w")
        ttk.Label(self.left_card, text=f"session目录：{APP_DIR}", justify="left", style="Card.TLabel").pack(anchor="w", pady=(8, 8))

        ttk.Label(self.left_card, text="Session 管理", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        self.session_name_var = tk.StringVar(value=sanitize_session_name(self.config.data.get("selected_session", DEFAULT_SESSION_BASENAME)))

        session_row = ttk.Frame(self.left_card, style="Card.TFrame")
        session_row.pack(fill="x", pady=(4, 6))
        ttk.Label(session_row, text="当前会话", style="Card.TLabel").pack(side="left")
        ttk.Entry(session_row, textvariable=self.session_name_var, width=18).pack(side="left", padx=(8, 8))
        ttk.Button(session_row, text="选择登录", command=self.select_session_login).pack(side="left")

        session_btns = ttk.Frame(self.left_card, style="Card.TFrame")
        session_btns.pack(fill="x", pady=(0, 6))
        ttk.Button(session_btns, text="刷新Session", command=self.refresh_session_list).pack(side="left")
        ttk.Button(session_btns, text="新建Session", command=self.create_session_slot).pack(side="left", padx=6)

        self.session_listbox = tk.Listbox(
            self.left_card,
            height=6,
            bg=self.theme["input_bg"],
            fg=self.theme["text"],
            selectbackground=self.theme["primary"],
            selectforeground="#FFFFFF",
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.theme["border"],
            highlightcolor=self.theme["highlight"]
        )
        self.session_listbox.pack(fill="x", pady=(0, 10))
        self.session_listbox.bind("<<ListboxSelect>>", self._on_session_pick)

        tip_text = (
            "登录流程：\n"
            "1. 输入手机号\n"
            "2. 点击发送验证码\n"
            "3. 去 Telegram 客户端查看验证码\n"
            "4. 输入验证码后点验证码登录\n"
            "5. 如账号开启 2FA，会显示 2FA 密码输入框\n"
            "6. 输入后点击 2FA 登录\n"
            "7. 若本地已有 session，会自动识别并自动登录"
        )
        ttk.Label(self.left_card, text=tip_text, justify="left", style="Card.TLabel").pack(anchor="w")

        ttk.Separator(self.left_card).pack(fill="x", pady=12)

        ttk.Label(self.left_card, text="连接设置", style="Section.TLabel").pack(anchor="w")

        self.proxy_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.left_card, text="启用代理", variable=self.proxy_enabled_var).pack(anchor="w", pady=(8, 4))

        p0 = ttk.Frame(self.left_card, style="Card.TFrame")
        p0.pack(fill="x", pady=3)
        ttk.Label(p0, text="代理类型", style="Card.TLabel").pack(side="left")
        self.proxy_type_var = tk.StringVar(value="auto")
        ttk.Combobox(p0, textvariable=self.proxy_type_var, values=["auto", "http", "socks5"], width=10, state="readonly").pack(side="left", padx=(8, 10))

        p1 = ttk.Frame(self.left_card, style="Card.TFrame")
        p1.pack(fill="x", pady=3)
        ttk.Label(p1, text="代理地址", style="Card.TLabel").pack(side="left")
        self.proxy_host_var = tk.StringVar()
        ttk.Entry(p1, textvariable=self.proxy_host_var, width=18).pack(side="left", padx=(8, 10))
        ttk.Label(p1, text="端口", style="Card.TLabel").pack(side="left")
        self.proxy_port_var = tk.StringVar()
        ttk.Entry(p1, textvariable=self.proxy_port_var, width=8).pack(side="left", padx=(8, 0))

        p2 = ttk.Frame(self.left_card, style="Card.TFrame")
        p2.pack(fill="x", pady=3)
        ttk.Label(p2, text="用户名", style="Card.TLabel").pack(side="left")
        self.proxy_user_var = tk.StringVar()
        ttk.Entry(p2, textvariable=self.proxy_user_var, width=14).pack(side="left", padx=(8, 10))
        ttk.Label(p2, text="密码", style="Card.TLabel").pack(side="left")
        self.proxy_pass_var = tk.StringVar()
        ttk.Entry(p2, textvariable=self.proxy_pass_var, show="*", width=14).pack(side="left", padx=(8, 0))

        self.system_proxy_status_var = tk.StringVar(value="系统代理：未检测")
        ttk.Label(self.left_card, textvariable=self.system_proxy_status_var, justify="left", style="Card.TLabel").pack(anchor="w", pady=(8, 4))

        proxy_btns = ttk.Frame(self.left_card, style="Card.TFrame")
        proxy_btns.pack(fill="x", pady=(4, 6))
        ttk.Button(proxy_btns, text="获取系统代理", command=self.load_system_proxy).pack(side="left")
        ttk.Button(proxy_btns, text="测试连接", command=self.test_proxy_connection).pack(side="left", padx=6)
        ttk.Button(proxy_btns, text="清空代理", command=self.clear_proxy_settings).pack(side="left")

        ttk.Button(self.left_card, text="保存连接设置", command=self.save_proxy_settings).pack(anchor="w", pady=(10, 6))

        ttk.Separator(self.left_card).pack(fill="x", pady=12)

        ttk.Label(self.left_card, text="说明", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            self.left_card,
            text="通知目标填 me 或留空时发送到自己的收藏夹。\n通知目标填纯数字时发送到对应用户 ID。\n支持频道、群组、私聊监听。",
            justify="left",
            style="Card.TLabel"
        ).pack(anchor="w", pady=(8, 0))

        topbar = ttk.Frame(self.mid_card, style="Card.TFrame")
        topbar.pack(fill="x")
        ttk.Label(topbar, text="选择要监听的频道 / 群组 / 私聊", style="Section.TLabel").pack(side="left")
        ttk.Button(topbar, text="全选", command=self.select_all_dialogs).pack(side="right", padx=4)
        ttk.Button(topbar, text="清空", command=self.clear_dialogs).pack(side="right", padx=4)

        self.dialog_search_var = tk.StringVar()
        self.dialog_search_var.trace_add("write", lambda *_: self.render_dialogs())
        ttk.Entry(self.mid_card, textvariable=self.dialog_search_var).pack(fill="x", pady=(10, 8))

        dialog_box = ttk.Frame(self.mid_card, style="Card.TFrame")
        dialog_box.pack(fill="both", expand=True)

        self.dialog_canvas = tk.Canvas(dialog_box, highlightthickness=0, bg=self.theme["card_bg"])
        self.dialog_scroll = ttk.Scrollbar(dialog_box, orient="vertical", command=self.dialog_canvas.yview)
        self.dialog_wrap = ttk.Frame(self.dialog_canvas, style="Card.TFrame")

        self.dialog_wrap.bind("<Configure>", lambda e: self.dialog_canvas.configure(scrollregion=self.dialog_canvas.bbox("all")))
        self.dialog_canvas.create_window((0, 0), window=self.dialog_wrap, anchor="nw")
        self.dialog_canvas.configure(yscrollcommand=self.dialog_scroll.set)

        self.dialog_canvas.pack(side="left", fill="both", expand=True)
        self.dialog_scroll.pack(side="right", fill="y")

        self._bind_left_mousewheel()

        ttk.Label(self.right_card, text="监听规则", style="Section.TLabel").pack(anchor="w")

        self.only_selected_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.right_card, text="仅监听所选会话", variable=self.only_selected_var).pack(anchor="w", pady=(8, 4))

        self.case_sensitive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.right_card, text="区分大小写", variable=self.case_sensitive_var).pack(anchor="w")

        ttk.Label(self.right_card, text="匹配模式", style="Card.TLabel").pack(anchor="w", pady=(10, 4))
        self.keyword_mode_var = tk.StringVar(value="fuzzy")
        ttk.Combobox(self.right_card, textvariable=self.keyword_mode_var, state="readonly", values=["exact", "fuzzy"]).pack(fill="x")

        ttk.Label(self.right_card, text="监听内容（每行一个关键词）", style="Card.TLabel").pack(anchor="w", pady=(10, 4))
        self.keyword_text = tk.Text(
            self.right_card,
            height=10,
            bg=self.theme["input_bg"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.theme["border"],
            highlightcolor=self.theme["highlight"]
        )
        self.keyword_text.pack(fill="x")

        ttk.Label(self.right_card, text="通知ID / 用户名（me=收藏夹，纯数字=指定用户）", style="Card.TLabel").pack(anchor="w", pady=(10, 4))
        self.notify_target_var = tk.StringVar(value="me")
        ttk.Entry(self.right_card, textvariable=self.notify_target_var).pack(fill="x")

        btns = ttk.Frame(self.right_card, style="Card.TFrame")
        btns.pack(fill="x", pady=12)
        self.monitor_toggle_btn = ttk.Button(btns, text="开启监听", style="Primary.TButton", command=self.toggle_monitor)
        self.monitor_toggle_btn.pack(side="left")
        ttk.Button(btns, text="导出配置", command=self.export_config).pack(side="left", padx=6)
        ttk.Button(btns, text="导入配置", command=self.import_config).pack(side="left")

        ttk.Label(self.right_card, text="运行日志", style="Section.TLabel").pack(anchor="w", pady=(6, 6))
        self.log_text = tk.Text(
            self.right_card,
            height=18,
            bg=self.theme["log_bg"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.theme["border"],
            highlightcolor=self.theme["highlight"]
        )
        self.log_text.pack(fill="both", expand=True)

    def _build_settings_menu(self, parent):
        self.settings_btn = ttk.Button(parent, text="设置 ▾", style="Menu.TButton")
        self.settings_btn.pack(side="left", padx=4)

        self.settings_menu = tk.Menu(self.root, tearoff=0)
        self.settings_menu.add_command(label="自定义配色", command=self.open_theme_settings)
        self.settings_menu.add_command(label="恢复默认配色", command=self.reset_theme)
        self._refresh_menu_theme()

        def show_menu():
            x = self.settings_btn.winfo_rootx()
            y = self.settings_btn.winfo_rooty() + self.settings_btn.winfo_height() + 4
            self.settings_menu.tk_popup(x, y)

        self.settings_btn.configure(command=show_menu)

    def _on_window_resize(self, event=None):
        try:
            if event is not None and event.widget is not self.root:
                return
            width = self.root.winfo_width()
            self._apply_responsive_layout(width)
        except Exception:
            pass

    def _apply_responsive_layout(self, width: int):
        compact = width < 1180

        if compact != self.is_compact_layout:
            self.is_compact_layout = compact
            if compact:
                self.top_fields_wrap.grid_configure(row=0, column=0, sticky="ew", padx=0, pady=(0, 10))
                self.top_actions_wrap.grid_configure(row=1, column=0, sticky="w", padx=0, pady=0)
            else:
                self.top_fields_wrap.grid_configure(row=0, column=0, sticky="ew", padx=0, pady=0)
                self.top_actions_wrap.grid_configure(row=0, column=1, sticky="e", padx=(12, 0), pady=0)

        desired_orient = tk.VERTICAL if width < 1050 else tk.HORIZONTAL
        if desired_orient != self.current_paned_orient and self.main_paned:
            self._rebuild_paned(orient=desired_orient)

    def _rebuild_paned(self, orient=tk.HORIZONTAL):
        try:
            self.main_paned.grid_forget()
        except Exception:
            pass

        self.main_paned = ttk.Panedwindow(self.root, orient=orient)
        self.main_paned.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        self.current_paned_orient = orient

        if orient == tk.HORIZONTAL:
            self.main_paned.add(self.left_host, weight=2)
            self.main_paned.add(self.mid_host, weight=3)
            self.main_paned.add(self.right_host, weight=3)
        else:
            self.main_paned.add(self.left_host, weight=2)
            self.main_paned.add(self.mid_host, weight=4)
            self.main_paned.add(self.right_host, weight=4)

    def _generate_sponsor_qr(self, text: str):
        if qrcode is None or ImageTk is None:
            return None
        try:
            qr = qrcode.QRCode(version=1, box_size=8, border=2)
            qr.add_data(text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def show_sponsor_popup(self):
        address = "TFm3n5TMLcTfBLpM2u892np1fjFeUWsV2p"

        win = tk.Toplevel(self.root)
        win.title("赞助")
        win.transient(self.root)
        win.grab_set()
        win.configure(bg=self.theme["window_bg"])
        win.geometry("430x520")
        win.minsize(360, 420)

        wrap = ttk.Frame(win, style="Root.TFrame", padding=16)
        wrap.pack(fill="both", expand=True)

        card = ttk.Frame(wrap, style="Card.TFrame", padding=16)
        card.pack(fill="both", expand=True)

        ttk.Label(card, text="赞助作者", style="Section.TLabel").pack(anchor="center", pady=(0, 14))

        ttk.Label(card, text="钱包地址", style="Card.TLabel").pack(anchor="w")
        addr_box = tk.Text(
            card,
            height=3,
            wrap="word",
            bg=self.theme["input_bg"],
            fg=self.theme["text"],
            insertbackground=self.theme["text"],
            relief="solid",
            bd=1,
            highlightthickness=1,
            highlightbackground=self.theme["border"],
            highlightcolor=self.theme["highlight"]
        )
        addr_box.pack(fill="x", pady=(6, 12))
        addr_box.insert("1.0", address)
        addr_box.configure(state="disabled")

        def copy_address():
            self.root.clipboard_clear()
            self.root.clipboard_append(address)
            self.root.update()
            messagebox.showinfo("已复制", "赞助地址已复制到剪贴板")

        ttk.Button(card, text="复制地址", command=copy_address).pack(anchor="w", pady=(0, 14))

        ttk.Label(card, text="二维码", style="Card.TLabel").pack(anchor="w", pady=(0, 8))

        qr_img = self._generate_sponsor_qr(address)
        self.sponsor_qr_image = qr_img

        qr_wrap = ttk.Frame(card, style="Card.TFrame")
        qr_wrap.pack(fill="both", expand=True)

        if qr_img:
            qr_label = tk.Label(
                qr_wrap,
                image=qr_img,
                bg=self.theme["card_bg"],
                bd=1,
                relief="solid"
            )
            qr_label.pack(pady=6)
        else:
            fallback = tk.Text(
                qr_wrap,
                height=10,
                wrap="word",
                bg=self.theme["input_bg"],
                fg=self.theme["text"],
                insertbackground=self.theme["text"],
                relief="solid",
                bd=1
            )
            fallback.pack(fill="both", expand=True)
            fallback.insert(
                "1.0",
                "当前环境未安装 qrcode / Pillow，暂时无法生成二维码。\n\n"
                f"二维码内容：\n{address}"
            )
            fallback.configure(state="disabled")

        ttk.Label(
            card,
            text="二维码内容与地址相同：TFm3n5TMLcTfBLpM2u892np1fjFeUWsV2p",
            justify="center",
            style="Card.TLabel"
        ).pack(anchor="center", pady=(12, 8))

        ttk.Button(card, text="关闭", command=win.destroy).pack(pady=(6, 0))

    def _pick_screen_color(self, callback):
        if ImageGrab is None:
            messagebox.showerror("缺少依赖", "吸取颜色需要 Pillow，请先执行：pip install pillow")
            return

        picker = tk.Toplevel(self.root)
        picker.title("吸取屏幕颜色")
        picker.geometry("420x170")
        picker.resizable(False, False)
        picker.configure(bg=self.theme["window_bg"])
        picker.attributes("-topmost", True)

        wrap = ttk.Frame(picker, style="Root.TFrame", padding=18)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="吸取屏幕颜色", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            wrap,
            text="点击“开始吸取”后，将鼠标移动到屏幕任意位置。\n左键确定取色，右键或 ESC 取消。",
            justify="left"
        ).pack(anchor="w", pady=(10, 12))

        info_var = tk.StringVar(value="当前颜色：未选择")
        ttk.Label(wrap, textvariable=info_var).pack(anchor="w", pady=(0, 10))

        preview = tk.Label(wrap, width=16, height=2, bg="#FFFFFF", relief="solid", bd=1)
        preview.pack(anchor="w")

        state = {"running": False}

        def stop_pick():
            state["running"] = False

        def sample_loop():
            if not state["running"]:
                return
            try:
                x = picker.winfo_pointerx()
                y = picker.winfo_pointery()
                img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
                rgb = img.getpixel((0, 0))
                hex_color = self._rgb_to_hex(rgb)
                preview.configure(bg=hex_color)
                info_var.set(f"当前颜色：{hex_color}    坐标：({x}, {y})")
            except Exception:
                pass
            picker.after(60, sample_loop)

        def start_pick():
            state["running"] = True
            picker.iconify()
            self.root.after(250, sample_loop)

        def confirm_pick(event=None):
            if not state["running"]:
                return
            try:
                x = picker.winfo_pointerx()
                y = picker.winfo_pointery()
                img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1))
                rgb = img.getpixel((0, 0))
                hex_color = self._rgb_to_hex(rgb)
                stop_pick()
                picker.destroy()
                callback(hex_color)
            except Exception as e:
                stop_pick()
                picker.destroy()
                messagebox.showerror("吸取失败", str(e))

        def cancel_pick(event=None):
            stop_pick()
            try:
                picker.destroy()
            except Exception:
                pass

        btn_row = ttk.Frame(wrap, style="Root.TFrame")
        btn_row.pack(fill="x", pady=(14, 0))

        ttk.Button(btn_row, text="开始吸取", style="Primary.TButton", command=start_pick).pack(side="left")
        ttk.Button(btn_row, text="取消", command=cancel_pick).pack(side="left", padx=8)

        picker.bind("<Button-1>", confirm_pick)
        picker.bind("<Button-3>", cancel_pick)
        picker.bind("<Escape>", cancel_pick)
  
    def open_theme_settings(self):
        for child in self.right_card.winfo_children():
            child.destroy()

        container = ttk.Frame(self.right_card, style="Card.TFrame")
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container, style="Card.TFrame")
        header.pack(fill="x", pady=(0, 12))

        ttk.Label(header, text="自定义配色", style="Section.TLabel").pack(side="left")

        theme_keys = [
            ("primary", "主色调"),
            ("secondary", "副色调"),
            ("text", "文字颜色"),
            ("window_bg", "页面背景"),
            ("card_bg", "卡片背景"),
            ("title", "标题颜色"),
            ("input_bg", "输入框背景"),
            ("border", "边框颜色"),
            ("highlight", "高亮颜色"),
            ("log_bg", "日志背景"),
        ]

        row_vars = {}
        preview_blocks = {}
        value_labels = {}
        name_map = {k: v for k, v in theme_keys}

        body = ttk.Frame(container, style="Card.TFrame")
        body.pack(fill="both", expand=True)

        left_wrap = ttk.Frame(body, style="Card.TFrame")
        left_wrap.pack(side="left", fill="both", expand=True, padx=(0, 10))

        right_wrap = ttk.Frame(body, style="Card.TFrame")
        right_wrap.pack(side="right", fill="both", expand=True)

        ttk.Label(left_wrap, text="颜色设置", style="Section.TLabel").pack(anchor="w", pady=(0, 10))

        list_box = ttk.Frame(left_wrap, style="Card.TFrame")
        list_box.pack(fill="both", expand=True)

        canvas = tk.Canvas(
            list_box,
            highlightthickness=0,
            bg=self.theme["card_bg"]
        )
        scrollbar = ttk.Scrollbar(list_box, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, style="Card.TFrame")

        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(right_wrap, text="实时预览", style="Section.TLabel").pack(anchor="w", pady=(0, 10))

        preview_page = tk.Frame(right_wrap, bg=self.theme["window_bg"], bd=0, highlightthickness=0)
        preview_page.pack(fill="both", expand=True)

        preview_card = tk.Frame(
            preview_page,
            bg=self.theme["card_bg"],
            bd=1,
            relief="solid",
            highlightthickness=1
        )
        preview_card.pack(fill="both", expand=True, padx=8, pady=8)

        preview_title = tk.Label(
            preview_card,
            text="Telegram 消息监听器",
            font=("Microsoft YaHei UI", 12, "bold"),
            anchor="w"
        )
        preview_title.pack(fill="x", padx=14, pady=(14, 8))

        preview_desc = tk.Label(
            preview_card,
            text="这里是配色预览区域，点击左侧色块即可打开调色盘。",
            justify="left",
            anchor="w"
        )
        preview_desc.pack(fill="x", padx=14)

        preview_entry = tk.Entry(preview_card, relief="solid", bd=1)
        preview_entry.insert(0, "输入框效果预览")
        preview_entry.pack(fill="x", padx=14, pady=(12, 10))

        preview_button = tk.Button(
            preview_card,
            text="按钮预览",
            relief="flat",
            bd=0,
            padx=12,
            pady=8,
            cursor="hand2"
        )
        preview_button.pack(anchor="w", padx=14)

        preview_soft = tk.Label(
            preview_card,
            text="副色调区域预览",
            anchor="w",
            padx=12,
            pady=8
        )
        preview_soft.pack(fill="x", padx=14, pady=(12, 10))

        preview_log = tk.Text(preview_card, height=8, relief="solid", bd=1)
        preview_log.insert("1.0", "[12:00:00] 运行日志预览\n[12:00:01] 当前配色已更新")
        preview_log.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        def get_live_theme():
            live = {}
            for key, default_value in DEFAULT_THEME.items():
                if key in row_vars:
                    live[key] = self._normalize_hex_color(row_vars[key].get(), default_value)
                else:
                    live[key] = self._normalize_hex_color(self.theme.get(key, default_value), default_value)
            return live

        def refresh_preview():
            live = get_live_theme()

            preview_page.configure(bg=live["window_bg"])
            preview_card.configure(
                bg=live["card_bg"],
                highlightbackground=live["border"],
                highlightcolor=live["border"]
            )
            preview_title.configure(bg=live["card_bg"], fg=live["title"])
            preview_desc.configure(bg=live["card_bg"], fg=live["text"])

            preview_entry.configure(
                bg=live["input_bg"],
                fg=live["text"],
                insertbackground=live["text"],
                highlightbackground=live["border"],
                highlightcolor=live["highlight"]
            )

            preview_button.configure(
                bg=live["primary"],
                fg="#FFFFFF",
                activebackground=live["title"],
                activeforeground="#FFFFFF"
            )

            preview_soft.configure(bg=live["secondary"], fg=live["text"])

            preview_log.configure(
                bg=live["log_bg"],
                fg=live["text"],
                insertbackground=live["text"],
                highlightbackground=live["border"],
                highlightcolor=live["highlight"]
            )

            for key, _ in theme_keys:
                color = self._normalize_hex_color(row_vars[key].get(), DEFAULT_THEME[key])
                preview_blocks[key].configure(bg=color)
                value_labels[key].configure(text=color)

        def choose_color(key):
            current = self._normalize_hex_color(row_vars[key].get(), DEFAULT_THEME[key])
            color = colorchooser.askcolor(
                title=f"选择颜色 - {name_map[key]}",
                color=current
            )[1]
            if color:
                row_vars[key].set(color.upper())
                refresh_preview()

        def build_color_row(parent, key, label_text):
            row = ttk.Frame(parent, style="Card.TFrame")
            row.pack(fill="x", pady=6)

            ttk.Label(row, text=label_text, width=10, style="Card.TLabel").pack(side="left")

            var = tk.StringVar(value=self.theme.get(key, DEFAULT_THEME[key]))
            row_vars[key] = var

            entry = ttk.Entry(row, textvariable=var, width=12)
            entry.pack(side="left", padx=(8, 8))

            color_block = tk.Label(
                row,
                width=4,
                height=1,
                bg=var.get(),
                relief="solid",
                bd=1,
                cursor="hand2"
            )
            color_block.pack(side="left", padx=(0, 8))
            color_block.bind("<Button-1>", lambda e, k=key: choose_color(k))
            preview_blocks[key] = color_block

            value_label = ttk.Label(row, text=var.get(), width=10, style="Card.TLabel")
            value_label.pack(side="left", padx=(0, 8))
            value_labels[key] = value_label

            ttk.Button(row, text="选择", command=lambda k=key: choose_color(k)).pack(side="left")

            var.trace_add("write", lambda *_: refresh_preview())

        for key, label_text in theme_keys:
            build_color_row(inner, key, label_text)

        action_bar = ttk.Frame(container, style="Card.TFrame")
        action_bar.pack(fill="x", pady=(12, 0))

        def apply_theme_settings():
            for key, default_value in DEFAULT_THEME.items():
                if key in row_vars:
                    self.theme[key] = self._normalize_hex_color(row_vars[key].get(), default_value)
            self._save_theme()
            self._apply_theme_runtime()
            refresh_preview()
            messagebox.showinfo("成功", "配色已保存并应用")

        def restore_defaults():
            for key, default_value in DEFAULT_THEME.items():
                if key in row_vars:
                    row_vars[key].set(default_value)
            refresh_preview()

        ttk.Button(
            action_bar,
            text="应用并保存",
            style="Primary.TButton",
            command=apply_theme_settings
        ).pack(side="left")

        ttk.Button(
            action_bar,
            text="恢复默认",
            command=restore_defaults
        ).pack(side="left", padx=8)

        ttk.Button(
            action_bar,
            text="返回主页",
            command=self.rebuild_main_ui
        ).pack(side="right")

        refresh_preview()

    def rebuild_main_ui(self):
        selected_ids = []
        current_rule = None

        try:
            selected_ids = [int(did) for did, var in self.dialog_vars.items() if var.get()]
        except Exception:
            selected_ids = []

        try:
            current_rule = self._collect_rule()
        except Exception:
            current_rule = None

        for child in self.root.winfo_children():
            child.destroy()

        self.dialog_vars = {}
        self.dialog_items = list(self.dialog_items)

        self.dialog_canvas = None
        self.dialog_wrap = None
        self.log_text = None
        self.keyword_text = None
        self.monitor_toggle_btn = None
        self.password_row = None
        self.password_entry = None
        self.main_paned = None
        self.left_card = None
        self.left_canvas = None
        self.left_scroll = None
        self.left_inner = None
        self.left_canvas_window = None
        self.mid_card = None
        self.right_card = None
        self.settings_btn = None
        self.settings_menu = None

        self._init_style()
        self._build_ui()
        self._load_saved_config_to_form()

        if current_rule:
            self.only_selected_var.set(current_rule.get("only_selected_dialogs", True))
            self.case_sensitive_var.set(current_rule.get("case_sensitive", False))
            self.keyword_mode_var.set(current_rule.get("keyword_mode", "fuzzy"))
            self.keyword_text.delete("1.0", tk.END)
            self.keyword_text.insert("1.0", current_rule.get("keywords", ""))
            self.notify_target_var.set(current_rule.get("notify_target", "me"))

        if selected_ids:
            for item in self.dialog_items:
                self.dialog_vars[item["id"]] = tk.BooleanVar(value=(item["id"] in selected_ids))
            self.render_dialogs()

    def reset_theme(self):
        self.theme = DEFAULT_THEME.copy()
        self._save_theme()
        self._apply_theme_runtime()
        self.rebuild_main_ui()
        messagebox.showinfo("成功", "已恢复默认配色")

    def _load_saved_config_to_form(self):
        self.phone_var.set(str(self.config.data.get("last_phone", "")))

        rule = MonitorRule(**self.config.data.get("rule", {}))
        self.only_selected_var.set(rule.only_selected_dialogs)
        self.case_sensitive_var.set(rule.case_sensitive)
        self.keyword_mode_var.set(rule.keyword_mode if rule.keyword_mode in ("exact", "fuzzy") else "fuzzy")
        self.keyword_text.delete("1.0", tk.END)
        self.keyword_text.insert("1.0", rule.keywords)
        self.notify_target_var.set(rule.notify_target or "me")

        proxy = self.config.data.get("proxy", {})
        self.proxy_enabled_var.set(bool(proxy.get("enabled")))
        self.proxy_type_var.set(str(proxy.get("proxy_type", "auto") or "auto"))
        self.proxy_host_var.set(str(proxy.get("host", "")))
        self.proxy_port_var.set(str(proxy.get("port", "")))
        self.proxy_user_var.set(str(proxy.get("username", "")))
        self.proxy_pass_var.set(str(proxy.get("password", "")))
        self.session_name_var.set(sanitize_session_name(self.config.data.get("selected_session", DEFAULT_SESSION_BASENAME)))
        self.refresh_session_list()

    def _check_session_after_start(self):
        self.refresh_session_list()
        fut = self.worker.call(self.worker.check_existing_session())
        fut.add_done_callback(self._future_guard)

    def refresh_session_list(self):
        if not self.session_listbox:
            return
        current_items = list_session_names()
        self.session_listbox.delete(0, tk.END)
        selected = sanitize_session_name(self.session_name_var.get().strip())
        chosen_index = 0
        for idx, name in enumerate(current_items):
            self.session_listbox.insert(tk.END, name)
            if name == selected:
                chosen_index = idx
        if current_items:
            self.session_listbox.selection_clear(0, tk.END)
            self.session_listbox.selection_set(chosen_index)
            self.session_listbox.see(chosen_index)

    def _on_session_pick(self, event=None):
        if not self.session_listbox:
            return
        sel = self.session_listbox.curselection()
        if not sel:
            return
        self.session_name_var.set(self.session_listbox.get(sel[0]))

    def select_session_login(self):
        session_name = sanitize_session_name(self.session_name_var.get().strip())
        self.session_name_var.set(session_name)
        self.save_proxy_settings()
        fut = self.worker.call(self.worker.apply_session_selection(session_name))
        fut.add_done_callback(self._future_guard)

    def create_session_slot(self):
        session_name = sanitize_session_name(self.session_name_var.get().strip())
        self.session_name_var.set(session_name)
        self.config.data["selected_session"] = session_name
        self.config.save()
        self.refresh_session_list()
        self.append_log(f"已创建/切换 Session 槽位：{session_name}")
        self.login_state_var.set(f"当前会话：{session_name}（待登录）")

    def load_system_proxy(self):
        info = detect_system_proxy_settings()
        pac = str(info.get("pac", "")).strip()
        if pac and not info.get("host"):
            self.proxy_enabled_var.set(True)
            self.proxy_type_var.set("auto")
            self.system_proxy_status_var.set(f"系统代理：PAC {pac}")
            self.config.data.setdefault("proxy", {})["pac"] = pac
            self.append_log(f"已检测到系统 PAC 代理：{pac}")
            messagebox.showwarning("检测到 PAC", f"已检测到系统 PAC 代理：\n{pac}\n\nPAC 无法直接提取固定 IP 和端口，请手动切换为固定代理或在代理软件中查看端口。")
            return

        if info.get("host") and info.get("port"):
            self.proxy_enabled_var.set(True)
            self.proxy_type_var.set(str(info.get("proxy_type", "http")))
            self.proxy_host_var.set(str(info.get("host", "")))
            self.proxy_port_var.set(str(info.get("port", "")))
            self.proxy_user_var.set(str(info.get("username", "")))
            self.proxy_pass_var.set(str(info.get("password", "")))
            self.system_proxy_status_var.set(f"系统代理：{info.get('source', '已检测')} {info.get('host')}:{info.get('port')}")
            self.config.data.setdefault("proxy", {})["pac"] = ""
            self.append_log(f"已获取系统代理：{info.get('host')}:{info.get('port')} ({info.get('proxy_type')})")
            messagebox.showinfo("成功", f"已获取系统代理\n类型：{info.get('proxy_type')}\n地址：{info.get('host')}\n端口：{info.get('port')}")
            return

        self.system_proxy_status_var.set("系统代理：未检测到")
        self.append_log("未检测到可用的系统代理。")
        messagebox.showwarning("提示", "未检测到可用的系统代理。")

    def clear_proxy_settings(self):
        self.proxy_enabled_var.set(False)
        self.proxy_type_var.set("auto")
        self.proxy_host_var.set("")
        self.proxy_port_var.set("")
        self.proxy_user_var.set("")
        self.proxy_pass_var.set("")
        self.config.data.setdefault("proxy", {})["pac"] = ""
        self.system_proxy_status_var.set("系统代理：已清空")
        self.append_log("代理设置已清空。")

    def test_proxy_connection(self):
        self.save_proxy_settings()
        fut = self.worker.call(self.worker.test_current_connection())
        fut.add_done_callback(self._future_guard)

    def save_proxy_settings(self):
        ensure_app_dir()
        self.config.data["proxy"] = {
            "enabled": self.proxy_enabled_var.get(),
            "host": self.proxy_host_var.get().strip(),
            "port": self.proxy_port_var.get().strip(),
            "username": self.proxy_user_var.get().strip(),
            "password": self.proxy_pass_var.get().strip(),
            "proxy_type": self.proxy_type_var.get().strip() or "auto",
            "pac": str(self.config.data.get("proxy", {}).get("pac", "")),
        }
        self.config.data["selected_session"] = sanitize_session_name(self.session_name_var.get().strip())
        self.config.save()
        self.append_log("连接设置已保存。")

    def refresh_connection(self):
        self.save_proxy_settings()
        fut = self.worker.call(self.worker.refresh_client())
        fut.add_done_callback(self._future_guard)

    def send_code(self):
        ensure_app_dir()
        self.save_proxy_settings()
        phone = self.phone_var.get().strip()
        self.config.data["last_phone"] = phone
        self.config.data["selected_session"] = sanitize_session_name(self.session_name_var.get().strip())
        self.config.save()
        fut = self.worker.call(self.worker.send_code(phone))
        fut.add_done_callback(self._future_guard)

    def login_by_code(self):
        code = self.code_var.get().strip()
        fut = self.worker.call(self.worker.sign_in_with_code(code))
        fut.add_done_callback(self._future_guard)

    def login_by_password(self):
        pwd = self.password_var.get().strip()
        fut = self.worker.call(self.worker.sign_in_with_password(pwd))
        fut.add_done_callback(self._future_guard)

    def logout(self):
        fut = self.worker.call(self.worker.logout())
        fut.add_done_callback(self._future_guard)

    def load_dialogs(self):
        fut = self.worker.call(self.worker.load_dialogs())
        fut.add_done_callback(self._future_guard)

    def _future_guard(self, fut):
        try:
            fut.result()
        except Exception as e:
            self.ui_queue.put(("error", str(e)))

    def render_dialogs(self):
        kw = self.dialog_search_var.get().strip().lower()

        for child in self.dialog_wrap.winfo_children():
            child.destroy()

        for item in self.dialog_items:
            hay = f"{item['name']} {item['username']} {item['id']} {item['type']}".lower()
            if kw and kw not in hay:
                continue

            var = self.dialog_vars.setdefault(item["id"], tk.BooleanVar(value=False))
            row = ttk.Frame(self.dialog_wrap, style="Card.TFrame")
            row.pack(fill="x", pady=3)

            ttk.Checkbutton(row, variable=var).pack(side="left")

            txt = f"[{item['type']}] {item['name']}"
            if item["username"]:
                txt += f" @{item['username']}"
            txt += f" ({item['id']})"

            ttk.Label(row, text=txt, style="Card.TLabel").pack(side="left", anchor="w")

    def select_all_dialogs(self):
        for item in self.dialog_items:
            self.dialog_vars.setdefault(item["id"], tk.BooleanVar(value=False)).set(True)

    def clear_dialogs(self):
        for var in self.dialog_vars.values():
            var.set(False)

    def _collect_rule(self) -> dict:
        selected_ids = [int(did) for did, var in self.dialog_vars.items() if var.get()]
        return asdict(MonitorRule(
            keyword_mode=self.keyword_mode_var.get(),
            keywords=self.keyword_text.get("1.0", tk.END).strip(),
            notify_target=(self.notify_target_var.get().strip() or "me"),
            only_selected_dialogs=self.only_selected_var.get(),
            case_sensitive=self.case_sensitive_var.get(),
            selected_dialog_ids=selected_ids,
        ))

    def toggle_monitor(self):
        rule = self._collect_rule()
        fut = self.worker.call(self.worker.toggle_monitor(rule))
        fut.add_done_callback(self._future_guard)

    def export_config(self):
        ensure_app_dir()
        self.config.data["last_phone"] = self.phone_var.get().strip()
        self.config.data["rule"] = self._collect_rule()
        self.save_proxy_settings()
        self._save_theme()

        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return

        Path(path).write_text(json.dumps(self.config.data, ensure_ascii=False, indent=2), "utf-8")
        self.append_log(f"配置已导出: {path}")

    def import_config(self):
        ensure_app_dir()
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return

        try:
            self.config.data.update(json.loads(Path(path).read_text("utf-8")))
            self.config.save()
            self.theme = self._build_theme()
            self._init_style()
            self._load_saved_config_to_form()
            self._rebuild_text_widgets_theme()
            self._refresh_menu_theme()
            self.append_log(f"配置已导入: {path}")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def append_log(self, text: str):
        self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log_text.see(tk.END)

    def _show_password_input(self, show: bool):
        if show:
            self.password_row.pack(side="left", padx=(6, 0))
            self.password_entry.focus_set()
        else:
            self.password_var.set("")
            self.password_row.pack_forget()

    def _set_monitor_state(self, enabled: bool):
        if enabled:
            self.monitor_state_var.set("已开启")
            self.monitor_toggle_btn.config(text="关闭监听")
        else:
            self.monitor_state_var.set("未开启")
            self.monitor_toggle_btn.config(text="开启监听")

    def _pump_ui_queue(self):
        while True:
            try:
                event, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if event == "log":
                self.append_log(payload)

            elif event == "error":
                self.append_log(f"错误: {payload}")
                messagebox.showerror("错误", payload)

            elif event == "login_ok":
                session_name = payload.get("session_name") if isinstance(payload, dict) else ""
                if session_name:
                    self.session_name_var.set(session_name)
                    self.refresh_session_list()
                    self.login_state_var.set(f"已登录：{payload['name']} | 会话：{session_name}")
                else:
                    self.login_state_var.set(f"已登录：{payload['name']}")
                self._show_password_input(False)
                messagebox.showinfo("成功", "Telegram 登录成功")

            elif event == "logged_out":
                self.login_state_var.set("未登录")
                self._show_password_input(False)

            elif event == "code_sent":
                self.login_state_var.set(f"验证码已发送：{payload['phone']}")
                self._show_password_input(False)
                messagebox.showinfo("提示", f"验证码已发送，请去 Telegram 客户端查看。\n{payload['phone']}")

            elif event == "session_list":
                selected = payload.get("selected", "") if isinstance(payload, dict) else ""
                if selected:
                    self.session_name_var.set(selected)
                self.refresh_session_list()

            elif event == "session_selected":
                self.session_name_var.set(str(payload))
                self.refresh_session_list()
                self.append_log(f"当前 Session 已切换：{payload}")

            elif event == "connection_test_ok":
                session_name = payload.get("session_name", "")
                login_name = payload.get("login_name", "未登录")
                proxy_type = payload.get("proxy_type", "")
                messagebox.showinfo("连接测试成功", f"当前会话：{session_name}\n代理类型：{proxy_type}\n登录状态：{login_name}")

            elif event == "dialogs":
                prev_selected = set(int(x) for x in self.config.data.get("rule", {}).get("selected_dialog_ids", []))
                self.dialog_items = payload
                for item in payload:
                    self.dialog_vars.setdefault(item["id"], tk.BooleanVar(value=(item["id"] in prev_selected)))
                self.render_dialogs()

            elif event == "need_password":
                self.login_state_var.set("需要 2FA 密码")
                self._show_password_input(True)
                messagebox.showwarning("需要 2FA", "该账号已开启两步验证，请输入 2FA 密码后点击“2FA登录”")

            elif event == "monitor_state":
                self._set_monitor_state(bool(payload))
                if payload:
                    self.append_log("监听成功，已进入实时监听状态。")
                else:
                    self.append_log("监听已关闭。")

            elif event == "hit":
                self.append_log(
                    f"监听命中 | 类型:{payload['chat_type']} | 会话:{payload['chat_name']} | "
                    f"匹配:{payload['match_type']} | 关键词:{payload['keyword']} | "
                    f"发送人:{payload['sender']} | 时间:{payload['time']} | 内容:{payload['text']}"
                )

        self.root.after(200, self._pump_ui_queue)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()