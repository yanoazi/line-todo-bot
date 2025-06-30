# app.py (v2.5.0 - No Guided Flow, No Priority, Private Task Records)
from flask import Flask, request, abort, jsonify
import os
import json
import random
import re
from typing import List, Optional, Dict, Any, Set
from datetime import datetime, timezone, date, timedelta # Added date
import logging
from dotenv import load_dotenv
import inspect

from models import (
    init_db, get_db, Member, Task,
    get_member_by_name_and_group, get_member_by_id, get_task_by_id,
    get_pending_tasks_by_group_id,
    get_pending_tasks_by_user_id,
    get_completed_tasks_by_user_id, # New for #紀錄
    create_member, create_task
)
from sqlalchemy import text, or_, orm
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)

app = Flask(__name__)
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
DATABASE_URL = os.environ.get('DATABASE_URL')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY') 

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    logger.error("環境變數 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET 未設定")
    exit(1)
if not DATABASE_URL:
    logger.error("環境變數 DATABASE_URL 未設定")
    exit(1)

IN_REPLIT = os.environ.get('REPL_ID') is not None
if IN_REPLIT:
    logger.info("在 Replit 環境中運行。")

try:
    line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(CHANNEL_SECRET)
except Exception as e:
    logger.exception(f"初始化 LINE SDK 失敗: {e}")
    exit(1)

try:
    init_db()
    logger.info("資料庫初始化檢查完成。")
except Exception as e:
    logger.exception(f"資料庫初始化失敗: {e}")

# --- Regex Patterns (Priority removed) ---
# Mentions ((?:@\S+\s*)*) are optional
ADD_TASK_PATTERN = r'#新增\s*((?:@\S+\s*)*)(.+?)(?:\s+(\d{4}/\d{1,2}/\d{1,2}))?$'
COMPLETE_TASK_PATTERN = r'#完成\s+T-(\d+)$'
LIST_TASK_PATTERN = r'#列表\s*(?:@(\S+))?$'
DELETE_TASK_PATTERN = r'#刪除\s+T-(\d+)$'
EDIT_TASK_PATTERN = r'#修改\s+T-(\d+)\s+(.+?)(?:\s*(\d{4}/\d{1,2}/\d{1,2}))?$' # No priority
DETAIL_TASK_PATTERN = r'#詳情\s+T-(\d+)$'
BATCH_ADD_TASK_PATTERN = r'#批量新增(?:\s+((?:@\S+\s*)+))?\s*\n(.+)$' # No priority in lines
DRAW_LOTS_PATTERN = r'#擲筊\s+(.+)$'
RANDOM_PICK_PATTERN = r'#抽籤\s+(.+)$'
RECORD_LIST_PATTERN = r'^#紀錄$' # New command for completed private tasks

def parse_mentioned_member_names(mention_block: Optional[str]) -> Set[str]:
    if not mention_block:
        return set()
    mentions = re.findall(r'@(\S+)', mention_block)
    return {name.strip() for name in mentions if name.strip()}

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    logger.debug(f"Request body: {body}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("Invalid signature.")
        abort(400)
    except Exception as e:
        logger.exception(f"處理回調時發生未預期錯誤: {e}")
        abort(500)
    return 'OK'

@app.route("/ping", methods=['GET'])
def ping():
    db_ok = False; db_error = None
    try:
        with get_db() as db: db.execute(text("SELECT 1")); db_ok = True
    except Exception as e: logger.error(f"Ping DB check failed: {e}"); db_error = str(e)
    return jsonify({
        "status": "ok", 
        "message": "LINE Bot running (v2.5.0 - No Guided Flow/Priority, Private Records)", 
        "timestamp": datetime.now(timezone.utc).isoformat(), 
        "db_connection": "ok" if db_ok else "error", 
        "db_error": db_error
    })

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
    text = event.message.text.strip()
    # reply_token = event.reply_token # Original line
    user_id = event.source.user_id

    # --- Add check for reply_token ---
    if not event.reply_token or not isinstance(event.reply_token, str) or event.reply_token == "<no-reply>": # Check for None, non-string, or placeholder
        logger.error(f"Invalid or missing reply_token for event from user {user_id}. Event type: {type(event)}, Reply token: '{event.reply_token}'")
        # If no valid reply token, we usually cannot proceed to reply.
        # Depending on the event, this might be normal (e.g., an "unsend" event if handled here).
        # For a TextMessage meant to be replied to, this is an issue.
        return 

    reply_token: str = event.reply_token # Now explicitly typed and checked before use

    is_private_chat = event.source.type == 'user'
    group_id: Optional[str] = None

    if is_private_chat:
        logger.info(f"Received private message from User {user_id}: '{text}'")
    elif event.source.type == 'group':
        group_id = event.source.group_id
        logger.info(f"Received from Group ID {group_id} by User {user_id}: '{text}'")
    elif event.source.type == 'room':
        group_id = event.source.room_id
        logger.info(f"Received from Room ID {group_id} by User {user_id}: '{text}'")
    else:
        logger.info(f"Ignoring message from unknown source type: {event.source.type}")
        return

    try:
        with get_db() as db:
            add_match = re.match(ADD_TASK_PATTERN, text)
            complete_match = re.match(COMPLETE_TASK_PATTERN, text)
            list_match = re.match(LIST_TASK_PATTERN, text)
            delete_match = re.match(DELETE_TASK_PATTERN, text)
            edit_match = re.match(EDIT_TASK_PATTERN, text)
            detail_match = re.match(DETAIL_TASK_PATTERN, text)
            draw_match = re.match(DRAW_LOTS_PATTERN, text)
            pick_match = re.match(RANDOM_PICK_PATTERN, text)
            batch_add_match = re.match(BATCH_ADD_TASK_PATTERN, text, re.DOTALL)
            record_list_match = re.match(RECORD_LIST_PATTERN, text)


            if add_match:
                handle_add_task(reply_token, add_match, user_id, db, is_private_chat, group_id)
            elif complete_match:
                handle_complete_task(reply_token, complete_match, user_id, db, is_private_chat, group_id)
            elif list_match:
                handle_list_tasks(reply_token, list_match, user_id, db, is_private_chat, group_id)
            elif record_list_match:
                if is_private_chat:
                    handle_record_list(reply_token, user_id, db)
                else:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="#紀錄 指令僅限私人聊天使用。"))
            elif delete_match:
                handle_delete_task(reply_token, delete_match, user_id, db, is_private_chat, group_id)
            elif edit_match:
                handle_edit_task(reply_token, edit_match, user_id, db, is_private_chat, group_id)
            elif detail_match:
                handle_task_details(reply_token, detail_match, user_id, db, is_private_chat, group_id)
            elif draw_match:
                handle_draw_lots(reply_token, draw_match)
            elif pick_match:
                handle_random_pick(reply_token, pick_match)
            elif batch_add_match:
                handle_batch_add_tasks(reply_token, batch_add_match, user_id, db, is_private_chat, group_id)
            elif text == "#幫助":
                send_help_message_v250(reply_token, is_private_chat)
            elif text == "#幫助新增":
                send_add_help_message_v250(reply_token, is_private_chat)
            elif text.startswith("#編輯幫助 T-"):
                task_id_match = re.match(r'#編輯幫助 T-(\d+)', text)
                if task_id_match:
                    send_edit_help_message_v250(reply_token, task_id_match.group(1))
                else:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text="指令格式錯誤..."))
            else:
                if is_private_chat and not text.startswith("#"):
                     line_bot_api.reply_message(reply_token, TextSendMessage(text="您好！請輸入 #幫助 查看可用指令。"))
                logger.info(f"Unmatched command/text in {'private' if is_private_chat else 'group'} chat.")

    except SQLAlchemyError as db_err:
        logger.exception(f"DB錯誤: {db_err}")
        try: 
            if reply_token: # Check again, just in case, before trying to reply with error
                line_bot_api.reply_message(reply_token, TextSendMessage(text=f"處理您的請求時發生資料庫錯誤。"))
        except Exception as reply_err: 
            logger.error(f"回覆DB錯誤訊息失敗: {reply_err}")
    except Exception as e:
        logger.exception(f"未預期錯誤: {e}")
        try: 
            if reply_token: # Check again
                line_bot_api.reply_message(reply_token, TextSendMessage(text=f"處理您的請求時發生內部錯誤。"))
        except Exception as reply_err: 
            logger.error(f"回覆內部錯誤訊息失敗: {reply_err}")


