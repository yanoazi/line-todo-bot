# app.py (SQLAlchemy + Refactored Features + Recurring Guide + List Recurring + Enhanced Notification + Error Fixes - v2.2.1)
from flask import Flask, request, abort, jsonify
import os
import json
import random
import re
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone, date, timedelta
import logging
from dotenv import load_dotenv
import inspect

# --- Database Imports (SQLAlchemy) ---
from models import (
    init_db, get_db, Member, Task,
    get_member_by_name_and_group, get_member_by_id, get_task_by_id,
    get_pending_tasks_by_member_id, get_pending_tasks_by_group_id,
    create_member, create_task
)
from sqlalchemy import text, or_,orm
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import SQLAlchemyError

# --- LINE SDK Imports (v2) ---
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FlexSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)

# --- Standard Python Imports ---
# (No changes needed here)

# --- Application Initialization ---
app = Flask(__name__)
load_dotenv()

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Configuration Loading ---
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
TARGET_GROUP_ID = os.environ.get('LINE_GROUP_ID')
N8N_API_KEY = os.environ.get('API_KEY', 'default_key')
DATABASE_URL = os.environ.get('DATABASE_URL')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

# --- Configuration Checks ---
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    logger.error("環境變數 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET 未設定")
    exit(1)
if not TARGET_GROUP_ID:
    logger.warning("環境變數 LINE_GROUP_ID 未設定。n8n 推播等功能可能無法指定預設群組。")
if not DATABASE_URL:
    logger.error("環境變數 DATABASE_URL 未設定！應用程式無法連接資料庫。")
    # exit(1) # Consider uncommenting if DB is absolutely required at startup
if not OPENAI_API_KEY:
    logger.warning("環境變數 OPENAI_API_KEY 未設定。未來 OpenAI 功能將無法使用。")


# --- Replit Specific Configuration ---
IN_REPLIT = os.environ.get('REPL_ID') is not None
REPLIT_DB_URL = os.environ.get('REPLIT_DB_URL')
if IN_REPLIT:
    logger.info("在 Replit 環境中運行，資料庫配置將在 models.py 中處理。")

# --- LINE API Initialization (v2) ---
try:
    line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(CHANNEL_SECRET)
except Exception as e:
    logger.exception(f"初始化 LINE SDK 失敗: {e}")
    exit(1)

# --- Database Initialization ---
try:
    init_db()
    logger.info("資料庫初始化檢查完成。")
except Exception as e:
    logger.exception(f"資料庫初始化失敗: {e}")
    # Depending on severity, you might want to exit(1) here

# --- Regex Patterns ---
ADD_TASK_PATTERN = r'#新增\s+@(\S+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)(?:\s+(\d{4}/\d{1,2}/\d{1,2}))?$'
COMPLETE_TASK_PATTERN = r'#完成\s+T-(\d+)$'
LIST_TASK_PATTERN = r'#列表\s*(?:@(\S+))?$'
DELETE_TASK_PATTERN = r'#刪除\s+T-(\d+)$'
EDIT_TASK_PATTERN = r'#修改\s+T-(\d+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)(?:\s*(\d{4}/\d{1,2}/\d{1,2}))?$'
DETAIL_TASK_PATTERN = r'#詳情\s+T-(\d+)$'
DRAW_LOTS_PATTERN = r'#擲筊\s+(.+)$'
RANDOM_PICK_PATTERN = r'#抽籤\s+(.+)$'
BATCH_ADD_TASK_PATTERN = r'#批量新增\s+@(\S+)\s*\n(.+)$'
RECURRING_TASK_PATTERN = r'#定期\s+@(\S+)\s+(?:(!(?:低|普通|高))\s+)?(.+?)\s+每(週[一二三四五六日]|月\d{1,2}日|年\d{1,2}月\d{1,2}日|天)$'
CANCEL_RECURRING_PATTERN = r'#取消定期\s+T-(\d+)$'
NEW_TASK_GUIDE_PATTERN = r'^#新任務$'
NEW_RECURRING_TASK_GUIDE_PATTERN = r'^#定期$'
RECURRING_LIST_PATTERN = r'^#定期列表$'

# --- User Session Management (In-Memory) ---
# WARNING: This state is lost on application restart. Consider persistent storage (Redis, DB) for production.
class UserSessions:
    _sessions: Dict[str, Dict[str, Any]] = {}
    @classmethod
    def get_session(cls, key: str) -> Optional[Dict[str, Any]]: return cls._sessions.get(key)
    @classmethod
    def set_session(cls, key: str, data: Dict[str, Any]): cls._sessions[key] = data; logger.debug(f"Session set for {key}: {data}")
    @classmethod
    def clear_session(cls, key: str):
        if key in cls._sessions: del cls._sessions[key]; logger.debug(f"Session cleared for {key}")
    @classmethod
    def update_session(cls, key: str, update_data: Dict[str, Any]):
        if key in cls._sessions: cls._sessions[key].update(update_data); logger.debug(f"Session updated for {key}: {cls._sessions[key]}")
        else: logger.warning(f"Attempted to update non-existent session: {key}")

# --- Flask Routes ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError: logger.error("Invalid signature"); abort(400)
    except Exception as e: logger.exception(f"處理回調時發生未預期錯誤: {e}"); abort(500)
    return 'OK'

@app.route("/ping", methods=['GET'])
def ping():
    db_ok = False; db_error = None
    try:
        with get_db() as db: db.execute(text("SELECT 1")); db_ok = True
    except Exception as e: logger.error(f"Ping DB check failed: {e}"); db_error = str(e)
    return jsonify({"status": "ok", "message": "LINE Bot running (v2.2.1)", "timestamp": datetime.now(timezone.utc).isoformat(), "db_connection": "ok" if db_ok else "error", "db_error": db_error})

# --- LINE Event Handlers ---
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
    text = event.message.text.strip(); reply_token = event.reply_token
    user_id = event.source.user_id; group_id = None
    if event.source.type == 'group': group_id = event.source.group_id
    elif event.source.type == 'room': group_id = event.source.room_id
    if not group_id: logger.info(f"Ignoring non-group/room message from {user_id}"); return

    logger.info(f"Received from {group_id} by {user_id}: {text}")
    session_key = f"{user_id}_{group_id}"; user_session = UserSessions.get_session(session_key)

    try:
        with get_db() as db:
            if user_session and user_session.get('state'):
                logger.debug(f"Handling conversation: {user_session}")
                if handle_conversation_state(text, user_session, group_id, user_id, db, reply_token): return

            # Match commands (order can matter)
            new_task_guide_match = re.match(NEW_TASK_GUIDE_PATTERN, text)
            new_recurring_task_guide_match = re.match(NEW_RECURRING_TASK_GUIDE_PATTERN, text)
            recurring_list_match = re.match(RECURRING_LIST_PATTERN, text)
            add_match = re.match(ADD_TASK_PATTERN, text)
            recurring_match = re.match(RECURRING_TASK_PATTERN, text) # Matches #定期 with args
            complete_match = re.match(COMPLETE_TASK_PATTERN, text)
            list_match = re.match(LIST_TASK_PATTERN, text)
            delete_match = re.match(DELETE_TASK_PATTERN, text)
            edit_match = re.match(EDIT_TASK_PATTERN, text)
            detail_match = re.match(DETAIL_TASK_PATTERN, text)
            draw_match = re.match(DRAW_LOTS_PATTERN, text)
            pick_match = re.match(RANDOM_PICK_PATTERN, text)
            batch_add_match = re.match(BATCH_ADD_TASK_PATTERN, text, re.DOTALL)
            cancel_recurring_match = re.match(CANCEL_RECURRING_PATTERN, text)

            # Route to handlers
            if new_task_guide_match: UserSessions.set_session(session_key, {'state': 'creating_task', 'step': 'get_content'}); line_bot_api.reply_message(reply_token, TextSendMessage(text="好的，請輸入要新增的任務內容：")); return
            elif new_recurring_task_guide_match: UserSessions.set_session(session_key, {'state': 'creating_recurring_task', 'step': 'get_content'}); line_bot_api.reply_message(reply_token, TextSendMessage(text="好的，請輸入要新增的「定期」任務內容：")); return
            elif recurring_list_match: handle_recurring_list(reply_token, group_id, db); return
            elif add_match: handle_add_task(reply_token, add_match, group_id, user_id, db)
            elif recurring_match: handle_recurring_task(reply_token, recurring_match, group_id, user_id, db)
            elif complete_match: handle_complete_task(reply_token, complete_match, user_id, db)
            elif list_match: handle_list_tasks(reply_token, list_match, group_id, db)
            elif delete_match: handle_delete_task(reply_token, delete_match, group_id, user_id, db)
            elif edit_match: handle_edit_task(reply_token, edit_match, group_id, user_id, db)
            elif detail_match: handle_task_details(reply_token, detail_match, db)
            elif draw_match: handle_draw_lots(reply_token, draw_match)
            elif pick_match: handle_random_pick(reply_token, pick_match)
            elif batch_add_match: handle_batch_add_tasks(reply_token, batch_add_match, group_id, user_id, db)
            elif cancel_recurring_match: handle_cancel_recurring_task(reply_token, cancel_recurring_match, group_id, user_id, db)
            elif text == "#幫助": send_help_message(reply_token)
            elif text == "#幫助新增": send_add_help_message(reply_token)
            elif text.startswith("#編輯幫助 T-"):
                task_id_match = re.match(r'#編輯幫助 T-(\d+)', text)
                if task_id_match: send_edit_help_message(reply_token, task_id_match.group(1))
                else: line_bot_api.reply_message(reply_token, TextSendMessage(text="指令格式錯誤..."))
            elif text == "#新增表單": send_add_task_form(reply_token, db, group_id)
            elif text == "#定期表單": send_recurring_task_form(reply_token, db, group_id)
            else: logger.info(f"Unmatched command/text.") # No reply for unmatched

    except SQLAlchemyError as db_err: logger.exception(f"DB錯誤: {db_err}"); # Reply handled below
    except Exception as e: logger.exception(f"未預期錯誤: {e}"); # Reply handled below

    # Centralized error reply (only if an exception occurred above)
    if 'db_err' in locals() or 'e' in locals():
        error_type = "資料庫" if 'db_err' in locals() else "內部"
        try: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"處理您的請求時發生{error_type}錯誤，請稍後再試。"))
        except Exception as reply_err: logger.error(f"回覆{error_type}錯誤訊息失敗: {reply_err}")


