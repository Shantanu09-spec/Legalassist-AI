import os
from datetime import datetime, timedelta, timezone

import pytest

# api.auth -> api.config loads settings at import-time. Ensure required env vars exist first.
os.environ["JWT_SECRET_KEY"] = "test-secret-key-value-12345"
os.environ["APP_ALLOWED_HOSTS"] = "localhost"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.routes.cases as cases_route
from api.auth import CurrentUser, get_current_user
from database import Base, Case, CaseStatus, CaseTimeline


@pytest.fixture()
def test_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Deduplicate indexes in metadata to prevent sqlite3 index already exists errors
    for table in Base.metadata.tables.values():
        seen_names = set()
        keep = set()
        for idx in list(table.indexes):
            if idx.name not in seen_names:
                seen_names.add(idx.name)
                keep.add(idx)
        table.indexes = keep

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        bind=engine,
    )
    db = SessionLocal()
    yield db
    db.close()


@pytest.fixture()
def client(test_db):
    app = FastAPI()
    app.include_router(cases_route.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser("42", "tester@example.com", "user")
    app.dependency_overrides[cases_route.get_db] = lambda: test_db
    return TestClient(app)


def _seed_case_with_timeline(test_db, user_id: int = 42):
    created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    case = Case(
        user_id=user_id,
        case_number="2023-CV-00001",
        case_type="civil",
        jurisdiction="Delhi",
        status=CaseStatus.CLOSED,
        title="Example Case",
        created_at=created_at,
        updated_at=created_at + timedelta(days=365),
    )
    test_db.add(case)
    test_db.commit()
    test_db.refresh(case)

    test_db.add_all(
        [
            CaseTimeline(
                case_id=case.id,
                event_type="filing",
                event_date=created_at,
                description="Case filed",
                event_metadata={
                    "court": "District Court",
                    "location": "New York, NY",
                    "documents": ["complaint.pdf"],
                },
            ),
            CaseTimeline(
                case_id=case.id,
                event_type="hearing",
                event_date=created_at + timedelta(days=30),
                description="Initial hearing",
                event_metadata={
                    "court": "District Court",
                    "judge": "Judge Smith",
                    "location": "New York, NY",
                },
            ),
            CaseTimeline(
                case_id=case.id,
                event_type="decision",
                event_date=created_at + timedelta(days=365),
                description="Court decision rendered",
                event_metadata={
                    "court": "District Court",
                    "judge": "Judge Smith",
                    "location": "New York, NY",
                    "documents": ["decision.pdf"],
                },
            ),
        ]
    )
    test_db.commit()

    return case


def test_case_timeline_response_matches_model(client, test_db):
    case = _seed_case_with_timeline(test_db)

    response = client.get(f"/api/v1/cases/{case.id}/timeline")

    assert response.status_code == 200
    payload = response.json()

    assert payload["case_id"] == str(case.id)
    assert payload["case_number"] == case.case_number
    assert payload["title"] == "Example Case"
    assert payload["status"] == "closed"
    assert payload["total_events"] == 3
    assert payload["duration_years"] == 1.0
    assert len(payload["events"]) == 3

    filing = next(event for event in payload["events"] if event["event_type"] == "filing")
    hearing = next(event for event in payload["events"] if event["event_type"] == "hearing")
    decision = next(event for event in payload["events"] if event["event_type"] == "decision")

    assert filing["court"] == "District Court"
    assert filing["location"] == "New York, NY"
    assert filing["documents"] == ["complaint.pdf"]
    assert hearing["judge"] == "Judge Smith"
    assert decision["documents"] == ["decision.pdf"]


def test_case_timeline_forbidden_for_other_user(client, test_db):
    case = _seed_case_with_timeline(test_db, user_id=99)

    response = client.get(f"/api/v1/cases/{case.id}/timeline")

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden: You do not own this case"


def _seed_case_with_pii_timeline(test_db, user_id: int = 42):
    created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    case = Case(
        user_id=user_id,
        case_number="CASE-PII-9999",
        case_type="civil",
        jurisdiction="Delhi",
        status=CaseStatus.ACTIVE,
        title="John Doe v. Acme Corp",
        created_at=created_at,
        updated_at=created_at + timedelta(days=10),
    )
    test_db.add(case)
    test_db.commit()
    test_db.refresh(case)

    test_db.add(
        CaseTimeline(
            case_id=case.id,
            event_type="filing",
            event_date=created_at,
            description="Filing by john.doe@example.com, contact +1 555 0199.",
            event_metadata={
                "court": "High Court (john.doe@example.com)",
                "judge": "Judge Smith +15550199",
                "location": "Room 404 (john.doe@example.com / +1-555-0199)",
                "documents": ["complaint_john.doe@example.com.pdf"],
            },
        )
    )
    test_db.commit()
    return case


def test_export_timeline_json_success(client, test_db):
    case = _seed_case_with_pii_timeline(test_db)

    response = client.get(f"/api/v1/cases/{case.id}/timeline/export?format=json")

    assert response.status_code == 200
    payload = response.json()

    assert payload["case_id"] == str(case.id)
    assert len(payload["events"]) == 1

    event = payload["events"][0]
    # Check that description and metadata are redacted
    assert "john.doe@example.com" not in event["description"]
    assert "+1 555 0199" not in event["description"]
    assert "[redacted-email]" in event["description"]
    assert "[redacted-phone]" in event["description"]

    assert "john.doe@example.com" not in event["court"]
    assert "[redacted-email]" in event["court"]

    assert "+15550199" not in event["judge"]
    assert "[redacted-phone]" in event["judge"]

    assert "john.doe@example.com" not in event["location"]
    assert "+1-555-0199" not in event["location"]
    assert "[redacted-email]" in event["location"]
    assert "[redacted-phone]" in event["location"]

    assert "john.doe@example.com" not in event["documents"][0]
    assert "[redacted-email]" in event["documents"][0]


def test_export_timeline_csv_success(client, test_db):
    case = _seed_case_with_pii_timeline(test_db)

    response = client.get(f"/api/v1/cases/{case.id}/timeline/export?format=csv")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert f'attachment; filename="case_{case.id}_timeline.csv"' in response.headers["content-disposition"]

    import csv
    import io
    content = response.content.decode("utf-8")
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)

    # Header check
    assert rows[0] == ["Date", "Event Type", "Description", "Court", "Judge", "Location", "Documents"]
    assert len(rows) == 2

    # Data row check
    data_row = rows[1]
    assert data_row[1] == "filing"
    # description
    assert "john.doe@example.com" not in data_row[2]
    assert "+1 555 0199" not in data_row[2]
    assert "[redacted-email]" in data_row[2]
    assert "[redacted-phone]" in data_row[2]
    # court
    assert "john.doe@example.com" not in data_row[3]
    assert "[redacted-email]" in data_row[3]
    # judge
    assert "+15550199" not in data_row[4]
    assert "[redacted-phone]" in data_row[4]
    # location
    assert "john.doe@example.com" not in data_row[5]
    assert "+1-555-0199" not in data_row[5]
    # documents
    assert "john.doe@example.com" not in data_row[6]
    assert "[redacted-email]" in data_row[6]


def test_export_timeline_forbidden(client, test_db):
    case = _seed_case_with_pii_timeline(test_db, user_id=99)

    response = client.get(f"/api/v1/cases/{case.id}/timeline/export?format=json")

    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden: You do not own this case"


def test_export_timeline_invalid_format(client, test_db):
    case = _seed_case_with_pii_timeline(test_db)

    response = client.get(f"/api/v1/cases/{case.id}/timeline/export?format=xml")

    assert response.status_code == 422