def handle_add_task(
    reply_token: str, match: re.Match, 
    adder_user_id: str, db: Session, 
    is_private_chat: bool, group_id_context: Optional[str]
):
    mention_block = match.group(1) 
    task_content = match.group(2).strip() # Group 2 is now content
    due_date_str = match.group(3)       # Group 3 is now date

    if not task_content:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增失敗：任務內容不能為空。"))
        return

    task_args = { "content": task_content, "status": 'pending' }

    due_date = parse_date(due_date_str)
    if due_date_str and due_date is None:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"新增失敗：日期格式不正確 ({due_date_str})。"))
        return
    task_args["due_date"] = due_date

    members_display_final = "我 (私人任務)"
    failed_member_names_creation: List[str] = []

    if is_private_chat:
        task_args["owner_user_id"] = adder_user_id
        if mention_block and mention_block.strip():
            line_bot_api.push_message(adder_user_id, TextSendMessage(text="提示：在私人聊天中新增任務時，@提及成員將被忽略。"))
    else: 
        if not group_id_context:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="新增失敗：缺少群組資訊。"))
            return

        member_names_to_assign = parse_mentioned_member_names(mention_block)
        if not member_names_to_assign:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="新增失敗：請至少 @提及 一位成員。"))
            return

        members_to_assign_obj: List[Member] = []
        for name in member_names_to_assign:
            member = get_member_by_name_and_group(db, name=name, group_id=group_id_context)
            if not member:
                try:
                    member = create_member(db, name=name, group_id=group_id_context)
                    members_to_assign_obj.append(member)
                except Exception as create_err:
                    logger.warning(f"指令新增任務時建立成員 '{name}' 失敗: {create_err}")
                    failed_member_names_creation.append(name)
            else:
                members_to_assign_obj.append(member)

        if not members_to_assign_obj:
            error_msg = "新增失敗：無法找到或建立任何指定的成員。"
            if failed_member_names_creation: error_msg += f" (嘗試建立失敗: {', '.join(failed_member_names_creation)})"
            line_bot_api.reply_message(reply_token, TextSendMessage(text=error_msg))
            db.rollback(); return

        task_args["group_id"] = group_id_context
        task_args["members"] = members_to_assign_obj
        members_display_final = ', '.join([f'@{m.name}' for m in members_to_assign_obj])

    try:
        task = create_task(db=db, **task_args)
        task_id_str = f"T-{task.id}"
        due_date_display = due_date.strftime('%Y/%m/%d') if due_date else '無'
        owner_desc = "您的私人" if is_private_chat else f"為 {members_display_final}"

        reply_text = (f"✅ 已建立{owner_desc}任務！\n"
                      f"內容：{task.content}\n"
                      f"任務ID：{task_id_str}\n"
                      f"截止：{due_date_display}")
        if failed_member_names_creation:
             reply_text += f"\n⚠️ 注意：無法建立成員：{', '.join(failed_member_names_creation)}"

        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
        logger.info(f"成功為 {members_display_final} 建立任務 T-{task.id} (指令)")

    except Exception as e:
        logger.exception(f"指令新增任務時錯誤: {e}")
        db.rollback()
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"新增任務失敗: {e}"))