# --- Conversation Handling Logic ---
def handle_conversation_state(text: str, user_session: Dict[str, Any], group_id: str, user_id: str, db: Session, reply_token: str) -> bool:
    state = user_session.get('state'); step = user_session.get('step')
    session_key = f"{user_id}_{group_id}"; logger.debug(f"Handling conversation: state={state}, step={step}, input='{text}'")

    if state == 'creating_task': # Guided Flow for Regular Tasks
        if step == 'get_content':
            user_session['content'] = text; user_session['step'] = 'get_member'; UserSessions.set_session(session_key, user_session)
            line_bot_api.reply_message(reply_token, TextSendMessage(text="收到內容！請 @提及 負責人 或直接輸入成員名稱："))
            return True
        elif step == 'get_member':
            member_name = text.lstrip('@').strip()
            if not member_name: line_bot_api.reply_message(reply_token, TextSendMessage(text="成員名稱不可為空...")); return True
            user_session['member_name'] = member_name; user_session['step'] = 'get_priority'; UserSessions.set_session(session_key, user_session)
            send_priority_selection(reply_token, member_name, user_session['content']); return True
        elif step == 'get_priority':
            priority_map = {"低": "low", "普通": "normal", "高": "high"}; selected_priority = priority_map.get(text)
            if selected_priority:
                user_session['priority'] = selected_priority; user_session['step'] = 'get_due_date'; UserSessions.set_session(session_key, user_session)
                send_due_date_inquiry(reply_token, user_session['member_name'], user_session['content'], selected_priority)
            else: line_bot_api.reply_message(reply_token, TextSendMessage(text="請點擊按鈕或輸入有效優先級..."))
            return True
        elif step == 'get_due_date':
            due_date = None
            if text.lower() not in ["無", "沒有", "skip", "跳過", "no", "-"]:
                try: due_date = datetime.strptime(text, "%Y/%m/%d")
                except ValueError: line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確...")); return True
            create_conversation_task(reply_token, user_session, group_id, db, due_date); UserSessions.clear_session(session_key); return True

    elif state == 'creating_recurring_task': # Guided Flow for Recurring Tasks
        if step == 'get_content':
            user_session['content'] = text; user_session['step'] = 'get_member'; UserSessions.set_session(session_key, user_session)
            line_bot_api.reply_message(reply_token, TextSendMessage(text="收到定期任務內容！請 @提及 負責人..."))
            return True
        elif step == 'get_member':
            member_name = text.lstrip('@').strip()
            if not member_name: line_bot_api.reply_message(reply_token, TextSendMessage(text="成員名稱不可為空...")); return True
            user_session['member_name'] = member_name; user_session['step'] = 'get_priority'; UserSessions.set_session(session_key, user_session)
            send_priority_selection(reply_token, member_name, user_session['content']); return True
        elif step == 'get_priority':
            priority_map = {"低": "low", "普通": "normal", "高": "high"}; selected_priority = priority_map.get(text)
            if selected_priority:
                user_session['priority'] = selected_priority; user_session['step'] = 'get_recurrence_pattern'; UserSessions.set_session(session_key, user_session)
                send_recurrence_pattern_selection(reply_token)
            else: line_bot_api.reply_message(reply_token, TextSendMessage(text="請點擊按鈕或輸入有效優先級..."))
            return True
        elif step == 'get_recurrence_pattern':
            system_pattern, user_friendly_pattern = parse_recurrence_input(text)
            if system_pattern:
                user_session['system_pattern'] = system_pattern; user_session['user_friendly_pattern'] = user_friendly_pattern
                create_conversation_recurring_task(reply_token, user_session, group_id, db); UserSessions.clear_session(session_key)
            else: line_bot_api.reply_message(reply_token, TextSendMessage(text="無法識別的重複模式..."))
            return True

    logger.debug(f"Input '{text}' did not match active conversation state/step for {session_key}")
    return False

# --- Helper Functions for Conversation Flow ---
def send_priority_selection(reply_token: str, member_name: str, task_content: str):
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"好的，任務內容：\n「{task_content}」\n負責人：@{member_name}\n\n請選擇任務優先級：", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="🟢 低", text="低")), QuickReplyButton(action=MessageAction(label="🟡 普通", text="普通")), QuickReplyButton(action=MessageAction(label="🔴 高", text="高")),])))
    except Exception as e: logger.exception(f"發送優先級選擇失敗: {e}")
def send_due_date_inquiry(reply_token: str, member_name: str, task_content: str, priority: str):
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; priority_display = priority_map_display.get(priority, priority); today_str = date.today().strftime('%Y/%m/%d'); tomorrow_str = (date.today() + timedelta(days=1)).strftime('%Y/%m/%d')
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=f"任務內容...\n負責人：@{member_name}\n優先級：{priority_display}\n\n請輸入截止日期 (格式：YYYY/MM/DD)...", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="無截止日期", text="無")), QuickReplyButton(action=MessageAction(label=f"今天 ({today_str})", text=today_str)), QuickReplyButton(action=MessageAction(label=f"明天 ({tomorrow_str})", text=tomorrow_str)),])))
    except Exception as e: logger.exception(f"發送截止日期詢問失敗: {e}")
def send_recurrence_pattern_selection(reply_token: str):
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text="請選擇重複週期...", quick_reply=QuickReply(items=[QuickReplyButton(action=MessageAction(label="每天", text="每天")), QuickReplyButton(action=MessageAction(label="每週一", text="每週一")), QuickReplyButton(action=MessageAction(label="每週五", text="每週五")), QuickReplyButton(action=MessageAction(label="每月1日", text="每月1日")),])))
    except Exception as e: logger.exception(f"發送重複週期選擇失敗: {e}")

# --- CORRECTED parse_recurrence_input ---
def parse_recurrence_input(text: str) -> (Optional[str], Optional[str]):
    text = text.strip(); system_pattern = None; user_friendly_pattern = None
    pattern_map_week = { "週一": "weekly_monday", "週二": "weekly_tuesday", "週三": "weekly_wednesday", "週四": "weekly_thursday", "週五": "weekly_friday", "週六": "weekly_saturday", "週日": "weekly_sunday" }
    if text == "每天": system_pattern = "daily"; user_friendly_pattern = "每天"
    elif text.startswith("每週") and text[2:] in pattern_map_week: day_zh = text[2:]; system_pattern = pattern_map_week[day_zh]; user_friendly_pattern = f"每週{day_zh}"
    elif text.startswith("每月") and text.endswith("日"): day_str = text[2:-1];
    if day_str.isdigit() and 1 <= int(day_str) <= 31: day_num = int(day_str); system_pattern = f"monthly_{day_num}"; user_friendly_pattern = f"每月{day_num}日"
    elif text.startswith("每年") and "月" in text and text.endswith("日"):
        try:
            match = re.match(r"每年(\d{1,2})月(\d{1,2})日", text)
            if match: month, day = int(match.group(1)), int(match.group(2));
            if 1 <= month <= 12 and 1 <= day <= 31: system_pattern = f"yearly_{month}_{day}"; user_friendly_pattern = f"每年{month}月{day}日"
        except (ValueError, IndexError):
             pass # Correctly indented pass for the except block
    # Correctly indented logger and return
    logger.debug(f"Parsed recurrence input '{text}' to system='{system_pattern}', user='{user_friendly_pattern}'")
    return system_pattern, user_friendly_pattern

# --- CORRECTED create_conversation_task ---
def create_conversation_task(reply_token: str, user_session: Dict[str, Any], group_id: str, db: Session, due_date: Optional[datetime]):
    member_name = user_session.get('member_name'); task_content = user_session.get('content'); priority = user_session.get('priority', 'normal')
    if not member_name or not task_content: logger.error(f"會話狀態不完整..."); line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，任務資訊不完整...")); return
    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        logger.info(f"成員 '{member_name}' 不存在...自動建立。")
        try: member = create_member(db, name=member_name, group_id=group_id); logger.info(f"自動建立成員成功...")
        except Exception as create_err: logger.exception(f"...建立成員失敗: {create_err}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text=f"建立成員 '{member_name}' 失敗...")); return
    try:
        task = create_task(db, member_id=member.id, content=task_content, due_date=due_date, priority=priority); task_id_str = f"T-{task.id}"
        priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; priority_display = priority_map_display.get(priority, priority)
        reply_text = f"✅ 已為 @{member.name} 新增任務！\n內容：{task.content}\n任務ID：{task_id_str}\n優先級：{priority_display}\n截止：{due_date.strftime('%Y/%m/%d') if due_date else '無'}"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except SQLAlchemyError as db_err:
        logger.exception(f"從會話新增任務DB失敗: {db_err}")
        db.rollback()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗 (DB)..."))
    except Exception as e:
        logger.exception(f"從會話創建任務未知錯誤: {e}")
        # Corrected rollback handling
        try:
            db.rollback()
            logger.info("因創建任務時發生未知錯誤，已成功回滾。")
        except Exception as rollback_err:
            logger.error(f"嘗試回滾資料庫變更時也發生錯誤: {rollback_err}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗 (Internal)..."))

# --- CORRECTED create_conversation_recurring_task ---
def create_conversation_recurring_task(reply_token: str, user_session: Dict[str, Any], group_id: str, db: Session):
    member_name = user_session.get('member_name'); task_content = user_session.get('content'); priority = user_session.get('priority', 'normal'); system_pattern = user_session.get('system_pattern'); user_friendly_pattern = user_session.get('user_friendly_pattern', '未知週期')
    if not member_name or not task_content or not system_pattern: logger.error(f"定期任務會話狀態不完整..."); line_bot_api.reply_message(reply_token, TextSendMessage(text="抱歉，定期任務資訊不完整...")); return
    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        logger.info(f"成員 '{member_name}' 不存在...自動建立。")
        try: member = create_member(db, name=member_name, group_id=group_id); logger.info(f"自動建立成員成功...")
        except Exception as create_err: logger.exception(f"...建立成員失敗: {create_err}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text=f"建立成員 '{member_name}' 失敗...")); return
    try:
        task = Task(member_id=member.id, content=task_content, status='recurring_master', priority=priority, is_recurring=True, recurrence_pattern=system_pattern, recurrence_count=0)
        db.add(task); db.commit()
        priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; priority_display = priority_map_display.get(priority, priority)
        reply_text = f"✅ 已為 @{member.name} 新增「定期」任務模板！\n內容：{task.content}\n任務ID：T-{task.id}\n優先級：{priority_display}\n重複：{user_friendly_pattern}\n👉 系統將定時自動生成此任務的待辦項。"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except SQLAlchemyError as db_err:
        logger.exception(f"從會話新增定期任務DB失敗: {db_err}")
        db.rollback()
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增定期任務失敗 (DB)..."))
    except Exception as e:
        logger.exception(f"從會話創建定期任務未知錯誤: {e}")
        # Corrected rollback handling
        try:
            db.rollback()
            logger.info("因創建定期任務時發生未知錯誤，已成功回滾。")
        except Exception as rollback_err:
            logger.error(f"嘗試回滾資料庫變更時也發生錯誤: {rollback_err}")
        line_bot_api.reply_message(reply_token, TextSendMessage(text="新增定期任務失敗 (Internal)..."))


# --- Command Handling Functions ---
# (Same as previous version unless specific bugs were in them)
# handle_add_task, handle_complete_task, handle_list_tasks, handle_delete_task,
# handle_edit_task, handle_task_details, handle_draw_lots, handle_random_pick,
# handle_batch_add_tasks, handle_recurring_task, handle_cancel_recurring_task,
# handle_recurring_list
def handle_add_task(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session):
    member_name = match.group(1); priority_tag = match.group(2); task_content = match.group(3).strip(); due_date_str = match.group(4)
    priority = "normal"; priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
    if priority_tag:
        if "低" in priority_tag: priority = "low"
        elif "高" in priority_tag: priority = "high"
    due_date = parse_date(due_date_str)
    if due_date_str and due_date is None: line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確...")); return
    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        logger.info(f"成員 '{member_name}' 不存在...自動建立。")
        try: member = create_member(db, name=member_name, group_id=group_id)
        except Exception as create_err: logger.exception(f"...建立成員失敗: {create_err}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text=f"建立成員 '{member_name}' 失敗...")); return
    try:
        task = create_task(db, member_id=member.id, content=task_content, due_date=due_date, priority=priority); task_id_str = f"T-{task.id}"
        priority_display = priority_map_display.get(priority, priority)
        reply_text = f"✅ 已為 @{member.name} 新增任務...\n內容：{task.content}\n任務ID：{task_id_str}\n優先級：{priority_display}\n截止：{due_date.strftime('%Y/%m/%d') if due_date else '無'}"
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except SQLAlchemyError as db_err: logger.exception(f"新增任務DB失敗: {db_err}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗 (DB)..."))
    except Exception as e: logger.exception(f"新增任務未知錯誤: {e}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text="新增任務失敗 (Internal)..."))
def handle_complete_task(reply_token: str, match: re.Match, completer_user_id: str, db: Session):
    task_id_num = int(match.group(1)); task = get_task_by_id(db, task_id=task_id_num)
    if not task: reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif task.status == 'completed': reply_text = f"ℹ️ 任務 T-{task_id_num} ({task.content[:15]}...) 已經是完成狀態。"
    else:
        try:
            task.status = 'completed'; task.completed_at = datetime.now(timezone.utc); db.commit()
            reply_text = f"🎉 已將 {task.member.name} 的任務 T-{task_id_num} 標記為完成！\n內容：{task.content}"
        except SQLAlchemyError as e: logger.exception(f"...更新任務狀態失敗 (DB): {e}"); db.rollback(); reply_text = f"❌ 更新任務 T-{task_id_num} 狀態失敗 (DB)。"
        except Exception as e: logger.exception(f"...更新任務狀態失敗: {e}"); db.rollback(); reply_text = f"❌ 更新任務 T-{task_id_num} 狀態失敗 (Internal)。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
def handle_list_tasks(reply_token: str, match: re.Match, group_id: str, db: Session):
    member_name = match.group(1); tasks: List[Task] = []; title = ""
    try:
        if member_name:
            member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
            if not member: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"找不到成員：{member_name}")); return
            tasks = get_pending_tasks_by_member_id(db, member_id=member.id); title = f"{member_name} 的待辦事項"
        else:
            tasks = db.query(Task).options(joinedload(Task.member)).filter(Task.member.has(group_id=group_id), Task.status == 'pending').order_by(Task.due_date.asc().nulls_last(), Task.priority.desc(), Task.created_at.asc()).all()
            title = "本群組待辦事項"
        if not tasks: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"✅ {title}：目前沒有待辦任務！")); return
        try: bubble_json = create_task_list_bubble(title, tasks, db); line_bot_api.reply_message(reply_token, messages=[FlexSendMessage(alt_text=title, contents=bubble_json)])
        except Exception as e: logger.exception(f"創建/發送 Flex 列表失敗: {e}"); task_list_text = create_task_list_text(title, tasks, db)
            # Split long messages...
        max_len = 4900; messages_to_send = [];
        while len(task_list_text) > max_len: split_pos = task_list_text.rfind('\n\n', 0, max_len); if split_pos == -1: split_pos = max_len; messages_to_send.append(TextSendMessage(text=task_list_text[:split_pos])); task_list_text = task_list_text[split_pos:].lstrip()
        messages_to_send.append(TextSendMessage(text=task_list_text)); line_bot_api.reply_message(reply_token, messages=messages_to_send)
    except SQLAlchemyError as e: logger.exception(f"列出任務DB失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text="查詢任務列表DB錯誤。"))
    except Exception as e: logger.exception(f"列出任務未知錯誤: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text="處理列表請求內部錯誤。"))
def handle_delete_task(reply_token: str, match: re.Match, group_id: str, deleter_user_id: str, db: Session):
    task_id_num = int(match.group(1)); task = get_task_by_id(db, task_id=task_id_num)
    if not task: reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif task.member.group_id != group_id: reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間。"
    else:
        try:
            task_content_preview = task.content[:20]; member_name = task.member.name
            db.delete(task); db.commit()
            reply_text = f"🗑️ 已成功刪除 @{member_name} 的任務 T-{task_id_num} ({task_content_preview}...)。"
        except SQLAlchemyError as e: logger.exception(f"...刪除任務失敗 (DB): {e}"); db.rollback(); reply_text = f"❌ 刪除任務 T-{task_id_num} 失敗 (DB)。"
        except Exception as e: logger.exception(f"...刪除任務失敗: {e}"); db.rollback(); reply_text = f"❌ 刪除任務 T-{task_id_num} 失敗 (Internal)。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
def handle_edit_task(reply_token: str, match: re.Match, group_id: str, editor_user_id: str, db: Session):
    task_id_num = int(match.group(1)); priority_tag = match.group(2); new_content = match.group(3).strip(); new_due_date_str = match.group(4)
    task = get_task_by_id(db, task_id=task_id_num); priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}
    if not task: reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif task.member.group_id != group_id: reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間。"
    else:
        updates = {};
        if not new_content: line_bot_api.reply_message(reply_token, TextSendMessage(text="❌ 修改任務時，任務內容不能為空。")); return
        updates['content'] = new_content
        new_due_date = None
        if new_due_date_str:
             new_due_date = parse_date(new_due_date_str)
             if new_due_date is None: line_bot_api.reply_message(reply_token, TextSendMessage(text="日期格式不正確...")); return
             updates['due_date'] = new_due_date
        if priority_tag:
            if "低" in priority_tag: updates['priority'] = "low"
            elif "高" in priority_tag: updates['priority'] = "high"
            else: updates['priority'] = "normal"
        if not updates: line_bot_api.reply_message(reply_token, TextSendMessage(text="ℹ️ 沒有提供任何有效的修改內容。")); return
        try:
            if 'content' in updates: task.content = updates['content']
            if 'priority' in updates: task.priority = updates['priority']
            if 'due_date' in updates: task.due_date = updates['due_date']
            db.commit()
            priority_display = priority_map_display.get(task.priority, task.priority)
            due_date_text = f"截止：{task.due_date.strftime('%Y/%m/%d')}" if task.due_date else "截止：無"
            reply_text = f"✏️ 已更新任務 T-{task_id_num} (@{task.member.name})：\n內容：{task.content}\n優先級：{priority_display}\n{due_date_text}"
        except SQLAlchemyError as e: logger.exception(f"...修改任務失敗 (DB): {e}"); db.rollback(); reply_text = f"❌ 修改任務 T-{task_id_num} 失敗 (DB)。"
        except Exception as e: logger.exception(f"...修改任務失敗: {e}"); db.rollback(); reply_text = f"❌ 修改任務 T-{task_id_num} 失敗 (Internal)。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
def handle_task_details(reply_token: str, match: re.Match, db: Session):
    task_id_num = int(match.group(1))
    try:
        task = db.query(Task).options(orm.joinedload(Task.member)).filter(Task.id == task_id_num).first()
        if not task: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"❌ 找不到ID為 T-{task_id_num} 的任務。")); return
        local_tz = timezone.utc; created_at_str = task.created_at.astimezone(local_tz).strftime('%Y/%m/%d %H:%M') if task.created_at else "未知"
        due_date_str = task.due_date.strftime('%Y/%m/%d') if task.due_date else "無"; status_str = "✅ 已完成" if task.status == 'completed' else "⏳ 待辦中"
        completed_at_str = task.completed_at.astimezone(local_tz).strftime('%Y/%m/%d %H:%M') if task.completed_at else ""
        priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; priority_display = priority_map_display.get(task.priority, task.priority)
        priority_color = "#28a745" if task.priority == "low" else "#ffc107" if task.priority == "normal" else "#dc3545"
        status_color = "#28a745" if task.status == "completed" else "#ffc107"
        recurring_info = []
        if task.is_recurring: pattern_text = format_recurrence_pattern(task.recurrence_pattern); recurring_info.extend([{"type": "separator", "margin": "md"}, {"type": "text", "text": f"⏰ 定期任務 ({pattern_text})", "size": "sm", "color": "#9C27B0", "margin": "sm"}, {"type": "text", "text": f"(已生成 {task.recurrence_count} 次)", "size": "xs", "color": "#9C27B0", "margin": "none"}])
        elif task.parent_task_id: parent_task = get_task_by_id(db, task_id=task.parent_task_id);
        if parent_task: parent_pattern_text = format_recurrence_pattern(parent_task.recurrence_pattern); recurring_info.extend([{"type": "separator", "margin": "md"}, {"type": "text", "text": f"🔄 定期任務衍生 (來自 T-{parent_task.id})", "size": "sm", "color": "#757575", "margin": "sm", "wrap": True}, {"type": "text", "text": f"({parent_pattern_text})", "size": "xs", "color": "#757575", "margin": "none"}])
        try:
            contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"任務詳情 T-{task_id_num}", "weight": "bold", "size": "lg"}]}, "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [{"type": "text", "text": task.content, "wrap": True, "weight": "bold", "size": "xl"}, {"type": "box", "layout": "baseline", "margin": "md", "contents": [{"type": "text", "text": "負責人:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"}, {"type": "text", "text": f"@{task.member.name}", "size": "sm", "color": "#1DB446", "flex": 4, "weight":"bold"}]}, {"type": "box", "layout": "baseline", "margin": "sm", "contents": [{"type": "text", "text": "優先級:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"}, {"type": "text", "text": priority_display, "size": "sm", "color": priority_color, "flex": 4, "weight":"bold"}]}, {"type": "box", "layout": "baseline", "margin": "sm", "contents": [{"type": "text", "text": "狀態:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"}, {"type": "text", "text": status_str + (f" ({completed_at_str})" if task.status == 'completed' and completed_at_str else ""), "size": "sm", "color": status_color, "flex": 4, "weight":"bold", "wrap":True}]}, {"type": "box", "layout": "baseline", "margin": "sm", "contents": [{"type": "text", "text": "截止日期:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"}, {"type": "text", "text": due_date_str, "size": "sm", "color": "#888888", "flex": 4}]}, {"type": "box", "layout": "baseline", "margin": "sm", "contents": [{"type": "text", "text": "建立時間:", "size": "sm", "color": "#888888", "flex": 2, "margin": "sm"}, {"type": "text", "text": created_at_str, "size": "sm", "color": "#888888", "flex": 4}]}, *recurring_info]}, "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": []}}
            footer_buttons = contents["footer"]["contents"]
            if task.status == 'pending': footer_buttons.append({"type": "button", "style": "primary", "color": "#28a745", "height": "sm", "action": {"type": "message", "label": "✅ 完成任務", "text": f"#完成 T-{task_id_num}"}})
            footer_buttons.append({"type": "box", "layout":"horizontal", "spacing":"sm", "contents":[{"type": "button", "style": "secondary", "color": "#ffc107", "height": "sm", "flex": 1, "action": {"type": "message", "label": "✏️ 編輯", "text": f"#編輯幫助 T-{task_id_num}"}}, {"type": "button", "style": "secondary", "color": "#dc3545", "height": "sm", "flex": 1, "action": {"type": "message", "label": "🗑️ 刪除", "text": f"#刪除 T-{task_id_num}"}}]})
            if task.is_recurring: footer_buttons.append({"type": "button", "style": "secondary", "color": "#9C27B0", "height": "sm", "action": {"type": "message", "label": "🚫 取消定期", "text": f"#取消定期 T-{task_id_num}"}})
            line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=f"任務 T-{task_id_num} 詳情", contents=contents)); return
        except Exception as e: logger.exception(f"創建任務詳情 Flex 失敗: {e}")
        reply_text = f"🔍 任務詳情 T-{task_id_num} 🔍\n..."; line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text)) # Fallback text
    except SQLAlchemyError as e: logger.exception(f"獲取任務詳情 T-{task_id_num} DB失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text=f"查詢任務 T-{task_id_num} 詳情DB錯誤。"))
    except Exception as e: logger.exception(f"獲取任務詳情 T-{task_id_num} 失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text=f"查詢任務 T-{task_id_num} 詳情內部錯誤。"))
def handle_draw_lots(reply_token: str, match: re.Match): # Same
    question = match.group(1); results = ["聖筊 👍 (同意)", "陰筊 👎 (不同意)", "笑筊 🤔 (重新問)"]; result = random.choice(results); reply_text = f"❓ 問題: {question}\n✨ 結果: {result}"
    try: result_emoji = "👍" if "聖筊" in result else "👎" if "陰筊" in result else "🤔"; result_color = "#28a745" if "聖筊" in result else "#dc3545" if "陰筊" in result else "#ffc107"
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "擲筊結果", "weight": "bold", "size": "lg"}]}, "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"問題: {question}", "wrap": True, "weight": "bold", "size": "md", "margin":"md"}, {"type": "box", "layout": "vertical", "margin": "xl", "contents": [{"type": "text", "text": result, "size": "xxl", "align": "center", "color": result_color, "weight": "bold"}]}]}, "footer": {"type": "box", "layout": "vertical", "spacing":"sm", "contents": [{"type": "button", "style": "primary", "color": result_color, "height": "sm", "action": {"type": "message", "label": f"再擲一次 {result_emoji}", "text": f"#擲筊 {question}"}}]}}
    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=reply_text, contents=contents))
    except Exception as e: logger.exception(f"創建擲筊 Flex 失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
def handle_random_pick(reply_token: str, match: re.Match): # Same
    options_text = match.group(1); options = [opt.strip() for opt in options_text.split() if opt.strip()]
    if not options: line_bot_api.reply_message(reply_token, TextSendMessage(text="請提供至少一個抽籤選項！")); return
    chosen = random.choice(options); reply_text = f"從 [{', '.join(options)}] {len(options)} 個選項中抽出：\n🎉 {chosen} 🎉"
    try: contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "抽籤結果", "weight": "bold", "size": "lg"}]}, "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"從 {len(options)} 個選項中抽出：", "size": "md", "color": "#555555", "wrap":True, "margin":"md"}, {"type": "box", "layout": "vertical", "margin": "xl", "contents": [{"type": "text", "text": chosen, "size": "xxl", "align": "center", "weight": "bold", "wrap": True, "color":"#2196F3"}]}]}, "footer": {"type": "box", "layout": "vertical", "spacing":"sm", "contents": [{"type": "text", "text": f"選項: {', '.join(options)}", "size": "xs", "color": "#888888", "wrap": True, "margin":"md"}, {"type": "separator", "margin":"md"}, {"type": "button", "style": "primary", "color": "#2196F3", "height": "sm", "action": {"type": "message", "label": "再抽一次", "text": f"#抽籤 {options_text}"}}]}}
    line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=reply_text, contents=contents))
    except Exception as e: logger.exception(f"創建抽籤 Flex 失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
def handle_batch_add_tasks(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session): # Same revised
    member_name = match.group(1); tasks_text = match.group(2).strip(); task_lines = [line.strip() for line in tasks_text.split('\n') if line.strip()]
    if not task_lines: line_bot_api.reply_message(reply_token, TextSendMessage(text="📝 批量新增任務格式說明...\n`#批量新增 @成員名稱`\n`[!優先級] 內容1 [日期]`...")); return
    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        try: member = create_member(db, name=member_name, group_id=group_id)
        except Exception as create_err: logger.exception(f"...建立成員失敗: {create_err}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text=f"建立成員 '{member_name}' 失敗...")); return
    created_tasks_info = []; failed_lines_info = []; priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; tasks_to_add = []
    for i, task_line in enumerate(task_lines):
        priority = "normal"; content = task_line; due_date_str = None; due_date = None; error_msg = None
        priority_match = re.match(r'^!(低|普通|高)\s+(.+)$', task_line)
        if priority_match: p_tag = priority_match.group(1); content = priority_match.group(2).strip();
        if p_tag == "低": priority = "low"; elif p_tag == "高": priority = "high"; else: priority = "normal"
        else: content = task_line.strip()
        date_match = re.search(r'(?:^|\s)(\d{4}/\d{1,2}/\d{1,2})$', content)
        if date_match: due_date_str = date_match.group(1); content = content[:date_match.start()].strip(); due_date = parse_date(due_date_str)
        if due_date is None: error_msg = f"日期格式錯誤 ({due_date_str})"
        if not content: error_msg = "任務內容為空"
        if error_msg: failed_lines_info.append({'line': task_line, 'error': error_msg})
        else:
            try: task_obj = Task(member_id=member.id, content=content, due_date=due_date, priority=priority, status='pending'); tasks_to_add.append(task_obj); priority_display = priority_map_display.get(priority, priority); task_summary = f"{priority_display} {content}";
            if due_date: task_summary += f" (截止: {due_date.strftime('%Y/%m/%d')})"; created_tasks_info.append({'summary_no_id': task_summary, 'obj': task_obj})
            except Exception as e: logger.exception(f"批量任務對象創建失敗: {e}"); failed_lines_info.append({'line': task_line, 'error': f"內部錯誤 ({type(e).__name__})"})
    final_summaries = [];
    if tasks_to_add:
        try:
            db.add_all(tasks_to_add); db.flush()
            for info in created_tasks_info: task_obj = info['obj'];
            if task_obj.id: final_summaries.append(f"T-{task_obj.id}: {info['summary_no_id']}")
            else: failed_lines_info.append({'line': info['summary_no_id'], 'error': "無法獲取任務ID"})
            db.commit(); logger.info(f"批量新增 {len(final_summaries)} 個任務成功 for {member.name}.")
        except SQLAlchemyError as e: db.rollback(); logger.exception(f"批量新增DB失敗: {e}");
        for info in created_tasks_info: failed_lines_info.append({'line': info['summary_no_id'], 'error': "資料庫儲存失敗"}); final_summaries = []
        except Exception as e: db.rollback(); logger.exception(f"批量新增未知錯誤: {e}");
        for info in created_tasks_info: failed_lines_info.append({'line': info['summary_no_id'], 'error': f"內部儲存錯誤 ({type(e).__name__})"}); final_summaries = []
    success_count = len(final_summaries); failure_count = len(failed_lines_info)
    if success_count == 0 and failure_count == 0: line_bot_api.reply_message(reply_token, TextSendMessage(text="未提供有效的任務內容。")); return
    alt_text = f"批量新增結果：成功 {success_count}, 失敗 {failure_count} (為 @{member.name})"
    try:
        bubble_contents = create_batch_add_result_bubble(member.name, final_summaries, failed_lines_info)
        line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text=alt_text, contents=bubble_contents))
    except Exception as flex_err: logger.error(f"創建批量新增結果 Flex 失敗: {flex_err}"); reply_text = f"批量新增任務結果 (@{member.name})：\n..."; # Build text...
        # Split long messages...
    max_len = 4900; messages_to_send = [];
    while len(reply_text) > max_len: split_pos = reply_text.rfind('\n', 0, max_len); if split_pos == -1: split_pos = max_len; messages_to_send.append(TextSendMessage(text=reply_text[:split_pos])); reply_text = reply_text[split_pos:].lstrip()
    messages_to_send.append(TextSendMessage(text=reply_text)); line_bot_api.reply_message(reply_token, messages=messages_to_send)
