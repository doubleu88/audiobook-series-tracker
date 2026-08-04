import datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


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