def handle_complete_task(
    reply_token: str, match: re.Match, 
    completer_user_id: str, db: Session, 
    is_private_chat: bool, group_id_context: Optional[str]
):
    task_id_num = int(match.group(1))
    load_opts = [joinedload(Task.members)] if not is_private_chat else []
    task = get_task_by_id(db, task_id=task_id_num, options=load_opts)
    reply_text = ""

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    # Permission checks
    elif is_private_chat and task.user_id != completer_user_id:
        reply_text = f"❌ 任務 T-{task_id_num} 不屬於您，無法完成。"
    elif not is_private_chat and task.group_id != group_id_context:
        reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間，無法完成。"

    if not reply_text: # No permission error yet
        if task.status == 'completed':
            reply_text = f"ℹ️ 任務 T-{task_id_num} ({task.content[:15]}...) 已經是完成狀態。"
        else: # Is pending
            try:
                task.status = 'completed'
                task.completed_at = datetime.now(timezone.utc)

                on_time_status_msg = ""
                if task.user_id: # It's a private task, calculate on_time
                    if task.due_date:
                        # Ensure due_date is treated as end of day for comparison if only date is relevant
                        due_date_end_of_day = datetime.combine(task.due_date.date(), datetime.max.time(), tzinfo=task.due_date.tzinfo or timezone.utc)
                        task.completed_on_time = task.completed_at <= due_date_end_of_day
                        on_time_status_msg = " (如期完成)" if task.completed_on_time else " (逾期完成)"
                    else: # No due date
                        task.completed_on_time = True # Or None, by convention True for no due date
                        on_time_status_msg = " (完成)" 

                db.commit()

                members_display = "您"
                if task.group_id and task.members: 
                    members_display = ', '.join([f'@{m.name}' for m in task.members])

                reply_text = f"🎉 已將任務 T-{task_id_num} 標記為完成{on_time_status_msg}！\n"
                if task.group_id:
                    reply_text += f"負責人: {members_display}\n"
                reply_text += f"內容：{task.content}"

                logger.info(f"使用者 {completer_user_id} 在 {'private' if is_private_chat else 'group '+str(group_id_context)} 完成了任務 T-{task.id}{on_time_status_msg}")
            except Exception as e:
                logger.exception(f"完成任務 T-{task_id_num} DB/Logic失敗: {e}"); db.rollback()
                reply_text = f"❌ 更新任務 T-{task_id_num} 狀態失敗。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_list_tasks( # For pending tasks
    reply_token: str, match: re.Match, 
    lister_user_id: str, db: Session, 
    is_private_chat: bool, group_id_context: Optional[str]
):
    member_name_filter = match.group(1)
    tasks: List[Task] = []
    title = ""

    try:
        if is_private_chat:
            if member_name_filter:
                line_bot_api.push_message(lister_user_id, TextSendMessage(text="提示：在私人聊天中，#列表 指令不支援指定成員。"))
            tasks = get_pending_tasks_by_user_id(db, user_id=lister_user_id)
            title = "📝 我的待辦事項"
        else: # Group chat
            if not group_id_context: 
                line_bot_api.reply_message(reply_token, TextSendMessage(text="錯誤：缺少群組資訊。")); return

            query = db.query(Task).options(joinedload(Task.members)).filter(Task.status == 'pending')
            if member_name_filter:
                target_member = get_member_by_name_and_group(db, name=member_name_filter, group_id=group_id_context)
                if not target_member:
                    line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到成員：{member_name_filter}")); return
                query = query.filter(Task.group_id == group_id_context, Task.members.any(id=target_member.id))
                title = f"{member_name_filter} 的待辦事項 (在本群組)"
            else:
                query = query.filter(Task.group_id == group_id_context)
                title = "本群組待辦事項"
            tasks = query.order_by(Task.due_date.asc().nulls_last(), Task.created_at.asc()).all()

        if not tasks:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"✅ {title}：目前沒有待辦任務！"))
            return

        # Pass is_private_chat for display formatting (e.g., overdue status)
        bubble_json = create_task_list_bubble(title, tasks, is_private_chat) 
        line_bot_api.reply_message(reply_token, messages=[FlexSendMessage(alt_text=title, contents=bubble_json)])
    except Exception as e:
        logger.exception(f"創建/發送 Flex 列表失敗: {e}。嘗試文字列表。")
        task_list_text = create_task_list_text(title, tasks, is_private_chat)
        # ... (message splitting logic as before)
        max_len = 4900 
        messages_to_send = []
        current_message = ""
        for line in task_list_text.splitlines(keepends=True):
            if len(current_message) + len(line) > max_len:
                messages_to_send.append(TextSendMessage(text=current_message.strip()))
                current_message = line
            else:
                current_message += line
        if current_message.strip():
             messages_to_send.append(TextSendMessage(text=current_message.strip()))
        if messages_to_send:
            line_bot_api.reply_message(reply_token, messages=messages_to_send)
        else: 
            line_bot_api.reply_message(reply_token, TextSendMessage(text="無法生成任務列表。"))


