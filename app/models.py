import datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asin: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    ended: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

    books: Mapped[list["Book"]] = relationship(
        back_populates="series", cascade="all, delete-orphan", order_by="Book.position"
    )
    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "series_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"), index=True)

    user: Mapped["User"] = relationship()
    series: Mapped["Series"] = relationship(back_populates="subscriptions")


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    endpoint: Mapped[str] = mapped_column(String, unique=True)
    p256dh: Mapped[str] = mapped_column(String)
    auth: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    user: Mapped["User"] = relationship()


class Book(Base):
    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"))
    asin: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    position: Mapped[float | None] = mapped_column(nullable=True)
    release_date: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    url: Mapped[str] = mapped_column(String)
    cover_image: Mapped[str | None] = mapped_column(String, nullable=True)

    series: Mapped["Series"] = relationship(back_populates="books")

    @property
    def released(self) -> bool:
        return self.release_date is not None and self.release_date <= datetime.date.today()