def handle_recurring_task(reply_token: str, match: re.Match, group_id: str, adder_user_id: str, db: Session): # For #定期 @... command
    member_name = match.group(1); priority_tag = match.group(2); task_content = match.group(3).strip(); recurrence_input = match.group(4)
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; priority = "normal"
    if priority_tag:
        if "低" in priority_tag: priority = "low"
        elif "高" in priority_tag: priority = "high"
    system_pattern, user_friendly_pattern = parse_recurrence_input(f"每{recurrence_input}")
    if not system_pattern: line_bot_api.reply_message(reply_token, TextSendMessage(text="無法識別的重複模式...")); return
    member = get_member_by_name_and_group(db, name=member_name, group_id=group_id)
    if not member:
        try: member = create_member(db, name=member_name, group_id=group_id)
        except Exception as create_err: logger.exception(f"...建立成員失敗: {create_err}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text=f"建立成員 '{member_name}' 失敗...")); return
    try:
        task = Task(member_id=member.id, content=task_content, status='recurring_master', priority=priority, is_recurring=True, recurrence_pattern=system_pattern, recurrence_count=0)
        db.add(task); db.commit()
        priority_display = priority_map_display.get(priority, priority)
        reply_text = f"✅ 已為 @{member.name} 新增定期任務：\n內容：{task.content}\n任務ID：T-{task.id} (此為定期模板)\n優先級：{priority_display}\n重複模式：{user_friendly_pattern}\n👉 ... #取消定期 T-{task.id} ..."
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
    except SQLAlchemyError as e: logger.exception(f"新增定期任務DB失敗: {e}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text="新增定期任務失敗 (DB)..."))
    except Exception as e: logger.exception(f"新增定期任務未知錯誤: {e}"); db.rollback(); line_bot_api.reply_message(reply_token, TextSendMessage(text="新增定期任務失敗 (Internal)..."))
def handle_cancel_recurring_task(reply_token: str, match: re.Match, group_id: str, user_id: str, db: Session): # Same
    task_id_num = int(match.group(1)); task = get_task_by_id(db, task_id=task_id_num)
    if not task: reply_text = f"❌ 找不到ID為 T-{task_id_num} 的任務。"
    elif not task.is_recurring: reply_text = f"❌ 任務 T-{task_id_num} 不是一個進行中的定期任務模板。"
    elif task.member.group_id != group_id: reply_text = f"❌ 任務 T-{task_id_num} 不屬於本群組/房間。"
    else:
        try:
            task_content_preview = task.content[:20]; member_name = task.member.name
            task.is_recurring = False; task.status = 'cancelled_recurring'; db.commit()
            reply_text = f"✅ 已取消 @{member_name} 的定期任務模板 T-{task_id_num}。\n內容：{task_content_preview}...\n將不再自動生成新任務。"
        except SQLAlchemyError as e: logger.exception(f"...取消定期任務失敗 (DB): {e}"); db.rollback(); reply_text = f"❌ 取消定期任務 T-{task_id_num} 失敗 (DB)。"
        except Exception as e: logger.exception(f"...取消定期任務失敗: {e}"); db.rollback(); reply_text = f"❌ 取消定期任務 T-{task_id_num} 失敗 (Internal)。"
    line_bot_api.reply_message(reply_token, TextSendMessage(text=reply_text))
def handle_recurring_list(reply_token: str, group_id: str, db: Session): # New handler
    logger.info(f"處理群組 {group_id} 的定期列表請求")
    try:
        recurring_tasks = db.query(Task).options(joinedload(Task.member)).filter(Task.is_recurring == True, Task.status == 'recurring_master', Task.member.has(group_id=group_id)).order_by(Member.name.asc(), Task.created_at.asc()).all()
        title = "本群組定期任務模板"
        if not recurring_tasks: line_bot_api.reply_message(reply_token, TextSendMessage(text=f"ℹ️ {title}：目前沒有設定任何定期任務。")); return
        try: bubble_json = create_recurring_list_bubble(title, recurring_tasks); line_bot_api.reply_message(reply_token, messages=[FlexSendMessage(alt_text=title, contents=bubble_json)])
        except Exception as e: logger.exception(f"創建/發送定期列表 Flex 失敗: {e}")
            # Fallback text...
        task_list_text = f"📋 {title} 📋 ({len(recurring_tasks)} 個)\n\n";
        for task in recurring_tasks: pattern_text = format_recurrence_pattern(task.recurrence_pattern); member_name = task.member.name if task.member else '未知'; task_list_text += f"• T-{task.id}: @{member_name} - {task.content[:20]}... ({pattern_text}) - 已生成 {task.recurrence_count} 次\n  操作: #詳情 T-{task.id} | #取消定期 T-{task.id}\n\n"
            # Split long messages...
        max_len = 4900; messages_to_send = [];
        while len(task_list_text) > max_len: split_pos = task_list_text.rfind('\n\n', 0, max_len); if split_pos == -1: split_pos = max_len; messages_to_send.append(TextSendMessage(text=task_list_text[:split_pos])); task_list_text = task_list_text[split_pos:].lstrip()
        messages_to_send.append(TextSendMessage(text=task_list_text)); line_bot_api.reply_message(reply_token, messages=messages_to_send)
    except SQLAlchemyError as e: logger.exception(f"列出定期任務DB失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text="查詢定期任務列表DB錯誤。"))
    except Exception as e: logger.exception(f"列出定期任務未知錯誤: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text="處理定期列表請求內部錯誤。"))


# --- Helper Functions ---
def parse_date(date_str: Optional[str]) -> Optional[datetime]: # Same
    if not date_str: return None
    try: return datetime.strptime(date_str, "%Y/%m/%d")
    except ValueError: return None
def format_recurrence_pattern(system_pattern: Optional[str]) -> str: # Same
    if not system_pattern: return "無"
    day_map_reverse = {"monday": "週一", "tuesday": "週二", "wednesday": "週三", "thursday": "週四", "friday": "週五", "saturday": "週六", "sunday": "週日"}
    if system_pattern == "daily": return "每天"
    elif system_pattern.startswith("weekly_"): day_en = system_pattern.split("_")[1]; return f"每{day_map_reverse.get(day_en, day_en)}"
    elif system_pattern.startswith("monthly_"): day = system_pattern.split("_")[1]; return f"每月{day}日"
    elif system_pattern.startswith("yearly_"): parts = system_pattern.split("_");
    if len(parts) >= 3: month, day = parts[1], parts[2]; return f"每年{month}月{day}日"
    return system_pattern

# --- Help Messages ---
# Updated help message
def send_help_message(reply_token: str):
    help_text = (
        "📋 **代辦事項機器人指令 v2.2.1** 📋\n\n"
        "✨ **常用指令** ✨\n"
        "`#新任務` - 引導式新增單一任務\n"
        "`#定期` - 引導式新增「定期」任務\n"
        "`#列表 [@成員]` - 顯示待辦任務\n"
        "`#定期列表` - 顯示定期任務模板\n" # Updated
        "`#完成 T-ID` - 標記任務完成\n"
        "`#詳情 T-ID` - 查看任務詳細資訊\n\n"
        "🔸 **進階新增** 🔸\n"
        "`#新增 @成員 [!優先級] 內容 [日期]`\n"
        "`#批量新增 @成員` (換行輸入多任務)\n"
        "`#定期 @成員 [!優先級] 內容 每週期`\n\n"
        "🔹 **管理任務** 🔹\n"
        "`#修改 T-ID [!優先級] 新內容 [日期]`\n"
        "`#刪除 T-ID`\n"
        "`#取消定期 T-ID` (取消定期模板)\n\n"
        "🕹️ **其他功能** 🕹️\n"
        "`#擲筊 問題`\n"
        "`#抽籤 選項1 選項2 ...`\n\n"
        "❓ **獲取幫助** ❓\n"
        "`#幫助` (本訊息)\n"
        "`#幫助新增` (新增指令說明)\n"
        "`#編輯幫助 T-ID` (修改指令說明)"
    )
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text,
          quick_reply=QuickReply(items=[
              QuickReplyButton(action=MessageAction(label="#新任務", text="#新任務")),
              QuickReplyButton(action=MessageAction(label="#定期", text="#定期")),
              QuickReplyButton(action=MessageAction(label="#列表", text="#列表")),
              QuickReplyButton(action=MessageAction(label="#定期列表", text="#定期列表")),
          ])))
    except Exception as e: logger.warning(f"發送 QuickReply 幫助失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))
# ... (send_add_help_message, send_edit_help_message - remain same) ...
def send_add_help_message(reply_token: str):
    help_text = ("📝 **如何新增任務** 📝\n\n1️⃣ **引導式新增 (推薦):**\n   輸入 `#新任務`...\n\n2️⃣ **指令式新增 (單一任務):**\n   `#新增 @成員...`\n\n3️⃣ **批量新增 (多個任務):**\n   `#批量新增 @成員`\n   (換行...)\n\n4️⃣ **定期任務:**\n   - 引導式: 輸入 `#定期`\n   - 指令式: `#定期 @成員...每週期`\n     (週期: `每天`, `每週一`..., `每月15日`..., `每年12月25日`...)")
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))
def send_edit_help_message(reply_token: str, task_id: str):
    help_text = (f"✏️ **如何編輯任務 T-{task_id}** ✏️\n\n`#修改 T-{task_id} [!優先級] 新任務內容 [新截止日期]`\n\n說明:\n - `!優先級`: 可選...\n - `新任務內容`: **必填**...\n - `新截止日期`: 可選...\n\n*範例 1...*\n*範例 2...*\n*範例 3...*")
    line_bot_api.reply_message(reply_token, TextSendMessage(text=help_text))

# --- Flex/Text Message Helpers ---
# create_task_list_bubble, create_task_list_text, create_batch_add_result_bubble, create_recurring_list_bubble
# ... (Functions from previous steps, ensure they are robust) ...
def create_recurring_list_bubble(title: str, recurring_tasks: List[Task]): # New helper
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; priority_color_map = {"low": "#28a745", "normal": "#ffc107", "high": "#dc3545"}
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg", "color": "#9C27B0"}]}, "body": {"type": "box", "layout": "vertical", "spacing":"lg", "contents": []}, "footer": {"type": "box", "layout": "vertical", "contents": [{"type": "button", "action": {"type": "message", "label": "✨ 新增定期任務", "text": "#定期"}, "style": "primary", "color":"#9C27B0", "height":"sm"}]}}
    body_contents = contents["body"]["contents"]
    if not recurring_tasks: body_contents.append({"type": "text", "text": "目前沒有設定定期任務模板。", "wrap": True, "color": "#555555", "size": "md"}); return contents
    for i, task in enumerate(recurring_tasks):
        try:
            member_name = task.member.name if task.member else '未知成員'; priority = task.priority or "normal"; priority_display = priority_map_display.get(priority, priority); priority_color = priority_color_map.get(priority, "#888888"); pattern_text = format_recurrence_pattern(task.recurrence_pattern); count_text = f"已生成 {task.recurrence_count or 0} 次"
            task_item_elements = [{"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": f"T-{task.id}", "size": "sm", "color": "#888888", "flex": 1, "weight":"bold"}, {"type": "text", "text": priority_display, "size": "xs", "color": priority_color, "align": "center", "flex": 1, "weight":"bold"}, {"type": "text", "text": f"@{member_name}", "size": "sm", "color": "#1DB446", "align": "end", "flex": 2, "weight":"bold"}]}, {"type": "text", "text": task.content, "wrap": True, "weight": "regular", "margin": "md", "size":"md"}, {"type": "box", "layout":"horizontal", "margin": "sm", "contents":[{"type": "text", "text": f"週期: {pattern_text}", "size": "xs", "color": "#9C27B0", "flex":2}, {"type": "text", "text": count_text, "size": "xs", "color": "#555555", "align":"end", "flex":1}]}]
            buttons_box = {"type": "box", "layout": "horizontal", "margin": "lg", "spacing":"sm", "contents": [{"type": "button", "style": "secondary", "color": "#2196F3", "height": "sm", "flex": 1, "action": {"type": "message", "label": "詳情", "text": f"#詳情 T-{task.id}"}}, {"type": "button", "style": "secondary", "color": "#9C27B0", "height": "sm", "flex": 1, "action": {"type": "message", "label": "取消定期", "text": f"#取消定期 T-{task.id}"}}]}; task_item_elements.append(buttons_box)
            body_contents.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "md", "backgroundColor": "#F3E5F5", "cornerRadius": "md", "contents": task_item_elements})
            if i < len(recurring_tasks) - 1: body_contents.append({"type":"separator", "margin":"lg"})
        except Exception as task_err: logger.error(f"處理定期模板 T-{task.id} 顯示錯誤: {task_err}"); body_contents.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "md", "backgroundColor": "#EEEEEE", "cornerRadius": "md", "contents": [{"type": "text", "text": f"❌ 無法顯示模板 T-{task.id} ({type(task_err).__name__})", "color": "#dc3545", "size":"sm", "wrap":True}]})
    return contents