def handle_record_list(reply_token: str, user_id: str, db: Session):
    """Handles #紀錄 for listing completed private tasks."""
    if not user_id: # Should always have user_id here
        logger.error("#紀錄 called without user_id.")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="無法查詢紀錄，用戶資訊錯誤。"))
        return

    logger.info(f"使用者 {user_id} 查詢私人完成紀錄。")
    try:
        completed_tasks = get_completed_tasks_by_user_id(db, user_id)
        title = "📊 我的完成紀錄"

        if not completed_tasks:
            line_bot_api.reply_message(reply_token, TextSendMessage(text=f"✅ {title}：目前沒有已完成的私人任務紀錄。"))
            return

        # Create Flex or Text message for records
        try:
            bubble_json = create_record_list_bubble(title, completed_tasks)
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=title, contents=bubble_json))
        except Exception as e_flex:
            logger.exception(f"創建/發送 Flex 紀錄列表失敗: {e_flex}。嘗試文字列表。")
            text_list = create_record_list_text(title, completed_tasks)
            # ... (message splitting logic as in handle_list_tasks)
            max_len = 4900; messages_to_send = []; current_message = ""
            for line in text_list.splitlines(keepends=True):
                if len(current_message) + len(line) > max_len:
                    messages_to_send.append(TextSendMessage(text=current_message.strip())); current_message = line
                else: current_message += line
            if current_message.strip(): messages_to_send.append(TextSendMessage(text=current_message.strip()))
            if messages_to_send: line_bot_api.reply_message(reply_token, messages=messages_to_send)
            else: line_bot_api.reply_message(reply_token, TextSendMessage(text="無法生成紀錄列表。"))

    except SQLAlchemyError as e_db:
        logger.exception(f"查詢 #紀錄 DB失敗: {e_db}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="查詢紀錄時發生資料庫錯誤。"))
    except Exception as e_gen:
        logger.exception(f"處理 #紀錄 時發生未知錯誤: {e_gen}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="處理紀錄請求時發生內部錯誤。"))


def handle_delete_task(
    reply_token: str, match: re.Match, 
    deleter_user_id: str, db: Session, 
    is_private_chat: bool, group_id_context: Optional[str]
):
    # ... (permission checks as before, no changes needed due to priority removal) ...
    task_id_num = int(match.group(1))
    load_opts = [joinedload(Task.members)] if not is_private_chat else []
    task = get_task_by_id(db, task_id=task_id_num, options=load_opts)
    reply_text = ""

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif is_private_chat and task.user_id != deleter_user_id:
        reply_text = f"❌ 任務 T-{task_id_num} 不屬於您，無法刪除。"
    elif not is_private_chat and task.group_id != group_id_context:
        reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間，無法刪除。"

    if not reply_text: 
        try:
            task_content_preview = task.content[:20]
            owner_desc = "您的私人任務" if task.user_id else f"群組任務 (負責人: {', '.join([f'@{m.name}' for m in task.members]) if task.members else '無'})"
            db.delete(task)
            db.commit()
            reply_text = f"🗑️ 已成功刪除 {owner_desc} T-{task_id_num}。\n內容: {task_content_preview}..."
            logger.info(f"使用者 {deleter_user_id} 在 {'private' if is_private_chat else 'group '+str(group_id_context)} 刪除了任務 T-{task.id}")
        except Exception as e:
            logger.exception(f"刪除任務 T-{task_id_num} 失敗: {e}"); db.rollback()
            reply_text = f"❌ 刪除任務 T-{task_id_num} 失敗。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_edit_task(
    reply_token: str, match: re.Match, 
    editor_user_id: str, db: Session, 
    is_private_chat: bool, group_id_context: Optional[str]
):
    task_id_num = int(match.group(1))
    new_content = match.group(2).strip()   # Group 2 is content
    new_due_date_str = match.group(3) # Group 3 is date

    load_opts = [joinedload(Task.members)] if not is_private_chat else []
    task = get_task_by_id(db, task_id=task_id_num, options=load_opts)
    reply_text = ""

    if not task:
        reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    # ... (permission checks as before) ...
    elif is_private_chat and task.user_id != editor_user_id:
        reply_text = f"❌ 任務 T-{task_id_num} 不屬於您，無法編輯。"
    elif not is_private_chat and task.group_id != group_id_context:
         reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間，無法編輯。"

    if not reply_text: 
        if not new_content:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="❌ 修改任務時，任務內容不能為空。")); return

        updates = {'content': new_content}
        new_due_date = parse_date(new_due_date_str)
        if new_due_date_str and new_due_date is None: # Date provided but invalid
            line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確，請使用 YYYY/MM/DD。")); return

        # Allow clearing date if new_due_date_str is empty (but regex makes it optional, so it'd be None)
        # Or if user explicitly types "無" or similar (not supported by current regex for edit)
        if new_due_date_str is not None: # If date part was in the command
             updates['due_date'] = new_due_date # This will be None if date was invalid and already handled, or a datetime

        try:
            task.content = updates['content']
            if 'due_date' in updates: # Only update if key exists (means date was part of command)
                task.due_date = updates['due_date']

            # If task was completed, editing it might reset its 'completed_on_time' status if due date changes
            # For simplicity, let's assume editing a task (especially content/date) makes it 'pending' again if it was 'completed'.
            # Or, we prevent editing completed tasks. For now, let's allow edits.
            # If it was completed, and due_date changed, completed_on_time might become invalid.
            # Let's reset completed_on_time if due_date changes significantly for a completed task.
            # Or simpler: editing a task (content/date) usually implies it's active again.
            # If we want to keep it completed, perhaps a different command.
            # For now, editing doesn't change 'status' or 'completed_on_time' directly. User must re-complete.

            db.commit()
            due_date_text = f"截止：{task.due_date.strftime('%Y/%m/%d')}" if task.due_date else "截止：無"
            owner_desc = "您的私人任務"
            if task.group_id:
                members_display = ', '.join([f'@{m.name}' for m in task.members]) if task.members else "未指定"
                owner_desc = f"群組任務 (負責人: {members_display})"

            reply_text = (f"✏️ 已更新 {owner_desc} T-{task_id_num}！\n"
                          f"內容：{task.content}\n"
                          f"{due_date_text}")
            logger.info(f"使用者 {editor_user_id} 在 {'private' if is_private_chat else 'group '+str(group_id_context)} 修改了任務 T-{task.id}")

        except Exception as e:
            logger.exception(f"修改任務 T-{task_id_num} 失敗: {e}"); db.rollback()
            reply_text = f"❌ 修改任務 T-{task_id_num} 失敗。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_task_details(
    reply_token: str, match: re.Match, 
    viewer_user_id: str, db: Session, 
    is_private_chat: bool, group_id_context: Optional[str]
):
    task_id_num = int(match.group(1))
    load_opts = [joinedload(Task.members)] if not is_private_chat or (is_private_chat and group_id_context) else []
    task = db.query(Task).options(*load_opts).filter(Task.id == task_id_num).first()

    if not task:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"❌ 找不到ID為 T-{task_id_num} 的任務。")); return

    # ... (Permission Check as before) ...
    can_view = False
    if is_private_chat:
        if task.user_id == viewer_user_id: can_view = True
    else: 
        if task.group_id == group_id_context: can_view = True
    if not can_view:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"❌ 您無權查看任務 T-{task_id_num} 的詳情。")); return

    members_display = "我 (私人任務)"
    if task.group_id:
        members_display = (', '.join([f'@{m.name}' for m in task.members]) if task.members 
                           else "未指定負責人")

    created_at_str = task.created_at.astimezone(timezone.utc).strftime('%Y/%m/%d %H:%M') if task.created_at else "未知"
    due_date_str = task.due_date.strftime('%Y/%m/%d') if task.due_date else "無"

    status_display = ""
    status_color = "#888888" # Default

    if task.status == 'completed':
        completed_at_str = task.completed_at.astimezone(timezone.utc).strftime('%Y/%m/%d %H:%M') if task.completed_at else ""
        status_display = f"✅ 已完成 (於 {completed_at_str})"
        status_color = "#28a745"
        if task.user_id: # Private task, show on_time status
            if task.completed_on_time is True:
                status_display = f"✅ 如期完成 (於 {completed_at_str})"
            elif task.completed_on_time is False:
                status_display = f"⚠️ 逾期完成 (於 {completed_at_str})"
                status_color = "#ffc107" # Yellow for late completion
            # If completed_on_time is None (e.g. no due date), it just shows "已完成"
    elif task.status == 'pending':
        status_display = "⏳ 待辦中"
        status_color = "#ffc107" # Yellow for pending
        if task.due_date and task.due_date.astimezone(timezone.utc) < datetime.now(timezone.utc):
            status_display += " (🔴 已逾期)"
            status_color = "#dc3545" # Red for overdue pending

    owner_type_display = "私人任務" if task.user_id else f"群組任務 (ID: {task.group_id or 'N/A'})"

    try:
        contents = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"任務詳情 T-{task.id}", "weight": "bold", "size": "lg"}]},
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": task.content or "(無內容)", "wrap": True, "weight": "bold", "size": "xl"},
                    {"type": "box", "layout": "baseline", "margin": "md", "contents": [
                        {"type": "text", "text": "類型:", "size": "sm", "color": "#888888", "flex": 2},
                        {"type": "text", "text": owner_type_display, "size": "sm", "color": "#555555", "flex": 4, "weight":"bold", "wrap": True}
                    ]},
                    {"type": "box", "layout": "baseline", "margin": "md", "contents": [
                        {"type": "text", "text": "負責人:", "size": "sm", "color": "#888888", "flex": 2},
                        {"type": "text", "text": members_display, "size": "sm", "color": "#1DB446" if task.group_id else "#555555", "flex": 4, "weight":"bold", "wrap": True}
                    ]},
                    {"type": "box", "layout": "baseline", "margin": "sm", "contents": [
                        {"type": "text", "text": "狀態:", "size": "sm", "color": "#888888", "flex": 2},
                        {"type": "text", "text": status_display, "size": "sm", "color": status_color, "flex": 4, "weight":"bold", "wrap":True}
                    ]},
                    {"type": "box", "layout": "baseline", "margin": "sm", "contents": [
                        {"type": "text", "text": "截止日期:", "size": "sm", "color": "#888888", "flex": 2},
                        {"type": "text", "text": due_date_str, "size": "sm", "color": "#888888", "flex": 4}
                    ]},
                    {"type": "box", "layout": "baseline", "margin": "sm", "contents": [
                         {"type": "text", "text": "建立時間:", "size": "sm", "color": "#888888", "flex": 2},
                         {"type": "text", "text": created_at_str, "size": "sm", "color": "#888888", "flex": 4}
                    ]},
                ]
            },
            "footer": { "type": "box", "layout": "vertical", "spacing": "sm", "contents": [] }
        }

        footer_buttons = contents["footer"]["contents"]
        if task.status == 'pending':
             footer_buttons.append({
                 "type": "button", "style": "primary", "color": "#28a745", "height": "sm",
                 "action": {"type": "message", "label": "✅ 完成任務", "text": f"#完成 T-{task.id}"}
             })
        # ... (Edit/Delete buttons as before) ...
        footer_buttons.append({
            "type": "box", "layout":"horizontal", "spacing":"sm", "contents":[
                {"type": "button", "style": "secondary", "color": "#ffc107", "height": "sm", "flex": 1, "action": {"type": "message", "label": "✏️ 編輯", "text": f"#編輯幫助 T-{task.id}"}},
                {"type": "button", "style": "secondary", "color": "#dc3545", "height": "sm", "flex": 1, "action": {"type": "message", "label": "🗑️ 刪除", "text": f"#刪除 T-{task.id}"}}
            ]
        })
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=f"任務 T-{task.id} 詳情", contents=contents))
    except Exception as flex_err:
         logger.exception(f"創建或發送 Flex 詳情訊息失敗 T-{task.id}: {flex_err}")
         # Fallback text needs to be updated for new status display
         fallback_text = (
             f"🔍 任務詳情 T-{task_id_num} (Flex失敗) 🔍\n"
             f"類型: {owner_type_display}\n"
             f"負責人: {members_display}\n"
             f"內容: {task.content or '(無內容)'}\n"
             f"狀態: {status_display}\n"
             f"截止日期: {due_date_str}\n"
             f"建立時間: {created_at_str}\n"
             f"\n操作: #完成 T-{task.id} | #編輯幫助 T-{task.id} | #刪除 T-{task.id}"
         )
         line_bot_api.reply_message(reply_token, TextSendMessage(text=fallback_text))


