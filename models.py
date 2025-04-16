# models.py - 簡化版資料庫模型（直接使用 SQLite）
import sqlite3
import os
import json
from datetime import datetime
from contextlib import contextmanager

DB_FILE = 'line_bot.db'

def dict_factory(cursor, row):
    """將結果轉換為字典格式"""
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

@contextmanager
def get_db_connection():
    """提供資料庫連接的上下文管理器"""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = dict_factory
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    """初始化資料庫，建立所需的表格"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 成員表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            line_user_id TEXT,
            group_id TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 任務表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            due_date TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            FOREIGN KEY (member_id) REFERENCES members (id)
        )
        ''')
        
        conn.commit()

class Member:
    """成員模型"""
    def __init__(self, id=None, name=None, line_user_id=None, group_id=None, created_at=None):
        self.id = id
        self.name = name
        self.line_user_id = line_user_id
        self.group_id = group_id
        self.created_at = created_at
    
    @staticmethod
    def get_by_name_and_group(name, group_id):
        """根據名稱和群組ID獲取成員"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM members WHERE name = ? AND group_id = ?",
                (name, group_id)
            )
            result = cursor.fetchone()
            
            if result:
                return Member(**result)
            return None
    
    @staticmethod
    def get_by_id(id):
        """根據ID獲取成員"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM members WHERE id = ?", (id,))
            result = cursor.fetchone()
            
            if result:
                return Member(**result)
            return None
    
    def save(self):
        """保存或更新成員資料"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            if self.id:
                # 更新現有成員
                cursor.execute(
                    """
                    UPDATE members 
                    SET name = ?, line_user_id = ?, group_id = ? 
                    WHERE id = ?
                    """,
                    (self.name, self.line_user_id, self.group_id, self.id)
                )
            else:
                # 新增成員
                cursor.execute(
                    """
                    INSERT INTO members (name, line_user_id, group_id, created_at) 
                    VALUES (?, ?, ?, ?)
                    """,
                    (self.name, self.line_user_id, self.group_id, datetime.now().isoformat())
                )
                self.id = cursor.lastrowid
            
            conn.commit()
            return self

class Task:
    """任務模型"""
    def __init__(self, id=None, member_id=None, content=None, status='pending', 
                 due_date=None, created_at=None, completed_at=None):
        self.id = id
        self.member_id = member_id
        self.content = content
        self.status = status
        self.due_date = due_date
        self.created_at = created_at
        self.completed_at = completed_at
    
    @staticmethod
    def get_by_id(id):
        """根據ID獲取任務"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE id = ?", (id,))
            result = cursor.fetchone()
            
            if result:
                task = Task(**result)
                # 處理日期格式
                if task.due_date:
                    task.due_date = datetime.fromisoformat(task.due_date)
                if task.created_at:
                    task.created_at = datetime.fromisoformat(task.created_at)
                if task.completed_at:
                    task.completed_at = datetime.fromisoformat(task.completed_at)
                return task
            return None
    
    @staticmethod
    def get_by_member_id(member_id, status='pending'):
        """獲取成員的任務"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM tasks WHERE member_id = ? AND status = ?",
                (member_id, status)
            )
            results = cursor.fetchall()
            
            tasks = []
            for result in results:
                task = Task(**result)
                # 處理日期格式
                if task.due_date:
                    task.due_date = datetime.fromisoformat(task.due_date)
                if task.created_at:
                    task.created_at = datetime.fromisoformat(task.created_at)
                if task.completed_at:
                    task.completed_at = datetime.fromisoformat(task.completed_at)
                tasks.append(task)
            
            return tasks
    
    @staticmethod
    def get_by_group_id(group_id, status='pending'):
        """獲取群組的任務"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT t.* FROM tasks t
                JOIN members m ON t.member_id = m.id
                WHERE m.group_id = ? AND t.status = ?
                """,
                (group_id, status)
            )
            results = cursor.fetchall()
            
            tasks = []
            for result in results:
                task = Task(**result)
                # 處理日期格式
                if task.due_date:
                    task.due_date = datetime.fromisoformat(task.due_date)
                if task.created_at:
                    task.created_at = datetime.fromisoformat(task.created_at)
                if task.completed_at:
                    task.completed_at = datetime.fromisoformat(task.completed_at)
                tasks.append(task)
            
            return tasks
    
    def save(self):
        """保存或更新任務資料"""
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # 處理日期格式
            due_date_str = None
            if self.due_date:
                if isinstance(self.due_date, str):
                    due_date_str = self.due_date
                else:
                    due_date_str = self.due_date.isoformat()
            
            completed_at_str = None
            if self.completed_at:
                if isinstance(self.completed_at, str):
                    completed_at_str = self.completed_at
                else:
                    completed_at_str = self.completed_at.isoformat()
            
            if self.id:
                # 更新現有任務
                cursor.execute(
                    """
                    UPDATE tasks 
                    SET member_id = ?, content = ?, status = ?, 
                        due_date = ?, completed_at = ? 
                    WHERE id = ?
                    """,
                    (self.member_id, self.content, self.status, 
                     due_date_str, completed_at_str, self.id)
                )
            else:
                # 新增任務
                cursor.execute(
                    """
                    INSERT INTO tasks 
                    (member_id, content, status, due_date, created_at) 
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.member_id, self.content, self.status, 
                     due_date_str, datetime.now().isoformat())
                )
                self.id = cursor.lastrowid
            
            conn.commit()
            return self