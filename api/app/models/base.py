from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass



def utcnow() -> datetime:
    return datetime.now(UTC)