def handle_draw_lots(reply_token: str, match: re.Match): # Unchanged
    # ...
    question = match.group(1)
    results = ["聖筊 👍 (同意)", "陰筊 👎 (不同意)", "笑筊 🤔 (重新問)"]
    result = random.choice(results)
    reply_text = f"❓ 問題: {question}\n✨ 結果: {result}"
    try:
        result_emoji = "👍" if "聖筊" in result else "👎" if "陰筊" in result else "🤔"
        result_color = "#28a745" if "聖筊" in result else "#dc3545" if "陰筊" in result else "#ffc107"
        contents = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "擲筊結果", "weight": "bold", "size": "lg"}]},
            "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"問題: {question}", "wrap": True, "weight": "bold", "size": "md", "margin":"md"}, {"type": "box", "layout": "vertical", "margin": "xl", "contents": [{"type": "text", "text": result, "size": "xxl", "align": "center", "color": result_color, "weight": "bold"}]}]},
            "footer": {"type": "box", "layout": "vertical", "spacing":"sm", "contents": [{"type": "button", "style": "primary", "color": result_color, "height": "sm", "action": {"type": "message", "label": f"再擲一次 {result_emoji}", "text": f"#擲筊 {question}"}}]}
        }
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=reply_text, contents=contents))
    except Exception as e:
        logger.exception(f"創建或發送擲筊 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))

def handle_random_pick(reply_token: str, match: re.Match): # Unchanged
    # ...
    options_text = match.group(1)
    options = [opt.strip() for opt in options_text.split() if opt.strip()]
    if not options:
        reply_text = "請提供至少一個抽籤選項！ (用空格分隔)"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text)); return
    chosen = random.choice(options)
    reply_text = f"從 [{', '.join(options)}] {len(options)} 個選項中抽出：\n🎉 {chosen} 🎉"
    try:
        contents = {
            "type": "bubble",
            "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "抽籤結果", "weight": "bold", "size": "lg"}]},
            "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"從 {len(options)} 個選項中抽出：", "size": "md", "color": "#555555", "wrap":True, "margin":"md"}, {"type": "box", "layout": "vertical", "margin": "xl", "contents": [{"type": "text", "text": chosen, "size": "xxl", "align": "center", "weight": "bold", "wrap": True, "color":"#2196F3"}]}]},
            "footer": {"type": "box", "layout": "vertical", "spacing":"sm", "contents": [{"type": "text", "text": f"選項: {', '.join(options)}", "size": "xs", "color": "#888888", "wrap": True, "margin":"md"}, {"type": "separator", "margin":"md"}, {"type": "button", "style": "primary", "color": "#2196F3", "height": "sm", "action": {"type": "message", "label": "再抽一次", "text": f"#抽籤 {options_text}"}}]}
        }
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=reply_text, contents=contents))
    except Exception as e:
        logger.exception(f"創建或發送抽籤 Flex 訊息失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))


def handle_batch_add_tasks(
    reply_token: str, match: re.Match, 
    adder_user_id: str, db: Session, 
    is_private_chat: bool, group_id_context: Optional[str]
):
    mention_block = match.group(1) 
    tasks_text = match.group(2).strip()
    task_lines = [line.strip() for line in tasks_text.split('\n') if line.strip()]

    if not task_lines:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="📝 請提供至少一行任務內容。")); return

    task_args_template = { "status": 'pending' }
    members_display_final = "我 (私人任務)"
    failed_member_names_creation: List[str] = []

    if is_private_chat:
        task_args_template["owner_user_id"] = adder_user_id
        if mention_block and mention_block.strip():
             line_bot_api.push_message(adder_user_id, TextSendMessage(text="提示：在私人聊天中批量新增任務時，@提及成員將被忽略。"))
    else: 
        if not group_id_context:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="批量新增失敗：缺少群組資訊。")); return

        member_names_to_assign = parse_mentioned_member_names(mention_block)
        if not member_names_to_assign:
            line_bot_api.reply_message(reply_token, TextSendMessage(text="批量新增失敗：請至少 @提及 一位成員。")); return

        members_to_assign_obj: List[Member] = []
        for name in member_names_to_assign: # ... (member creation logic as before) ...
            member = get_member_by_name_and_group(db, name=name, group_id=group_id_context)
            if not member:
                try: member = create_member(db, name=name, group_id=group_id_context); members_to_assign_obj.append(member)
                except Exception as create_err: logger.warning(f"批量新增建立成員 '{name}' 失敗: {create_err}"); failed_member_names_creation.append(name)
            else: members_to_assign_obj.append(member)

        if not members_to_assign_obj:
            error_msg = "批量新增失敗：無法找到或建立任何指定的成員。"
            if failed_member_names_creation: error_msg += f" (嘗試建立失敗: {', '.join(failed_member_names_creation)})"
            line_bot_api.reply_message(reply_token, TextSendMessage(text=error_msg)); db.rollback(); return

        task_args_template["group_id"] = group_id_context
        task_args_template["members"] = members_to_assign_obj
        members_display_final = ', '.join([f'@{m.name}' for m in members_to_assign_obj])

    tasks_to_create_params = []
    failed_lines_info = []

    for task_line in task_lines:
        current_task_params = task_args_template.copy()
        content = task_line # Priority tag removed
        due_date_str = None; due_date = None; error_msg = None

        date_match = re.search(r'(?:^|\s)(\d{4}/\d{1,2}/\d{1,2})$', content)
        if date_match:
            due_date_str = date_match.group(1)
            content = content[:date_match.start()].strip()
            due_date = parse_date(due_date_str)
            if due_date is None: error_msg = f"日期格式錯誤 ({due_date_str})"

        if not content: error_msg = "任務內容為空"
        current_task_params["content"] = content
        current_task_params["due_date"] = due_date

        if error_msg:
            failed_lines_info.append({'line': task_line, 'error': error_msg})
        else:
            tasks_to_create_params.append(current_task_params)

    final_summaries = []
    if tasks_to_create_params:
        try:
            for params in tasks_to_create_params:
                task_obj = create_task(db=db, **params)
                task_summary = f"T-{task_obj.id}: {params['content']}" # No priority display
                if params['due_date']:
                    task_summary += f" (截止: {params['due_date'].strftime('%Y/%m/%d')})"
                final_summaries.append(task_summary)
            logger.info(f"批量新增 {len(final_summaries)} 個任務成功 for {members_display_final}.")
        except Exception as e: # ... (error handling as before, simplified due to create_task handling its own commit/rollback)
            logger.exception(f"批量新增時發生錯誤: {e}")
            for params in tasks_to_create_params: 
                 failed_lines_info.append({'line': params['content'], 'error': f"資料庫儲存失敗 ({type(e).__name__})"})
            final_summaries = []

    success_count = len(final_summaries)
    failure_count = len(failed_lines_info)

    if success_count == 0 and failure_count == 0 :
        line_bot_api.reply_message(reply_token, TextSendMessage(text="未提供有效任務內容或所有行格式錯誤。")); return

    alt_text = f"批量新增結果：成功 {success_count}, 失敗 {failure_count} (為 {members_display_final})"
    try:
        bubble_contents = create_batch_add_result_bubble(members_display_final, final_summaries, failed_lines_info)
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=alt_text, contents=bubble_contents))
    except Exception as flex_err: # ... (fallback text generation as before, simplified due to no priority)
        logger.error(f"創建批量新增結果 Flex 失敗: {flex_err}")
        reply_text = f"批量新增任務結果 ({members_display_final})：\n✅ 成功: {success_count} | ❌ 失敗: {failure_count}\n"
        if final_summaries: reply_text += "\n-- 成功 --\n" + "\n".join(final_summaries[:10]) + ("\n..." if len(final_summaries) > 10 else "")
        if failed_lines_info: reply_text += "\n-- 失敗 --\n" + "\n".join([f"行: {f['line'][:30]}... 原因: {f['error']}" for f in failed_lines_info[:5]]) + ("\n..." if len(failed_lines_info) > 5 else "")
        if failed_member_names_creation: reply_text += f"\n⚠️ 無法建立成員: {', '.join(failed_member_names_creation)}"
        # Split long fallback text
        messages_to_send = []; current_message = ""
        for line_msg in reply_text.splitlines(keepends=True):
            if len(current_message) + len(line_msg) > 4900: messages_to_send.append(TextSendMessage(text=current_message.strip())); current_message = line_msg
            else: current_message += line_msg
        if current_message.strip(): messages_to_send.append(TextSendMessage(text=current_message.strip()))
        if messages_to_send: line_bot_api.reply_message(reply_token, messages=messages_to_send)

