import asyncio

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.all import EventMessageType
from astrbot.api import logger
from astrbot.api.message_components import At

import auto_reply
from config import get_memory_db_path


class AutoReplyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.enabled = True

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.enabled:
            return

        msg_obj = event.message_obj
        if msg_obj is None:
            return

        sender = msg_obj.sender
        user_id = str(sender.user_id) if sender else ""
        user_name = sender.nickname if sender else "未知"
        content = event.message_str or ""

        if not content.strip():
            return

        if msg_obj.self_id:
            bot_qq = str(msg_obj.self_id)
            auto_reply.BOT_QQ = bot_qq

        if user_id == auto_reply.BOT_QQ:
            return

        logger.info(f"[群 {msg_obj.group_id}] [{user_name}({user_id})]: {content[:60]}")

        group_id = str(msg_obj.group_id) if msg_obj.group_id else "unknown"
        chat_key = f"group_{group_id}"
        db_path = get_memory_db_path(chat_key)

        try:
            reply = await asyncio.to_thread(
                auto_reply.handle_message,
                user_id=user_id,
                user_name=user_name,
                content=content,
                db_path=db_path,
            )
        except Exception as e:
            logger.error(f"handle_message 异常: {e}")
            return

        if reply and reply.strip():
            event.stop_event()
            yield event.plain_result(reply)

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        if not self.enabled:
            return

        msg_obj = event.message_obj
        if msg_obj is None:
            return

        sender = msg_obj.sender
        user_id = str(sender.user_id) if sender else ""
        user_name = sender.nickname if sender else "未知"
        content = event.message_str or ""

        if not content.strip():
            return

        if msg_obj.self_id:
            auto_reply.BOT_QQ = str(msg_obj.self_id)

        if user_id == auto_reply.BOT_QQ:
            return

        logger.info(f"[私聊] [{user_name}({user_id})]: {content[:60]}")

        private_id = user_id if user_id else "unknown"
        chat_key = f"private_{private_id}"
        db_path = get_memory_db_path(chat_key)

        try:
            reply = await asyncio.to_thread(
                auto_reply.handle_message,
                user_id=user_id,
                user_name=user_name,
                content=content,
                db_path=db_path,
            )
        except Exception as e:
            logger.error(f"handle_message 异常: {e}")
            return

        if reply and reply.strip():
            event.stop_event()
            yield event.plain_result(reply)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("autoreply")
    async def cmd_autoreply(self, event: AstrMessageEvent, action: str = ""):
        if action == "off":
            self.enabled = False
            yield event.plain_result("[AutoReply] 已关闭自动回复")
        elif action == "on":
            self.enabled = True
            yield event.plain_result("[AutoReply] 已开启自动回复")
        else:
            status = "开启" if self.enabled else "关闭"
            yield event.plain_result(
                f"[AutoReply] 状态: {status}\n"
                f"记忆库目录: {auto_reply._MEMORY_DB_PATH}\n"
                f"LLM: {auto_reply.LLM_MODEL_NAME}"
            )

    async def terminate(self):
        self.enabled = False