def create_task_list_bubble(title: str, tasks: List[Task], db: Session): # Existing revised
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; priority_color_map = {"low": "#28a745", "normal": "#ffc107", "high": "#dc3545"}
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg"}]}, "body": {"type": "box", "layout": "vertical", "spacing":"lg", "contents": []}, "footer": {"type": "box", "layout": "horizontal", "spacing": "md", "contents": [{"type": "button", "style": "primary", "color": "#1E88E5", "height": "sm", "flex": 1, "action": {"type": "message", "label": "✨ 新增任務", "text": "#新任務"}}, {"type": "button", "style": "secondary", "color": "#6c757d", "height": "sm", "flex": 1, "action": {"type": "message", "label": "❓ 幫助", "text": "#幫助"}}]}}
    body_contents = contents["body"]["contents"]
    for i, task in enumerate(tasks):
        try:
            member_name = task.member.name if task.member else '未知成員'; priority = task.priority or "normal"; priority_display = priority_map_display.get(priority, priority); priority_color = priority_color_map.get(priority, "#888888")
            task_item_elements = [{"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": f"T-{task.id}", "size": "sm", "color": "#888888", "flex": 1, "weight":"bold"}, {"type": "text", "text": priority_display, "size": "xs", "color": priority_color, "align": "center", "flex": 1, "weight":"bold"}, {"type": "text", "text": f"@{member_name}", "size": "sm", "color": "#1DB446", "align": "end", "flex": 2, "weight":"bold"}]}, {"type": "text", "text": task.content, "wrap": True, "weight": "regular", "margin": "md", "size":"md"}]
            if task.due_date:
                try:
                    due_date_obj = task.due_date; today = date.today(); days_left = (due_date_obj - today).days
                    if days_left < 0: due_date_status = f"(已逾期 {-days_left} 天)"; color = "#dc3545"
                    elif days_left == 0: due_date_status = "(今天截止!)"; color = "#ffc107"
                    elif days_left == 1: due_date_status = "(明天截止!)"; color = "#ffc107"
                    elif days_left < 4: due_date_status = f"({days_left} 天後截止)"; color = "#ffc107"
                    else: due_date_status = f"({days_left} 天)"; color = "#888888"
                    due_date_str_display = due_date_obj.strftime('%Y/%m/%d')
                    task_item_elements.append({"type": "text", "text": f"截止: {due_date_str_display} {due_date_status}", "size": "xs", "color": color, "margin": "sm"})
                except Exception as date_err: logger.error(f"處理任務 T-{task.id} 截止日期失敗 (Flex): {date_err}"); task_item_elements.append({"type": "text", "text": f"截止: 日期處理錯誤", "size": "xs", "color": "#dc3545", "margin": "sm"})
            buttons_box = {"type": "box", "layout": "horizontal", "margin": "lg", "spacing":"sm", "contents": [{"type": "button", "style": "primary", "color": "#4CAF50", "height": "sm", "flex": 1, "action": {"type": "message", "label": "完成", "text": f"#完成 T-{task.id}"}}, {"type": "button", "style": "secondary", "color": "#2196F3", "height": "sm", "flex": 1, "action": {"type": "message", "label": "詳情", "text": f"#詳情 T-{task.id}"}}]}; task_item_elements.append(buttons_box)
            if task.parent_task_id: task_item_elements.append({"type": "text", "text": f"🔄 定期衍生 (來自 T-{task.parent_task_id})", "size": "xs", "color": "#757575", "margin": "md"})
            body_contents.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "md", "backgroundColor": "#FAFAFA", "cornerRadius": "md", "contents": task_item_elements})
            if i < len(tasks) - 1: body_contents.append({"type":"separator", "margin":"lg"})
        except Exception as task_err: logger.error(f"處理列表任務 T-{task.id} 時發生錯誤: {task_err}"); body_contents.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "md", "backgroundColor": "#EEEEEE", "cornerRadius": "md", "contents": [{"type": "text", "text": f"❌ 無法顯示任務 T-{task.id} ({type(task_err).__name__})", "color": "#dc3545", "size":"sm", "wrap":True}]})
    return contents