def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str: return None
    try:
        # Return as datetime object (time will be 00:00:00)
        # Timezone handling should be consistent; assume naive datetime, convert to UTC later if needed.
        return datetime.strptime(date_str, "%Y/%m/%d")
    except ValueError:
        return None

def send_help_message_v250(reply_token: str, is_private_chat: bool):
    private_specific = (
        "\n✨ 私人聊天指令 ✨\n"
        "`#新增 內容 [日期]`\n"
        "`#列表` - 顯示您的待辦任務\n"
        "`#紀錄` - 查看您已完成的任務紀錄\n"
        "`#批量新增` (換行輸入多任務)\n"
        "  `內容1 [日期]`\n"
        "  `內容2 [日期]`\n"
    )
    group_specific = (
        "\n✨ 群組聊天指令 ✨\n"
        "`#新增 @成員1 @成員2... 內容 [日期]`\n"
        "`#列表 [@成員]` - 顯示群組或指定成員待辦\n"
        "`#批量新增 @成員1 @成員2...` (換行輸入多任務)\n"
        "  `內容1 [日期]`\n"
        "  `內容2 [日期]`\n"
    )

    common_intro = "📋 待辦事項機器人指令 v2.5.0 📋\n"
    common_suffix = (
        "\n🔸 通用管理 (私人/群組) 🔸\n"
        "`#完成 T-ID` - 標記任務完成\n"
        "`#詳情 T-ID` - 查看任務詳細資訊\n"
        "`#修改 T-ID 新內容 [新截止日期]` (無法改負責人)\n"
        "`#刪除 T-ID`\n\n"
        "🕹️ 其他功能 🕹️\n"
        "`#擲筊 問題`\n"
        "`#抽籤 選項1 選項2 ...`\n\n"
        "❓ 獲取幫助 ❓\n"
        "`#幫助` (本訊息)\n"
        "`#幫助新增` (新增指令說明)\n"
        "`#編輯幫助 T-ID` (修改指令說明)"
    )

    help_text = common_intro + (private_specific if is_private_chat else group_specific) + common_suffix

    quick_reply_items = [QuickReplyButton(action=MessageAction(label="#新增", text="#新增 "))] # Add space for user to type
    quick_reply_items.append(QuickReplyButton(action=MessageAction(label="#列表", text="#列表")))
    if is_private_chat:
        quick_reply_items.append(QuickReplyButton(action=MessageAction(label="#紀錄", text="#紀錄")))

    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text,
            quick_reply=QuickReply(items=quick_reply_items)))
    except Exception as e:
        logger.warning(f"發送 QuickReply 幫助失敗: {e}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))


def send_add_help_message_v250(reply_token: str, is_private_chat: bool):
    private_text = (
        "📝 如何新增您的私人任務 📝\n\n"
        "1️⃣ 指令式新增:\n"
        "    `#新增 任務內容 [截止日期]`\n"
        "    * 日期: YYYY/MM/DD (可選)\n"
        "    * 範例: `#新增 完成報告 2025/12/31`\n\n"
        "2️⃣ 批量新增:\n"
        "    `#批量新增`\n"
        "    (換行輸入多個任務, 每行格式同上)\n"
        "    `任務內容1 [日期]`\n"
        "    `任務內容2`\n"
    )
    group_text = (
        "📝 如何新增群組任務 📝\n\n"
        "1️⃣ 指令式新增:\n"
        "    `#新增 @成員1 @成員2... 任務內容 [截止日期]`\n"
        "    * @成員: **必填**\n"
        "    * 日期: YYYY/MM/DD (可選)\n"
        "    * 範例: `#新增 @用戶A @用戶B 重要報告 2025/12/31`\n\n"
        "2️⃣ 批量新增:\n"
        "    `#批量新增 @成員1 @成員2...`\n"
        "    (換行輸入多個任務, 每行格式同指令式)\n"
        "    `任務內容1 [日期]`\n"
        "    `任務內容2`\n"
    )
    help_text = private_text if is_private_chat else group_text
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

def send_edit_help_message_v250(reply_token: str, task_id: str):
    help_text = (f"✏️ 如何編輯任務 T-{task_id} ✏️\n\n"
                 f"`#修改 T-{task_id} 新任務內容 [新截止日期]`\n\n"
                 "說明:\n"
                 " - `新任務內容`: **必填**。\n"
                 " - `[新截止日期]`: 可選填，格式為 YYYY/MM/DD。\n"
                 " - **注意:** 無法修改任務的負責人/歸屬。\n\n"
                 "*範例 (修改內容):*\n"
                 f"`#修改 T-{task_id} 更新後的報告內容`\n\n"
                 "*範例 (修改內容和日期):*\n"
                 f"`#修改 T-{task_id} 報告內容延期 2025/07/01`")
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

# --- Flex/Text Message Creation Helpers ---
def create_task_list_bubble(title: str, tasks: List[Task], is_private_chat: bool): # For PENDING tasks
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg"}]}, "body": {"type": "box", "layout": "vertical", "spacing":"lg", "contents": []}, "footer": {"type": "box", "layout": "horizontal", "spacing": "md", "contents": [{"type": "button", "style": "primary", "color": "#1E88E5", "height": "sm", "flex": 1, "action": {"type": "message", "label": "➕ 新增任務", "text": "#新增 "}}, {"type": "button", "style": "secondary", "color": "#6c757d", "height": "sm", "flex": 1, "action": {"type": "message", "label": "❓ 幫助", "text": "#幫助"}}]}}
    body_contents = contents["body"]["contents"]

    if not tasks:
        body_contents.append({"type": "text", "text": "目前沒有待辦任務。", "wrap": True, "color": "#555555", "size": "md"}); return contents

    now_utc = datetime.now(timezone.utc)
    for i, task in enumerate(tasks):
        members_display = "我" if task.user_id else (', '.join([f'@{m.name}' for m in task.members]) if task.members else "未指定")
        member_color = "#555555" if task.user_id else "#1DB446"

        task_item_elements = [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": f"T-{task.id}", "size": "sm", "color": "#888888", "flex": 1, "weight":"bold"},
                {"type": "text", "text": members_display, "size": "sm", "color": member_color, "align": "end", "flex": 3, "weight":"bold", "wrap": True }
            ]},
            {"type": "text", "text": task.content, "wrap": True, "weight": "regular", "margin": "md", "size":"md"}
        ]

        if task.due_date:
            due_date_obj = task.due_date.astimezone(timezone.utc) # Ensure timezone for comparison
            due_date_display_str = due_date_obj.strftime('%Y/%m/%d')
            date_info_text = f"截止: {due_date_display_str}"
            date_color = "#888888" # Default
            if due_date_obj < now_utc: # Overdue
                days_overdue = (now_utc.date() - due_date_obj.date()).days
                date_info_text += f" (🔴 已逾期 {days_overdue} 天)" if days_overdue > 0 else " (🔴 今天已逾期)"
                date_color = "#dc3545"
            elif due_date_obj.date() == now_utc.date(): # Due today
                date_info_text += " (🟡 今天截止)"
                date_color = "#ffc107"
            else: # Due in future
                days_left = (due_date_obj.date() - now_utc.date()).days
                if days_left == 1: date_info_text += " (明天截止)"
                elif days_left < 4: date_info_text += f" ({days_left} 天後截止)"
                date_color = "#ffc107" if days_left < 4 else "#888888"

            task_item_elements.append({"type": "text", "text": date_info_text, "size": "xs", "color": date_color, "margin": "sm"})
        else:
            task_item_elements.append({"type": "text", "text": "截止: 無", "size": "xs", "color": "#888888", "margin": "sm"})

        buttons_box = {"type": "box", "layout": "horizontal", "margin": "lg", "spacing":"sm", "contents": [{"type": "button", "style": "primary", "color": "#4CAF50", "height": "sm", "flex": 1, "action": {"type": "message", "label": "完成", "text": f"#完成 T-{task.id}"}}, {"type": "button", "style": "secondary", "color": "#2196F3", "height": "sm", "flex": 1, "action": {"type": "message", "label": "詳情", "text": f"#詳情 T-{task.id}"}}]}; task_item_elements.append(buttons_box)
        body_contents.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "md", "backgroundColor": "#FAFAFA", "cornerRadius": "md", "contents": task_item_elements})
        if i < len(tasks) - 1: body_contents.append({"type":"separator", "margin":"lg"})
    return contents

