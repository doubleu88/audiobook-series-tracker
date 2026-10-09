import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Book, BookEdition, Series, User, UserBookStatus
from app.scheduler import update_series_from_scraped
from app.scraper import (
    ScrapedBook,
    ScrapedEdition,
    ScrapedSeries,
    SeriesPageError,
    _classify_edition,
    _is_us_edition,
    fetch_series_via_api,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_scenario_1_single_edition_ranks_ahead_of_box_set():
    """Scenario 1: A legitimate non-US single edition should outrank a US box set for an individual slot."""
    uk_single = (
        {"asin": "UK03", "sequence": "3"},
        {
            "asin": "UK03",
            "title": "Book 3 (UK Edition)",
            "format_type": "unabridged",
            "distribution_rights": {"distribution_rights_region": "GB"},
        },
    )
    us_box_set = (
        {"asin": "BOX123", "sequence": "1-3"},
        {
            "asin": "BOX123",
            "title": "Books 1-3 Box Set",
            "format_type": "unabridged",
            "distribution_rights": {"distribution_rights_region": "US"},
        },
    )

    def _rank(item):
        rel, prod = item
        sku = prod.get("sku") or rel.get("sku") or ""
        is_placeholder = sku.startswith("PL_HLDR") or prod.get("release_date") == "2200-01-01"
        f = _classify_edition(prod)
        return (
            is_placeholder,
            f == "box_set",
            not _is_us_edition(prod),
            f in ("dramatized", "booktrack", "abridged"),
            rel["asin"],
        )

    candidates = [us_box_set, uk_single]
    candidates.sort(key=_rank)
    assert candidates[0][0]["asin"] == "UK03"
    assert candidates[1][0]["asin"] == "BOX123"


def test_scenario_2_failed_product_chunk_raises_series_page_error():
    """Scenario 2: A failed product-chunk request must raise SeriesPageError rather than proceeding with partial data."""
    mock_series_resp = MagicMock()
    mock_series_resp.status_code = 200
    mock_series_resp.json.return_value = {
        "product": {
            "title": "Test Series",
            "relationships": [
                {"relationship_type": "series", "asin": "B01", "sequence": "1"},
                {"relationship_type": "series", "asin": "B02", "sequence": "2"},
            ],
        }
    }

    mock_chunk_resp = MagicMock()
    mock_chunk_resp.status_code = 503

    with patch("httpx.Client") as mock_client_cls, patch("time.sleep"):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.side_effect = [mock_series_resp, mock_chunk_resp, mock_chunk_resp, mock_chunk_resp]

        with pytest.raises(SeriesPageError, match="Failed to fetch product chunk"):
            fetch_series_via_api("SERIES_ASIN", "http://example.com")


def test_scenario_3_duplicate_merge_preserves_status_and_reparents_editions(db_session):
    """Scenario 3: Merging a duplicate book row carries over requested_at/checked_at/last_error and re-parents editions."""
    user = User(username="testuser", password_hash="hash")
    db_session.add(user)
    db_session.commit()

    series = Series(name="Test Series", url="http://example.com", asin="SERIES1")
    db_session.add(series)
    db_session.commit()

    # Slot book that matches position 1
    target_book = Book(series_id=series.id, asin="US01", title="Book 1", position=1.0, url="http://example.com/1")
    # Stale duplicate book
    stale_book = Book(series_id=series.id, asin="OLD01", title="Book 1 (Old Duplicate)", position=1.0, url="http://example.com/1old")
    db_session.add_all([target_book, stale_book])
    db_session.commit()

    # Add alternate edition to stale book
    stale_edition = BookEdition(book_id=stale_book.id, asin="ALT01", title="Alt Edition", format_type="dramatized")
    db_session.add(stale_edition)

    # Add user status to stale book with requested_at, checked_at, last_error
    now = datetime.datetime.utcnow()
    stale_status = UserBookStatus(
        user_id=user.id,
        book_id=stale_book.id,
        in_library=True,
        matched_asin="OLD01",
        requested_at=now,
        checked_at=now,
        last_error="Download error",
        acknowledged=True,
        acknowledged_at=now,
    )
    db_session.add(stale_status)
    db_session.commit()

    scraped = ScrapedSeries(
        name="Test Series",
        asin="SERIES1",
        url="http://example.com",
        books=[
            ScrapedBook(
                asin="US01",
                title="Book 1",
                position=1.0,
                release_date=datetime.date(2025, 1, 1),
                url="http://example.com/1",
                image_url=None,
                editions=[ScrapedEdition(asin="US01", title="Book 1", is_primary=True)],
            )
        ],
    )

    update_series_from_scraped(db_session, series, scraped)

    # Verify stale_book was merged and deleted
    assert db_session.get(Book, stale_book.id) is None

    # Verify target_book now has user status with all fields intact
    status = db_session.query(UserBookStatus).filter_by(user_id=user.id, book_id=target_book.id).first()
    assert status is not None
    assert status.in_library is True
    assert status.matched_asin == "OLD01"
    assert status.requested_at == now
    assert status.checked_at == now
    assert status.last_error == "Download error"
    assert status.acknowledged is True
    assert status.acknowledged_at == now

    # Verify alternate edition was re-parented to target_book
    reparented_ed = db_session.query(BookEdition).filter_by(asin="ALT01").first()
    assert reparented_ed is not None
    assert reparented_ed.book_id == target_book.id


def test_scenario_4_temporarily_missing_book_is_not_deleted(db_session):
    """Scenario 4: A book temporarily missing from one API refresh response is NOT deleted, preserving user state."""
    user = User(username="testuser", password_hash="hash")
    db_session.add(user)
    db_session.commit()

    series = Series(name="Test Series", url="http://example.com", asin="SERIES1")
    db_session.add(series)
    db_session.commit()

    book1 = Book(series_id=series.id, asin="B01", title="Book 1", position=1.0, url="http://example.com/1")
    book2 = Book(series_id=series.id, asin="B02", title="Book 2", position=2.0, url="http://example.com/2")
    db_session.add_all([book1, book2])
    db_session.commit()

    # User has book 2 in library
    now = datetime.datetime.utcnow()
    status2 = UserBookStatus(
        user_id=user.id,
        book_id=book2.id,
        in_library=True,
        matched_asin="B02",
        checked_at=now,
    )
    db_session.add(status2)
    db_session.commit()

    # Scrape returns only Book 1 (Book 2 is temporarily missing from API response)
    scraped = ScrapedSeries(
        name="Test Series",
        asin="SERIES1",
        url="http://example.com",
        books=[
            ScrapedBook(
                asin="B01",
                title="Book 1",
                position=1.0,
                release_date=datetime.date(2025, 1, 1),
                url="http://example.com/1",
                image_url=None,
                editions=[ScrapedEdition(asin="B01", title="Book 1", is_primary=True)],
            )
        ],
    )

    update_series_from_scraped(db_session, series, scraped)

    # Book 2 must still exist!
    assert db_session.get(Book, book2.id) is not None
    # UserBookStatus for Book 2 must still exist!
    st = db_session.query(UserBookStatus).filter_by(user_id=user.id, book_id=book2.id).first()
    assert st is not None
    assert st.in_library is True
    assert st.matched_asin == "B02"


def test_prowlarr_download_form_custom_query():
    """Prowlarr download form searches custom query if provided, else book title."""
    from app.main import download_book_form

    mock_request = MagicMock()
    user = User(id=1, username="testuser", prowlarr_base_url="http://prowlarr:9696", prowlarr_api_key="key")
    book = Book(id=1, series_id=1, asin="B01", title="Canonical Title", url="http://example.com/1")

    with patch("app.main.get_session") as mock_get_session, \
         patch("app.main._require_subscription_for_book", return_value=book), \
         patch("app.main.ProwlarrClient") as mock_prowlarr_cls, \
         patch("app.main.templates.TemplateResponse"):

        mock_session = MagicMock()
        mock_get_session.return_value = mock_session
        mock_session.get.return_value = user

        mock_client = MagicMock()
        mock_prowlarr_cls.return_value = mock_client
        mock_client.search.return_value = []

        # When custom query is passed
        download_book_form(mock_request, book_id=1, query="GraphicAudio Edition", user=user)
        mock_client.search.assert_called_with("GraphicAudio Edition")

        # When query is None or empty, defaults to book.title
        download_book_form(mock_request, book_id=1, query=None, user=user)
        mock_client.search.assert_called_with("Canonical Title")

def test_release_date_announcement_not_repeated_when_date_flaps(db_session):
    """A date that disappears and reappears between scrapes must be announced only once."""
    series = Series(
        name="Test Series", url="http://example.com", asin="SERIES1",
        last_checked=datetime.datetime.utcnow(),
    )
    db_session.add(series)
    db_session.commit()
    book = Book(series_id=series.id, asin="B01", title="Book 1", position=1.0, url="http://example.com/1")
    db_session.add(book)
    db_session.commit()

    def scrape(release_date):
        return ScrapedSeries(
            name="Test Series", asin="SERIES1", url="http://example.com",
            books=[ScrapedBook(
                asin="B01", title="Book 1", position=1.0, release_date=release_date,
                url="http://example.com/1", image_url=None,
                editions=[ScrapedEdition(asin="B01", title="Book 1", is_primary=True)],
            )],
        )

    future = datetime.date.today() + datetime.timedelta(days=30)
    with patch("app.scheduler._push_to_series_subscribers") as push:
        update_series_from_scraped(db_session, series, scrape(future))
        update_series_from_scraped(db_session, series, scrape(None))
        update_series_from_scraped(db_session, series, scrape(future))
    assert push.call_count == 1

def test_cascade_delete_books_removes_user_book_status(db_session):
    """Deleting a series or book cascades and removes associated UserBookStatus records."""
    user = User(username="testuser", password_hash="hash")
    db_session.add(user)
    db_session.commit()

    series = Series(name="Cascade Series", url="http://example.com", asin="CASC01")
    db_session.add(series)
    db_session.commit()

    book = Book(series_id=series.id, asin="B01", title="Book 1", position=1.0, url="http://example.com/1")
    db_session.add(book)
    db_session.commit()

    status = UserBookStatus(user_id=user.id, book_id=book.id, in_library=True, matched_asin="B01")
    db_session.add(status)
    db_session.commit()

    status_id = status.id
    # Delete series, which should cascade to books, which should cascade to statuses
    db_session.delete(series)
    db_session.commit()

    assert db_session.get(Book, book.id) is None
    assert db_session.get(UserBookStatus, status_id) is None


def test_scan_detects_and_resolves_asin_mismatch(db_session):
    """Library scan detects if a status record has a mismatched alien ASIN and re-evaluates it."""
    from app.models import Subscription
    from app.scheduler import run_scan_for_user

    user = User(
        username="scanuser",
        password_hash="hash",
        abs_base_url="http://abs:80",
        abs_api_key="key",
        abs_library_id="lib1",
    )
    db_session.add(user)
    db_session.commit()

    series = Series(name="Mismatch Series", url="http://example.com", asin="MISMATCH01")
    db_session.add(series)
    db_session.commit()

    sub = Subscription(user_id=user.id, series_id=series.id)
    db_session.add(sub)
    db_session.commit()

    book1 = Book(
        series_id=series.id,
        asin="REAL01",
        title="Book 1",
        position=1.0,
        release_date=datetime.date(2025, 1, 1),
        url="http://example.com/1",
    )
    book2 = Book(
        series_id=series.id,
        asin="REAL02",
        title="Book 2",
        position=2.0,
        release_date=datetime.date(2025, 1, 1),
        url="http://example.com/2",
    )
    db_session.add_all([book1, book2])
    db_session.commit()

    b1_id = book1.id
    b2_id = book2.id

    # Corrupt both statuses with alien ASINs from recycled IDs
    st1 = UserBookStatus(user_id=user.id, book_id=b1_id, in_library=True, matched_asin="ALIEN_A")
    st2 = UserBookStatus(user_id=user.id, book_id=b2_id, in_library=True, matched_asin="ALIEN_B")
    db_session.add_all([st1, st2])
    db_session.commit()

    # Suppose ABS library only actually has REAL01 (REAL02 is missing)
    mock_abs = MagicMock()
    mock_abs.list_asins_in_library.return_value = {"REAL01"}

    with patch("app.scheduler.get_session", return_value=db_session), \
         patch("app.scheduler.ABSClient", return_value=mock_abs):
        run_scan_for_user(user.id)

    # st1 should have detected the mismatch with ALIEN_A, but found REAL01 in ABS -> in_library=True, matched_asin=REAL01
    res1 = db_session.query(UserBookStatus).filter_by(book_id=b1_id, user_id=user.id).first()
    assert res1.in_library is True
    assert res1.matched_asin == "REAL01"

    # st2 should have detected the mismatch with ALIEN_B, and since REAL02 is NOT in ABS -> in_library=False, matched_asin=None
    res2 = db_session.query(UserBookStatus).filter_by(book_id=b2_id, user_id=user.id).first()
    assert res2.in_library is False
    assert res2.matched_asin is None


def test_reconcile_cached_asins_detects_and_resolves_mismatch(db_session):
    """reconcile_series_with_cached_asins fixes alien ASIN matches against cached library ASINs."""
    from app.scheduler import reconcile_series_with_cached_asins

    user = User(username="recuser", password_hash="hash")
    db_session.add(user)
    db_session.commit()

    series = Series(name="Rec Series", url="http://example.com", asin="REC01")
    db_session.add(series)
    db_session.commit()

    book1 = Book(series_id=series.id, asin="REAL01", title="Book 1", position=1.0, url="http://example.com/1")
    book2 = Book(series_id=series.id, asin="REAL02", title="Book 2", position=2.0, url="http://example.com/2")
    db_session.add_all([book1, book2])
    db_session.commit()

    # Corrupt both statuses with alien ASINs
    st1 = UserBookStatus(user_id=user.id, book_id=book1.id, in_library=True, matched_asin="ALIEN_A")
    st2 = UserBookStatus(user_id=user.id, book_id=book2.id, in_library=True, matched_asin="ALIEN_B")
    db_session.add_all([st1, st2])
    db_session.commit()

    with patch("app.scheduler.get_cached_abs_asins", return_value={"REAL01"}):
        updated = reconcile_series_with_cached_asins(db_session, user.id, series)

    assert updated == 2
    db_session.refresh(st1)
    db_session.refresh(st2)
    assert st1.in_library is True
    assert st1.matched_asin == "REAL01"
    assert st2.in_library is False
    assert st2.matched_asin is None


def test_db_migration_cleans_orphaned_and_mismatched_statuses():
    """_migrate removes orphaned user_book_status rows and resets mismatched ASIN statuses."""
    from app.db import _migrate
    from sqlalchemy import text

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)
    session = Session()

    user = User(username="miguser", password_hash="hash")
    session.add(user)
    session.commit()

    series = Series(name="Mig Series", url="http://example.com", asin="MIG01")
    session.add(series)
    session.commit()

    book = Book(series_id=series.id, asin="VALID01", title="Valid Book", position=1.0, url="http://example.com/1")
    book_mismatched = Book(series_id=series.id, asin="REAL_BOOK_2", title="Book 2", position=2.0, url="http://example.com/2")
    book_predated = Book(
        series_id=series.id,
        asin="REAL_BOOK_3",
        title="Book 3",
        position=3.0,
        url="http://example.com/3",
        created_at=datetime.datetime(2026, 9, 30, 12, 0, 0),
    )
    book_case = Book(series_id=series.id, asin="lower_asin", title="Book 4", position=4.0, url="http://example.com/4")
    session.add_all([book, book_mismatched, book_predated, book_case])
    session.commit()
    b_valid_id = book.id
    b_mismatched_id = book_mismatched.id
    b_predated_id = book_predated.id
    b_case_id = book_case.id

    # 1. Legitimate status
    valid_st = UserBookStatus(user_id=user.id, book_id=b_valid_id, in_library=True, matched_asin="VALID01")
    # 2. Mismatched status pointing to real book but with alien ASIN
    mismatched_st = UserBookStatus(user_id=user.id, book_id=b_mismatched_id, in_library=True, matched_asin="BOGUS_ALIEN")
    # 3. Predated status with acknowledged/requested timestamps predating book.created_at (e.g. Soccer Supremo bug)
    predated_st = UserBookStatus(
        user_id=user.id,
        book_id=b_predated_id,
        in_library=True,
        matched_asin="REAL_BOOK_3",
        acknowledged=True,
        acknowledged_at=datetime.datetime(2026, 9, 18, 12, 0, 0),
        requested_at=datetime.datetime(2026, 9, 18, 12, 0, 0),
        last_error="Old error",
    )
    # 4. Status with matched_asin matching book.asin case-insensitively
    case_st = UserBookStatus(user_id=user.id, book_id=b_case_id, in_library=True, matched_asin="LOWER_ASIN")
    session.add_all([valid_st, mismatched_st, predated_st, case_st])
    session.commit()

    # 5. Create an orphaned status manually via raw SQL (disabling foreign keys temporarily to simulate legacy DB)
    session.execute(text("PRAGMA foreign_keys = OFF"))
    session.execute(text("INSERT INTO user_book_status (user_id, book_id, in_library, matched_asin, acknowledged) VALUES (1, 99999, 1, 'GHOST_ASIN', 0)"))
    session.commit()

    # Verify setup before migration
    assert session.execute(text("SELECT COUNT(*) FROM user_book_status WHERE book_id = 99999")).scalar() == 1
    session.close()

    with engine.begin() as conn:
        _migrate(conn)

    session = Session()
    # Orphan should be deleted
    assert session.execute(text("SELECT COUNT(*) FROM user_book_status WHERE book_id = 99999")).scalar() == 0

    # Mismatched status should have been reset
    db_mismatched = session.query(UserBookStatus).filter_by(book_id=b_mismatched_id).first()
    assert db_mismatched.in_library is False
    assert db_mismatched.matched_asin is None

    # Valid status should be completely untouched
    db_valid = session.query(UserBookStatus).filter_by(book_id=b_valid_id).first()
    assert db_valid.in_library is True
    assert db_valid.matched_asin == "VALID01"

    # Predated status should have acknowledged and requested timestamps reset
    db_predated = session.query(UserBookStatus).filter_by(book_id=b_predated_id).first()
    assert db_predated.acknowledged is False
    assert db_predated.acknowledged_at is None
    assert db_predated.requested_at is None
    assert db_predated.last_error is None
    assert db_predated.in_library is True
    assert db_predated.matched_asin == "REAL_BOOK_3"

    # Case-insensitive matched status should be preserved
    db_case = session.query(UserBookStatus).filter_by(book_id=b_case_id).first()
    assert db_case.in_library is True
    assert db_case.matched_asin == "LOWER_ASIN"
    session.close()
