import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import current_app, g


SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    property_type TEXT NOT NULL,
    purpose TEXT NOT NULL,
    location TEXT NOT NULL,
    size REAL NOT NULL CHECK (size > 0),
    size_unit TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    price_pkr INTEGER,
    price_review_required INTEGER NOT NULL DEFAULT 0 CHECK (price_review_required IN (0, 1)),
    status TEXT NOT NULL,
    bedrooms INTEGER CHECK (bedrooms IS NULL OR bedrooms >= 0),
    bathrooms INTEGER CHECK (bathrooms IS NULL OR bathrooms >= 0),
    description TEXT NOT NULL DEFAULT '',
    owner_name TEXT NOT NULL DEFAULT '',
    owner_phone TEXT NOT NULL DEFAULT '',
    image_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_properties_status ON properties(status);
CREATE INDEX IF NOT EXISTS idx_properties_type ON properties(property_type);
CREATE INDEX IF NOT EXISTS idx_properties_location ON properties(location);
CREATE INDEX IF NOT EXISTS idx_properties_price ON properties(price);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    client_type TEXT NOT NULL,
    budget_min REAL CHECK (budget_min IS NULL OR budget_min > 0),
    budget_max REAL CHECK (budget_max IS NULL OR budget_max > 0),
    preferred_location TEXT NOT NULL DEFAULT '',
    preferred_property_type TEXT,
    preferred_purpose TEXT,
    required_size REAL CHECK (required_size IS NULL OR required_size > 0),
    required_size_unit TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_clients_name ON clients(full_name);
CREATE INDEX IF NOT EXISTS idx_clients_phone ON clients(phone);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    property_id INTEGER REFERENCES properties(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    next_follow_up TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_leads_client ON leads(client_id);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_follow_up ON leads(next_follow_up);

CREATE TABLE IF NOT EXISTS property_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    image_path TEXT NOT NULL UNIQUE,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_property_images_property ON property_images(property_id);
"""


def get_db():
    if "db" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE"])
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


def close_db(_error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db():
    db = get_db()
    backup_legacy_database()
    db.executescript(SCHEMA)
    db.execute("BEGIN IMMEDIATE")

    property_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(properties)")
    }
    if "price_pkr" not in property_columns:
        db.execute("ALTER TABLE properties ADD COLUMN price_pkr INTEGER")
    if "price_review_required" not in property_columns:
        db.execute(
            """ALTER TABLE properties ADD COLUMN price_review_required INTEGER
               NOT NULL DEFAULT 0 CHECK (price_review_required IN (0, 1))"""
        )
        db.execute(
            """UPDATE properties SET price_review_required = 1
               WHERE price != CAST(price AS INTEGER)"""
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_properties_price_pkr ON properties(price_pkr)"
    )
    db.execute(
        """UPDATE properties SET price_pkr = CAST(ROUND(price) AS INTEGER)
           WHERE price_pkr IS NULL"""
    )
    legacy_images = db.execute(
        """SELECT id, image_path FROM properties
           WHERE image_path IS NOT NULL AND image_path != ''"""
    ).fetchall()
    static_root = (Path(current_app.root_path) / "static").resolve()
    for property_row in legacy_images:
        image_path = Path(property_row["image_path"])
        candidate = (static_root / image_path).resolve()
        if static_root not in candidate.parents or not candidate.is_file():
            continue
        db.execute(
            """INSERT OR IGNORE INTO property_images
               (property_id, image_path, is_primary) VALUES (?, ?, 1)""",
            (property_row["id"], property_row["image_path"]),
        )
    db.commit()


def backup_legacy_database():
    database_path = Path(current_app.config["DATABASE"])
    if not database_path.is_file() or database_path.stat().st_size == 0:
        return

    inspection = sqlite3.connect(database_path)
    try:
        property_table = inspection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'properties'"
        ).fetchone()
        if not property_table:
            return
        columns = {
            row[1] for row in inspection.execute("PRAGMA table_info(properties)")
        }
        image_table = inspection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'property_images'"
        ).fetchone()
    finally:
        inspection.close()
    if "price_pkr" in columns and "price_review_required" in columns and image_table:
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = database_path.with_name(f"{database_path.name}.backup-{timestamp}")
    source = sqlite3.connect(database_path)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    except sqlite3.Error:
        destination.close()
        source.close()
        backup_path.unlink(missing_ok=True)
        raise
    destination.close()
    source.close()