def create_task_list_text(title: str, tasks: List[Task], db: Session): # Existing fallback
    priority_map_display = {"low": "🟢 低", "normal": "🟡 普通", "high": "🔴 高"}; result = f"📋 {title} 📋\n\n"
    for i, task in enumerate(tasks, 1):
        try:
            member_name = task.member.name if task.member else '未知成員'; priority = task.priority or "normal"; priority_display = priority_map_display.get(priority, priority)
            result += f"【任務 T-{task.id}】 {priority_display}\n👤 負責人: @{member_name}\n📝 內容: {task.content}\n"
            if task.due_date:
                try: due_date_obj = task.due_date; today = date.today(); days_left = (due_date_obj - today).days; due_date_str_display = due_date_obj.strftime('%Y/%m/%d'); status = ("(⚠️ 已逾期)" if days_left < 0 else "(⚠️ 今天截止!)" if days_left == 0 else f"(⚠️ {days_left}天後截止)" if days_left < 4 else f"(還有 {days_left} 天)"); result += f"📅 截止: {due_date_str_display} {status}\n"
                except Exception as date_err: logger.error(f"處理任務 T-{task.id} 截止日期失敗 (Text): {date_err}"); result += f"📅 截止: 日期錯誤\n"
            else: result += f"📅 截止: 無\n"
            if task.parent_task_id: result += f"🔄 定期衍生 (來自 T-{task.parent_task_id})\n"
            result += f"👉 操作: #完成 T-{task.id} | #詳情 T-{task.id}\n"
            if i < len(tasks): result += "\n" + ("-" * 20) + "\n\n"
        except Exception as e: logger.error(f"生成任務 T-{task.id} 文字描述失敗: {e}"); result += f"【任務 T-{task.id}】\n❌ 無法顯示此任務詳情 ({type(e).__name__})\n\n";
        if i < len(tasks): result += "\n" + ("-" * 20) + "\n\n"
    return result
