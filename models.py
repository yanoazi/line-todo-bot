# models.py (SQLAlchemy Version for PostgreSQL - with Type Hint Fixes)
import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey, UniqueConstraint
)
# --- Type Hint Fix 1: Import Session from sqlalchemy.orm and typing helpers ---
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session
from typing import Optional, List
# --- End Fix 1 ---
from sqlalchemy.sql import func
from contextlib import contextmanager
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.environ.get('DATABASE_URL')

if not DATABASE_URL:
    raise ValueError("環境變數 DATABASE_URL 未設定！")

engine = create_engine(DATABASE_URL)
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
    member = relationship("Member", back_populates="tasks")
    def __repr__(self):
        return f"<Task(id={self.id}, content='{self.content[:20]}...', status='{self.status}')>"

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

def create_task(db: Session, member_id: int, content: str, due_date: Optional[datetime] = None) -> Task:
    db_task = Task(member_id=member_id, content=content, status='pending', due_date=due_date)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task
# --- End Fix 2 ---