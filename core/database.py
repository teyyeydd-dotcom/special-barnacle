from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

Base = declarative_base()

class AssistantDB(Base):
    __tablename__ = 'assistants'
    id = Column(Integer, primary_key=True)
    session_string = Column(Text, unique=True)
    name = Column(String)
    user_id = Column(Integer)
    username = Column(String)
    added_at = Column(DateTime, default=datetime.now)

class BridgeDB(Base):
    __tablename__ = 'bridges'
    id = Column(Integer, primary_key=True)
    source_id = Column(Integer)
    target_id = Column(Integer)
    source_name = Column(String)
    target_name = Column(String)
    gain = Column(Float, default=100)
    echo = Column(Float, default=0)
    loudness = Column(Float, default=0)
    bass = Column(Float, default=0)
    started_at = Column(DateTime, default=datetime.now)
    ended_at = Column(DateTime, nullable=True)
    # Kis sudo user ke assistant ne ye /join actually run kiya — is column
    # ke bina livescreenshare ko restart ke baad pata hi nahi chalta ki
    # is route ke liye kaunsa assistant use karna hai, aur wo galat
    # (calling user ka apna) assistant use kar leta hai jo source group
    # ki VC mein joined hi nahi hota.
    owner_id = Column(Integer, nullable=True)

class ScreenshareDB(Base):
    __tablename__ = 'screenshares'
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer)
    quality = Column(String, default="720p")
    fps = Column(Integer, default=30)
    started_at = Column(DateTime, default=datetime.now)
    ended_at = Column(DateTime, nullable=True)

class DatabaseManager:
    def __init__(self, db_path: str = "sqlite:///vcfight.db"):
        self.db_path = db_path
        self.engine = create_engine(db_path, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self._migrate_add_missing_columns()
        self.Session = sessionmaker(bind=self.engine)
        logger.info("Database initialized")

    def _migrate_add_missing_columns(self):
        """create_all() sirf missing TABLES banata hai, existing table mein
        naya column apne aap add nahi karta. Isliye purani deployed vcfight.db
        (jisme owner_id column nahi hai) pe save_bridge() seedha crash karta
        ('no such column: owner_id'). Ye lightweight check-and-ALTER isse bachata
        hai — sirf tabhi chalta hai jab column sach mein missing ho, harmless
        no-op hai fresh DB pe."""
        try:
            with self.engine.connect() as conn:
                existing_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(bridges)").fetchall()}
                if existing_cols and "owner_id" not in existing_cols:
                    conn.exec_driver_sql("ALTER TABLE bridges ADD COLUMN owner_id INTEGER")
                    logger.info("[DB] migrated: bridges.owner_id column added")
        except Exception as e:
            logger.warning(f"[DB] migration check failed (non-fatal): {e}")
    
    def get_session(self):
        return self.Session()
    
    def save_assistant(self, session_string: str, name: str, user_id: int, username: str = None):
        session = self.get_session()
        try:
            existing = session.query(AssistantDB).filter_by(session_string=session_string).first()
            if not existing:
                assistant = AssistantDB(
                    session_string=session_string,
                    name=name,
                    user_id=user_id,
                    username=username
                )
                session.add(assistant)
                session.commit()
                logger.info(f"Assistant saved: {name}")
        except Exception as e:
            logger.error(f"Failed to save assistant: {e}")
            session.rollback()
        finally:
            session.close()
    
    def get_assistants(self):
        session = self.get_session()
        try:
            return session.query(AssistantDB).all()
        finally:
            session.close()
    
    def remove_assistant(self, session_string: str):
        session = self.get_session()
        try:
            session.query(AssistantDB).filter_by(session_string=session_string).delete()
            session.commit()
        except Exception as e:
            logger.error(f"Failed to remove assistant: {e}")
            session.rollback()
        finally:
            session.close()
    
    def save_bridge(self, source_id: int, target_id: int, source_name: str, target_name: str,
                    gain: float, echo: float, loudness: float, bass: float, owner_id: int = None):
        session = self.get_session()
        try:
            bridge = BridgeDB(
                source_id=source_id,
                target_id=target_id,
                source_name=source_name,
                target_name=target_name,
                gain=gain,
                echo=echo,
                loudness=loudness,
                bass=bass,
                owner_id=owner_id
            )
            session.add(bridge)
            session.commit()
        except Exception as e:
            logger.error(f"Failed to save bridge: {e}")
            session.rollback()
        finally:
            session.close()
    
    def close_bridge(self, source_id: int, target_id: int):
        session = self.get_session()
        try:
            bridge = session.query(BridgeDB).filter_by(
                source_id=source_id, target_id=target_id, ended_at=None
            ).first()
            if bridge:
                bridge.ended_at = datetime.now()
                session.commit()
        except Exception as e:
            logger.error(f"Failed to close bridge: {e}")
            session.rollback()
        finally:
            session.close()
    
    def get_bridge_history(self, limit: int = 10):
        session = self.get_session()
        try:
            return session.query(BridgeDB).order_by(BridgeDB.started_at.desc()).limit(limit).all()
        finally:
            session.close()