def create_batch_add_result_bubble(member_name: str, success_summaries: List[str], failed_lines_info: List[Dict[str, str]]): # Existing revised
    success_count = len(success_summaries); failure_count = len(failed_lines_info); header_text = f"批量新增結果 (@{member_name})"
    header_color = "#1DB446" if success_count > 0 and failure_count == 0 else "#ffc107" if success_count > 0 and failure_count > 0 else "#dc3545"
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": header_text, "weight": "bold", "size": "lg", "color": header_color}]}, "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [{"type": "text", "text": f"✅ 成功: {success_count}  |  ❌ 失敗: {failure_count}", "weight": "bold", "size": "md", "wrap": True}]}, "footer": {"type": "box", "layout": "vertical", "contents": [{"type": "button", "action": {"type": "message", "label": "查看我的任務列表", "text": f"#列表 @{member_name}"}, "style": "primary", "color":"#1DB446", "height":"sm"}]}}
    body_contents = contents["body"]["contents"]
    if success_summaries:
        body_contents.append({"type": "separator", "margin": "lg"}); body_contents.append({"type": "text", "text": "成功新增列表:", "weight": "bold", "size": "sm", "color": "#1DB446", "margin": "md"})
        success_box = {"type": "box", "layout": "vertical", "margin": "sm", "spacing":"xs", "contents": []}
        for summary in success_summaries[:8]: success_box["contents"].append({"type": "text", "text": f"• {summary}", "size": "sm", "wrap": True})
        if len(success_summaries) > 8: success_box["contents"].append({"type": "text", "text": f"... (共 {success_count} 個)", "size": "xs", "color": "#555555", "margin": "sm"})
        body_contents.append(success_box)
    if failed_lines_info:
        body_contents.append({"type": "separator", "margin": "lg"}); body_contents.append({"type": "text", "text": "失敗行與原因:", "weight": "bold", "size": "sm", "color": "#dc3545", "margin": "md"})
        failed_box = {"type": "box", "layout": "vertical", "margin": "sm", "spacing":"xs", "contents": []}
        for failed in failed_lines_info[:5]: line_preview = failed['line'][:60] + ('...' if len(failed['line']) > 60 else ''); failed_box["contents"].append({"type": "box", "layout":"vertical", "margin":"xxs", "contents":[{"type": "text", "text": f"行: \"{line_preview}\"", "size": "xs", "wrap": True, "color": "#555555"}, {"type": "text", "text": f"原因: {failed['error']}", "size": "xs", "wrap": True, "color": "#dc3545", "weight":"bold"}]})
        if len(failed_lines_info) > 5: failed_box["contents"].append({"type": "text", "text": f"... (共 {failure_count} 行失敗)", "size": "xs", "color": "#dc3545", "margin": "sm"})
        body_contents.append(failed_box)
    return contents

# --- n8n Integration API Endpoints ---
# Corrected Recurring Task Generation API with Enhanced Notification
@app.route("/api/generate-recurring-tasks", methods=['POST'])
def api_generate_recurring_tasks():
    api_key = request.headers.get('X-API-KEY')
    if not api_key or api_key != N8N_API_KEY: logger.warning("未授權的定期任務生成請求"); return jsonify({"error": "Unauthorized"}), 401
    logger.info("開始生成定期任務..."); current_date = datetime.now().date(); day_of_week = current_date.strftime('%A').lower(); day_of_month = current_date.day; month_day = f"{current_date.month}_{current_date.day}"; weekly_pattern = f"weekly_{day_of_week}"; monthly_pattern = f"monthly_{day_of_month}"; yearly_pattern = f"yearly_{month_day}"; daily_pattern = "daily"
    logger.info(f"當前日期: {current_date}, 匹配模式: daily='{daily_pattern}', weekly='{weekly_pattern}', monthly='{monthly_pattern}', yearly='{yearly_pattern}'")
    created_tasks_report = []; processed_master_ids = set(); notifications = {} # group_id -> {'new': [], 'other_pending': []}
    try:
        with get_db() as db:
            recurring_master_tasks = db.query(Task).options(joinedload(Task.member)).filter(Task.is_recurring == True, Task.status == 'recurring_master', or_(Task.recurrence_pattern == daily_pattern, Task.recurrence_pattern == weekly_pattern, Task.recurrence_pattern == monthly_pattern, Task.recurrence_pattern == yearly_pattern)).all()
            logger.info(f"找到 {len(recurring_master_tasks)} 個符合條件的定期模板。")
            new_tasks_to_add = []; newly_generated_task_ids = set()
            for master_task in recurring_master_tasks:
                if master_task.id in processed_master_ids: continue; logger.debug(f"處理模板 T-{master_task.id} ({master_task.recurrence_pattern})")
                new_task = Task(member_id=master_task.member_id, content=master_task.content, status='pending', priority=master_task.priority, parent_task_id=master_task.id, is_recurring=False)
                new_tasks_to_add.append(new_task); master_task.recurrence_count = (master_task.recurrence_count or 0) + 1; processed_master_ids.add(master_task.id)
                group_id = master_task.member.group_id
                if group_id:
                    if group_id not in notifications: notifications[group_id] = {'new': [], 'other_pending': []}
                    member_name = master_task.member.name; priority_map = {"low": "🟢", "normal": "🟡", "high": "🔴"}; p_emoji = priority_map.get(master_task.priority, "")
                    task_info = f"{p_emoji} @{member_name}: {new_task.content}"; notifications[group_id]['new'].append({'info': task_info, 'obj': new_task})
            if not new_tasks_to_add: logger.info("沒有新的定期任務需要創建。"); return jsonify({"success": True, "created_count": 0, "message":"沒有新任務生成。","tasks": []})
            db.add_all(new_tasks_to_add); db.flush()
            for task_report_obj in new_tasks_to_add:
                if task_report_obj.id: newly_generated_task_ids.add(task_report_obj.id); created_tasks_report.append({"new_task_id": f"T-{task_report_obj.id}", "master_task_id": f"T-{task_report_obj.parent_task_id}" if task_report_obj.parent_task_id else None, "member_id": task_report_obj.member_id, "content": task_report_obj.content})
                else: logger.error(f"新任務未能獲取ID (來自 T-{task_report_obj.parent_task_id})")
            involved_group_ids = list(notifications.keys())
            if involved_group_ids:
                other_pending_tasks = db.query(Task).options(joinedload(Task.member)).filter(Task.member.has(Member.group_id.in_(involved_group_ids)), Task.status == 'pending', Task.id.notin_(newly_generated_task_ids)).order_by(Task.due_date.asc().nulls_last(), Task.priority.desc()).all()
                logger.info(f"查詢到 {len(other_pending_tasks)} 個其他待辦任務。")
                for task in other_pending_tasks:
                    group_id = task.member.group_id
                    if group_id in notifications:
                        member_name = task.member.name; priority_map = {"low": "🟢", "normal": "🟡", "high": "🔴"}; p_emoji = priority_map.get(task.priority, ""); due_date_str = f" (截止:{task.due_date.strftime('%y/%m/%d')})" if task.due_date else ""
                        task_info = f"{p_emoji} @{member_name}: {task.content}{due_date_str}"; notifications[group_id]['other_pending'].append({'info': task_info, 'id': task.id})
            MAX_TASKS_PER_SECTION = 8
            for group_id, data in notifications.items():
                if not group_id: continue
                try:
                    notif_text = "🗓️ **今日任務提醒** 🗓️\n\n"; new_tasks_today = data.get('new', []); other_pending = data.get('other_pending', [])
                    if not new_tasks_today and not other_pending: logger.info(f"Group {group_id}: 無任務可通知。"); continue
                    if new_tasks_today:
                        notif_text += "✨ **今日新增定期任務：**\n"; count = 0
                        for item in new_tasks_today: task_obj = item['obj'];
                        if task_obj.id: notif_text += f"• T-{task_obj.id} {item['info']}\n"; count += 1
                        else: notif_text += f"• (新) {item['info']}\n"; count += 1
                        if count >= MAX_TASKS_PER_SECTION: notif_text += f"... (等共計 {len(new_tasks_today)} 個新任務)\n"; break
                        if count == 0: notif_text += "_無_\n"; notif_text += "\n"
                    if other_pending:
                        notif_text += "⏳ **其他待辦任務：**\n"; count = 0
                        for item in other_pending: notif_text += f"• T-{item['id']} {item['info']}\n"; count += 1
                        if count >= MAX_TASKS_PER_SECTION: notif_text += f"... (等共計 {len(other_pending)} 個其他任務)\n"; break
                        if count == 0: notif_text += "_無_\n"; notif_text += "\n"
                    if not new_tasks_today and not other_pending: continue
                    notif_text += f"👉 使用 `#列表` 查看完整待辦清單。"
                    logger.info(f"發送合併通知到 Group ID: {group_id} (新:{len(new_tasks_today)}, 其他:{len(other_pending)})")
                    line_bot_api.push_message(group_id, TextSendMessage(text=notif_text))
                except Exception as push_err: logger.exception(f"發送合併通知到 {group_id} 失敗: {push_err}")
            db.commit(); logger.info(f"成功生成並提交 {len(created_tasks_report)} 個新任務。")
            return jsonify({"success": True, "created_count": len(created_tasks_report), "tasks": created_tasks_report})
    except SQLAlchemyError as e: logger.exception(f"生成定期任務DB錯誤: {e}"); db.rollback(); return jsonify({"success": False, "error": f"Database error: {e}"}), 500
    except Exception as e: logger.exception(f"生成定期任務未知錯誤: {e}"); db.rollback(); return jsonify({"success": False, "error": f"Internal server error: {e}"}), 500
