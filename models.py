# models.py (SQLAlchemy Version for PostgreSQL - with Type Hint Fixes)
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint, Boolean
)
# --- Type Hint Fix 1: Import Session from sqlalchemy.orm and typing helpers ---
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session
from typing import Optional, List
# --- End Fix 1 ---
from sqlalchemy.sql import func
from contextlib import contextmanager
from datetime import datetime
from dotenv import load_dotenv
import logging

# 設置日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
DATABASE_URL = os.environ.get('DATABASE_URL')

# 檢測是否在 Replit 環境中運行，若是則自動配置 PostgreSQL 連接
IN_REPLIT = os.environ.get('REPL_ID') is not None
if IN_REPLIT and not DATABASE_URL:
    # 使用 Replit 的 Secrets 管理器存儲 PostgreSQL 憑據
    PGUSER = os.environ.get('PGUSER')
    PGPASSWORD = os.environ.get('PGPASSWORD')
    PGHOST = os.environ.get('PGHOST')
    PGDATABASE = os.environ.get('PGDATABASE')
    PGPORT = os.environ.get('PGPORT', '5432')
    
    if PGUSER and PGPASSWORD and PGHOST and PGDATABASE:
        DATABASE_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}"
        os.environ['DATABASE_URL'] = DATABASE_URL  # 設置環境變數供其他模組使用
        logger.info("已從 Replit Secrets 設置 PostgreSQL 連接。")
    else:
        logger.error("在 Replit 環境中，但未設置完整 PostgreSQL 連接信息。請在 Secrets 中配置 PGUSER, PGPASSWORD, PGHOST, PGDATABASE。")

if not DATABASE_URL:
    raise ValueError("環境變數 DATABASE_URL 未設定！")

# 修復 PostgreSQL URL (Render 兼容)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    logger.info("已將 postgres:// 修正為 postgresql:// 以兼容 SQLAlchemy")

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)
Base = declarative_base()

@contextmanager
def get_db():
    db: Session = SessionLocal() # Use Session for hinting
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

class Member(Base):
    __tablename__ = "members"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    line_user_id = Column(String, unique=True, index=True, nullable=True)
    group_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    tasks = relationship("Task", back_populates="member", cascade="all, delete-orphan")
    __table_args__ = (UniqueConstraint('name', 'group_id', name='_member_name_group_uc'),)
    def __repr__(self):
        return f"<Member(id={self.id}, name='{self.name}', group_id='{self.group_id}')>"

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    member_id = Column(Integer, ForeignKey("members.id"), nullable=False)
    content = Column(Text, nullable=False)
    status = Column(String, default='pending', index=True)
    due_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    priority = Column(String, default='normal', index=True)
    is_recurring = Column(Boolean, default=False)
    recurrence_pattern = Column(String, nullable=True)
    recurrence_count = Column(Integer, default=0)
    parent_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    member = relationship("Member", back_populates="tasks")
    child_tasks = relationship("Task", backref="parent_task", remote_side=[id])
    def __repr__(self):
        return f"<Task(id={self.id}, content='{self.content[:20]}...', status='{self.status}', priority='{self.priority}')>"

def init_db():
    print("初始化資料庫，嘗試建立表格...")
    try:
        Base.metadata.create_all(bind=engine)
        print("表格建立完成 (如果原本不存在的話)。")
    except Exception as e:
        print(f"初始化資料庫時發生錯誤: {e}")

# --- CRUD Helper Functions with Corrected Type Hints ---

# --- Type Hint Fix 2: Use Session, Optional, List ---
def get_member_by_name_and_group(db: Session, name: str, group_id: str) -> Optional[Member]:
    return db.query(Member).filter(Member.name == name, Member.group_id == group_id).first()

def get_member_by_id(db: Session, member_id: int) -> Optional[Member]:
    return db.query(Member).filter(Member.id == member_id).first()

def get_task_by_id(db: Session, task_id: int) -> Optional[Task]:
    return db.query(Task).filter(Task.id == task_id).first()

def get_pending_tasks_by_member_id(db: Session, member_id: int) -> List[Task]:
    return db.query(Task).filter(Task.member_id == member_id, Task.status == 'pending').order_by(Task.due_date.asc().nulls_last(), Task.created_at.asc()).all()

def get_pending_tasks_by_group_id(db: Session, group_id: str) -> List[Task]:
    return db.query(Task).join(Member).filter(Member.group_id == group_id, Task.status == 'pending').order_by(Task.due_date.asc().nulls_last(), Task.created_at.asc()).all()

def create_member(db: Session, name: str, group_id: str, line_user_id: Optional[str] = None) -> Member:
    db_member = Member(name=name, group_id=group_id, line_user_id=line_user_id)
    db.add(db_member)
    db.commit()
    db.refresh(db_member)
    return db_member

def create_task(db: Session, member_id: int, content: str, due_date: Optional[datetime] = None, priority: str = "normal") -> Task:
    db_task = Task(member_id=member_id, content=content, status='pending', due_date=due_date, priority=priority)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task
# --- End Fix 2 ---