def create_task_list_text(title: str, tasks: List[Task], is_private_chat: bool): # For PENDING tasks
    result = f"📋 {title} 📋\n\n"
    now_utc_date = datetime.now(timezone.utc).date()
    for i, task in enumerate(tasks, 1):
        owner_info = "👤 您的任務" if task.user_id else (f"👥 負責人: {', '.join([f'@{m.name}' for m in task.members]) if task.members else '未指定'}")
        result += f"【任務 T-{task.id}】\n"
        if not is_private_chat or task.group_id: result += f"{owner_info}\n"
        result += f"📝 內容: {task.content}\n"

        if task.due_date:
            due_date_obj_date = task.due_date.astimezone(timezone.utc).date()
            due_date_str_display = due_date_obj_date.strftime('%Y/%m/%d')
            status = ""
            if due_date_obj_date < now_utc_date: status = f"(🔴 已逾期)"
            elif due_date_obj_date == now_utc_date: status = "(🟡 今天截止)"
            else:
                days_left = (due_date_obj_date - now_utc_date).days
                if days_left < 4 : status = f"(⚠️ {days_left}天後截止)"
            result += f"📅 截止: {due_date_str_display} {status}\n"
        else:
            result += f"📅 截止: 無\n"
        result += f"👉 操作: #完成 T-{task.id} | #詳情 T-{task.id}\n"
        if i < len(tasks): result += "\n" + ("-" * 20) + "\n\n"
    return result