# ... (api_pending_tasks, api_send_reminder - same as revised version) ...
@app.route("/api/pending-tasks", methods=['GET'])
def api_pending_tasks(): # Existing, used by enhanced notification
    api_key = request.headers.get('X-API-KEY')
    if not api_key or api_key != N8N_API_KEY: return jsonify({"error": "Unauthorized"}), 401
    target_group = request.args.get('group_id', TARGET_GROUP_ID) # Allow specifying group_id
    if not target_group: return jsonify({"error": "Target Group ID is required."}), 400
    try:
        with get_db() as db:
            tasks = db.query(Task).options(joinedload(Task.member)).filter(Task.member.has(group_id=target_group), Task.status == 'pending').order_by(Task.due_date.asc().nulls_last(), Task.priority.desc(), Task.created_at.asc()).all()
            result = [] # ... (Build result list same as before) ...
            today = date.today()
            for task in tasks:
                due_date_str, days_left = None, None;
                if task.due_date:
                    try: due_date_obj = task.due_date; if isinstance(due_date_obj, datetime): due_date_obj = due_date_obj.date(); days_left = (due_date_obj - today).days; due_date_str = due_date_obj.strftime('%Y/%m/%d')
                    except Exception as e: logger.warning(f"API日期處理錯誤 {task.id}: {e}"); due_date_str = "日期錯誤"
                result.append({"id": task.id, "task_id": f"T-{task.id}", "member": task.member.name if task.member else '未知', "member_id": task.member_id, "content": task.content, "priority": task.priority, "status": task.status, "due_date": due_date_str, "days_left": days_left, "is_recurring": task.is_recurring, "parent_task_id": task.parent_task_id, "created_at": task.created_at.isoformat() if task.created_at else None, "completed_at": task.completed_at.isoformat() if task.completed_at else None})
            return jsonify({"tasks": result, "count": len(result), "group_id": target_group})
    except SQLAlchemyError as e: logger.exception(f"API /api/pending-tasks DB錯誤: {e}"); return jsonify({"error": "Internal DB error."}), 500
    except Exception as e: logger.exception(f"API /api/pending-tasks 錯誤: {e}"); return jsonify({"error": "Internal server error."}), 500
@app.route("/api/send-reminder", methods=['POST'])
def api_send_reminder(): # Existing
    api_key = request.headers.get('X-API-KEY');
    if not api_key or api_key != N8N_API_KEY: return jsonify({"error": "Unauthorized"}), 401
    # Allow target_id override, default to TARGET_GROUP_ID if configured
    default_target = TARGET_GROUP_ID
    data = request.get_json()
    if not data or 'message' not in data: return jsonify({"error": "Missing 'message'"}), 400
    message_text = data['message']; target_id = data.get('target_id', default_target) # Use default if available
    if not target_id: return jsonify({"error": "Target ID is required (either in request or default config)."}), 400
    if not message_text: return jsonify({"error": "Message empty"}), 400
    try:
        line_bot_api.push_message(target_id, messages=[TextSendMessage(text=message_text)])
        logger.info(f"API 發送提醒至 ID: {target_id}")
        return jsonify({"success": True, "message": "Reminder sent", "target_id": target_id})
    except Exception as e: logger.exception(f"API 發送提醒至 {target_id} 失敗: {e}"); return jsonify({"success": False, "error": f"Send failed: {e}"}), 500

# --- Informational Forms ---
# ... (send_add_task_form, send_recurring_task_form - remain same informational versions) ...
def send_add_task_form(reply_token: str, db: Session, group_id: str): # Informational
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "新增任務選項", "weight": "bold", "size": "xl", "color": "#2196F3"}]}, "body": {"type": "box", "layout": "vertical", "spacing":"lg", "contents": [{"type": "text", "text": "你可以使用以下方式新增任務：", "wrap": True}, {"type": "button", "style": "primary", "color": "#1E88E5", "action": {"type": "message", "label": "引導式新增 (#新任務)", "text": "#新任務"}}, {"type": "button", "style": "secondary", "action": {"type": "message", "label": "查看指令說明 (#幫助新增)", "text": "#幫助新增"}}, {"type": "box", "layout":"vertical", "margin":"lg", "contents":[{"type":"text", "text":"或者直接輸入完整指令，例如：", "size":"sm", "color":"#888888", "wrap":True}, {"type":"text", "text":"#新增 @成員 !優先級 內容 日期", "size":"xs", "color":"#555555", "wrap":True}, {"type":"text", "text":"#批量新增 @成員\\n任務1\\n任務2", "size":"xs", "color":"#555555", "wrap":True}]}]}}
    try: line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="新增任務選項", contents=contents))
    except Exception as e: logger.exception(f"發送任務新增表單失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text="無法顯示新增選項..."))
def send_recurring_task_form(reply_token: str, db: Session, group_id: str): # Informational
    contents = {"type": "bubble", "header": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "新增定期任務說明", "weight": "bold", "size": "xl", "color": "#9C27B0"}]}, "body": {"type": "box", "layout": "vertical", "spacing":"lg", "contents": [{"type": "text", "text": "你可以使用以下方式新增定期任務：", "wrap": True}, {"type": "button", "style": "primary", "color": "#9C27B0", "action": {"type": "message", "label": "引導式新增 (#定期)", "text": "#定期"}}, {"type": "separator"}, {"type": "text", "text":"或使用指令:", "size":"sm", "color":"#555555"}, {"type": "box", "layout":"vertical", "margin":"md", "contents":[{"type":"text", "text":"`#定期 @成員 [!優先級] 內容 每週期`", "wrap":True, "size":"sm"}, {"type":"text", "text":"週期範例:", "size":"sm", "margin":"sm", "weight":"bold"}, {"type":"text", "text":"• `每天`\n• `每週一`...\n• `每月15日`...\n• `每年12月25日`...", "wrap":True, "size":"xs", "color":"#555555"}]}, {"type": "separator"}, {"type": "button", "style": "secondary", "action": {"type": "message", "label": "查看完整說明 (#幫助)", "text": "#幫助"}}]}}
    try: line_bot_api.reply_message(reply_token, FlexSendMessage(alt_text="新增定期任務說明", contents=contents))
    except Exception as e: logger.exception(f"發送定期任務新增表單失敗: {e}"); line_bot_api.reply_message(reply_token, TextSendMessage(text="無法顯示定期任務說明..."))

# --- Main Execution Block ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    logger.info(f"讀取到的端口配置為: {port}")
    host = '0.0.0.0'
    if IN_REPLIT: logger.info(f"在 Replit 環境中運行，將使用 host='{host}' 和 port={port}")
    logger.info(f"Flask 應用啟動於 host={host}, port={port}")
    try:
        # Set debug=True ONLY for development testing, False for deployment
        app.run(host=host, port=port, debug=False)
    except OSError as e:
        logger.error(f"無法在端口 {port} 上啟動 Flask: {e}")
        logger.error("請檢查該端口是否已被其他程序佔用，或嘗試修改 PORT 環境變數。")
    except Exception as e:
        logger.exception(f"啟動 Flask 應用時發生未預期錯誤: {e}")