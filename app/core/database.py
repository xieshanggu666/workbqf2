from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import DB_URL, DB_ECHO

# timeout：并发触发预报时，后到的写事务等待锁释放而非立即报 database is locked
engine = create_engine(DB_URL,
                       connect_args={"check_same_thread": False, "timeout": 30},
                       echo=DB_ECHO)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