def create_record_list_bubble(title: str, tasks: List[Task]): # For COMPLETED private tasks
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg", "color":"#1E88E5"}]}, "body": {"type": "box", "layout": "vertical", "spacing":"md", "contents": []}}
    body_contents = contents["body"]["contents"]

    if not tasks:
        body_contents.append({"type": "text", "text": "目前沒有已完成的任務紀錄。", "wrap": True, "color": "#555555", "size": "md"}); return contents

    on_time_count = sum(1 for t in tasks if t.completed_on_time is True and t.due_date is not None)
    late_count = sum(1 for t in tasks if t.completed_on_time is False)
    no_due_date_completed_count = sum(1 for t in tasks if t.due_date is None) # Assumes completed_on_time might be True or None

    summary_texts = []
    if on_time_count > 0: summary_texts.append({"type":"text", "text":f"✅ 如期完成: {on_time_count} 項", "size":"sm", "color":"#28a745"})
    if late_count > 0: summary_texts.append({"type":"text", "text":f"⚠️ 逾期完成: {late_count} 項", "size":"sm", "color":"#ffc107"})
    if no_due_date_completed_count > 0: summary_texts.append({"type":"text", "text":f"👍 無限期完成: {no_due_date_completed_count} 項", "size":"sm", "color":"#007bff"}) # Blue for no due date

    if summary_texts:
        body_contents.append({"type":"box", "layout":"vertical", "spacing":"xs", "contents": summary_texts, "margin":"md", "paddingBottom":"md"})
        body_contents.append({"type":"separator"})


    for i, task in enumerate(tasks):
        completion_status_text = "✅ 完成"
        completion_status_color = "#28a745" # Green
        if task.completed_on_time is True and task.due_date:
            completion_status_text = "👍 如期完成"
        elif task.completed_on_time is False:
            completion_status_text = "🟠 逾期完成"
            completion_status_color = "#ffc107" # Orange/Yellow

        completed_at_str = task.completed_at.astimezone(timezone.utc).strftime('%Y/%m/%d %H:%M') if task.completed_at else "N/A"
        due_date_str = f"(截止: {task.due_date.strftime('%Y/%m/%d')})" if task.due_date else "(無截止日)"

        task_item = {
            "type": "box", "layout": "vertical", "margin": "md", "spacing": "sm",
            "contents": [
                {"type": "box", "layout":"horizontal", "contents":[
                    {"type":"text", "text":f"T-{task.id}", "size":"sm", "color":"#888888", "flex":1},
                    {"type":"text", "text":completion_status_text, "size":"sm", "color":completion_status_color, "weight":"bold", "align":"end", "flex":2}
                ]},
                {"type": "text", "text": task.content, "wrap": True, "size":"md"},
                {"type": "text", "text": f"完成於: {completed_at_str} {due_date_str}", "size": "xs", "color": "#888888"},
            ]
        }
        body_contents.append(task_item)
        if i < len(tasks) - 1: body_contents.append({"type":"separator", "margin":"md"})
    return contents

def create_record_list_text(title: str, tasks: List[Task]): # For COMPLETED private tasks
    result = f"📊 {title} 📊\n\n"
    on_time_count = sum(1 for t in tasks if t.completed_on_time is True and t.due_date is not None)
    late_count = sum(1 for t in tasks if t.completed_on_time is False)
    no_due_date_completed_count = sum(1 for t in tasks if t.due_date is None)

    if on_time_count > 0: result += f"✅ 如期完成: {on_time_count} 項\n"
    if late_count > 0: result += f"⚠️ 逾期完成: {late_count} 項\n"
    if no_due_date_completed_count > 0: result += f"👍 無限期完成: {no_due_date_completed_count} 項\n"
    if on_time_count or late_count or no_due_date_completed_count: result += "--------------------\n\n"

    for i, task in enumerate(tasks, 1):
        completion_status_text = "✅ 完成"
        if task.completed_on_time is True and task.due_date: completion_status_text = "👍 如期完成"
        elif task.completed_on_time is False: completion_status_text = "🟠 逾期完成"

        completed_at_str = task.completed_at.astimezone(timezone.utc).strftime('%Y/%m/%d %H:%M') if task.completed_at else "N/A"
        due_date_str = f"(截止: {task.due_date.strftime('%Y/%m/%d')})" if task.due_date else "(無截止日)"

        result += f"【任務 T-{task.id}】 {completion_status_text}\n"
        result += f"📝 內容: {task.content}\n"
        result += f"⏱️ 完成於: {completed_at_str} {due_date_str}\n"
        if i < len(tasks): result += "\n"
    return result

def create_batch_add_result_bubble(members_display: str, success_summaries: List[str], failed_lines_info: List[Dict[str, str]]):
    # ... (This function's display remains the same, as priority was already removed from summaries)
    success_count = len(success_summaries)
    failure_count = len(failed_lines_info)
    header_text = f"批量新增結果 ({members_display})"
    header_color = "#1DB446" if success_count > 0 and failure_count == 0 else "#ffc107" if success_count > 0 and failure_count > 0 else "#dc3545"
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": header_text, "weight": "bold", "size": "lg", "color": header_color, "wrap":True}]}, "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [{"type": "text", "text": f"✅ 成功: {success_count}  |  ❌ 失敗: {failure_count}", "weight": "bold", "size": "md", "wrap": True}]}, "footer": {"type": "box", "layout": "vertical", "contents": [{"type": "button", "action": {"type": "message", "label": "查看任務列表", "text": f"#列表"}, "style": "primary", "color":"#1DB446", "height":"sm"}]}}
    body_contents = contents["body"]["contents"]

    if success_summaries:
        body_contents.append({"type": "separator", "margin": "lg"})
        body_contents.append({"type": "text", "text": "成功新增列表:", "weight": "bold", "size": "sm", "color": "#1DB446", "margin": "md"})
        success_box = {"type": "box", "layout": "vertical", "margin": "sm", "spacing":"xs", "contents": []}
        for summary in success_summaries[:8]: success_box["contents"].append({"type": "text", "text": f"• {summary}", "size": "sm", "wrap": True})
        if len(success_summaries) > 8: success_box["contents"].append({"type": "text", "text": f"... (共 {success_count} 個)", "size": "xs", "color": "#555555", "margin": "sm"})
        body_contents.append(success_box)

    if failed_lines_info:
        body_contents.append({"type": "separator", "margin": "lg"})
        body_contents.append({"type": "text", "text": "失敗行與原因:", "weight": "bold", "size": "sm", "color": "#dc3545", "margin": "md"})
        failed_box = {"type": "box", "layout": "vertical", "margin": "sm", "spacing":"xs", "contents": []}
        for failed in failed_lines_info[:5]:
             line_preview = failed['line'][:60] + ('...' if len(failed['line']) > 60 else '')
             failed_box["contents"].append({"type": "box", "layout":"vertical", "margin":"xxs", "contents":[ {"type": "text", "text": f"行: \"{line_preview}\"", "size": "xs", "wrap": True, "color": "#555555"}, {"type": "text", "text": f"原因: {failed['error']}", "size": "xs", "wrap": True, "color": "#dc3545", "weight":"bold"}]})
        if len(failed_lines_info) > 5: failed_box["contents"].append({"type": "text", "text": f"... (共 {failure_count} 行失敗)", "size": "xs", "color": "#dc3545", "margin": "sm"})
        body_contents.append(failed_box)
    return contents

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"讀取到的端口配置為: {port}")
    host = '0.0.0.0'
    app.run(host=host, port=port, debug=False)