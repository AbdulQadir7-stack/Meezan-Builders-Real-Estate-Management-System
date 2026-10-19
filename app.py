import io
import logging
import math
import os
import sqlite3
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_wtf.csrf import CSRFProtect
from PIL import Image, UnidentifiedImageError

from database import close_db, get_db, init_db

PROPERTY_TYPES = ("plot", "house", "apartment", "commercial", "other")
PURPOSES = ("sale", "rent")
PROPERTY_STATUSES = ("available", "reserved", "sold", "rented")
SIZE_UNITS = ("marla", "kanal", "sq ft")
CLIENT_TYPES = ("buyer", "seller", "tenant", "landlord", "investor")
LEAD_SOURCES = ("phone", "WhatsApp", "website", "referral", "walk-in")
LEAD_STATUSES = ("new", "contacted", "interested", "negotiation", "converted", "lost")
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
PRICE_UNITS = {
    "PKR": Decimal("1"),
    "Thousand": Decimal("1000"),
    "Lakh": Decimal("100000"),
    "Million": Decimal("1000000"),
    "Crore": Decimal("10000000"),
    "Billion": Decimal("1000000000"),
}
IMAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or uuid.uuid4().hex,
        DATABASE=os.environ.get(
            "DATABASE", str(Path(app.root_path) / "data" / "real_estate.db")
        ),
        MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_TOTAL_BYTES", 25 * 1024 * 1024)),
        MAX_IMAGE_BYTES=int(os.environ.get("MAX_IMAGE_BYTES", 5 * 1024 * 1024)),
        MAX_IMAGE_PIXELS=25_000_000,
        UPLOAD_FOLDER=str(Path(app.root_path) / "static" / "uploads"),
    )
    if test_config:
        app.config.update(test_config)
    if app.config["MAX_IMAGE_BYTES"] <= 0 or app.config["MAX_CONTENT_LENGTH"] <= 0:
        raise ValueError("Image and request size limits must be positive byte counts.")
    CSRFProtect(app)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()

    @app.template_filter("pkr")
    def format_pkr(value):
        if value is None:
            return "—"
        amount = int(value)
        return f"PKR {amount:,} ({format_south_asian(amount)})"

    @app.template_filter("property_price")
    def format_property_price(property_row):
        if property_row["price_review_required"]:
            return f"Review needed · legacy PKR {property_row['price']:,.2f}".rstrip("0").rstrip(".")
        return format_pkr(property_row["price_pkr"])

    @app.context_processor
    def navigation_choices():
        return {
            "property_types": PROPERTY_TYPES,
            "purposes": PURPOSES,
            "price_units": tuple(PRICE_UNITS),
        }

    @app.route("/")
    def home():
        db = get_db()
        stats = {
            "properties": db.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
            "available": db.execute(
                "SELECT COUNT(*) FROM properties WHERE status = 'available'"
            ).fetchone()[0],
            "reserved": db.execute(
                "SELECT COUNT(*) FROM properties WHERE status = 'reserved'"
            ).fetchone()[0],
            "sold": db.execute(
                "SELECT COUNT(*) FROM properties WHERE status = 'sold'"
            ).fetchone()[0],
            "rented": db.execute(
                "SELECT COUNT(*) FROM properties WHERE status = 'rented'"
            ).fetchone()[0],
            "clients": db.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
            "active_leads": db.execute(
                "SELECT COUNT(*) FROM leads WHERE status NOT IN ('converted', 'lost')"
            ).fetchone()[0],
            "followups_due": db.execute(
                """SELECT COUNT(*) FROM leads
                   WHERE next_follow_up IS NOT NULL
                   AND next_follow_up <= ? AND status NOT IN ('converted', 'lost')""",
                (date.today().isoformat(),),
            ).fetchone()[0],
        }
        recent_properties = [dict(row) for row in db.execute(
            "SELECT * FROM properties ORDER BY created_at DESC, id DESC LIMIT 5"
        ).fetchall()]
        attach_property_images(db, recent_properties)
        status_counts = db.execute(
            "SELECT status, COUNT(*) AS count FROM properties GROUP BY status"
        ).fetchall()
        due_followups = db.execute(
            """SELECT leads.*, clients.full_name AS client_name
               FROM leads JOIN clients ON clients.id = leads.client_id
               WHERE next_follow_up IS NOT NULL AND next_follow_up <= ?
               AND status NOT IN ('converted', 'lost')
               ORDER BY next_follow_up LIMIT 5""",
            (date.today().isoformat(),),
        ).fetchall()
        return render_template(
            "index.html", stats=stats, recent_properties=recent_properties,
            due_followups=due_followups, status_counts={r["status"]: r["count"] for r in status_counts},
        )

    @app.route("/properties")
    def properties():
        filters = {
            "q": request.args.get("q", "").strip(),
            "status": request.args.get("status", ""),
            "type": request.args.get("type", ""),
            "location": request.args.get("location", "").strip(),
            "min_price": request.args.get("min_price", "").strip(),
            "max_price": request.args.get("max_price", "").strip(),
        }
        where, params = [], []
        if filters["q"]:
            where.append("(title LIKE ? OR location LIKE ? OR CAST(id AS TEXT) LIKE ?)")
            search = f"%{filters['q']}%"
            params.extend((search, search, search))
        if filters["status"] in PROPERTY_STATUSES:
            where.append("status = ?")
            params.append(filters["status"])
        if filters["type"] in PROPERTY_TYPES:
            where.append("property_type = ?")
            params.append(filters["type"])
        if filters["location"]:
            where.append("location LIKE ?")
            params.append(f"%{filters['location']}%")
        for key, operator in (("min_price", ">="), ("max_price", "<=")):
            if filters[key]:
                try:
                    amount = float(filters[key])
                    if not math.isfinite(amount) or amount < 0:
                        raise ValueError
                except ValueError:
                    flash(f"{key.replace('_', ' ').title()} must be a valid non-negative amount.", "error")
                    filters[key] = ""
                else:
                    where.append(f"price_pkr {operator} ?")
                    params.append(amount)
        sql = "SELECT * FROM properties"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC"
        rows = [dict(row) for row in get_db().execute(sql, params).fetchall()]
        attach_property_images(get_db(), rows)
        return render_template("properties.html", properties=rows, filters=filters)

    @app.route("/properties/new", methods=["GET", "POST"])
    @app.route("/properties/<int:property_id>/edit", methods=["GET", "POST"])
    def property_form(property_id=None):
        db = get_db()
        existing = None
        existing_images = []
        if property_id is not None:
            existing = db.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
            if existing is None:
                abort(404)
            existing_images = get_property_images(db, property_id)

        if request.method == "POST":
            data, errors = validate_property(request.form)
            preserve_legacy_price = bool(
                existing
                and existing["price_review_required"]
                and price_matches_legacy_value(
                    request.form.get("amount", ""),
                    request.form.get("price_unit", ""),
                    existing["price"],
                )
            )
            uploaded_images, upload_errors = validate_images(
                request.files.getlist("images"), app.config
            )
            errors.extend(upload_errors)
            remove_values = request.form.getlist("remove_image_ids")
            try:
                remove_ids = {int(value) for value in remove_values}
                if any(value <= 0 for value in remove_ids):
                    raise ValueError
            except ValueError:
                remove_ids = set()
                errors.append("Select valid images to remove.")
            removable_images = [
                image for image in existing_images if image["id"] in remove_ids
            ]
            if len(removable_images) != len(remove_ids):
                errors.append("One or more selected images do not belong to this property.")
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "property_form.html", property=existing, values=request.form,
                    images=existing_images,
                    editing=existing is not None,
                ), 400

            fields = (
                "title", "property_type", "purpose", "location", "size",
                "size_unit", "status", "bedrooms", "bathrooms",
                "description", "owner_name", "owner_phone",
            )
            property_values = [data[field] for field in fields]
            saved_paths = []
            try:
                for image_data in uploaded_images:
                    filename = f"{uuid.uuid4().hex}.{image_data['extension']}"
                    path = Path(app.config["UPLOAD_FOLDER"]) / filename
                    saved_paths.append(path)
                    path.write_bytes(image_data["content"])
                    image_data["path"] = f"uploads/{filename}"

                with db:
                    if existing:
                        assignments = ", ".join(f"{field} = ?" for field in fields)
                        if preserve_legacy_price:
                            db.execute(
                                f"UPDATE properties SET {assignments} WHERE id = ?",
                                (*property_values, property_id),
                            )
                        else:
                            db.execute(
                                f"""UPDATE properties SET {assignments}, price = ?,
                                    price_pkr = ?, price_review_required = 0 WHERE id = ?""",
                                (*property_values, data["price_pkr"], data["price_pkr"], property_id),
                            )
                        target_id = property_id
                    else:
                        columns = ", ".join(fields) + ", price, price_pkr, price_review_required"
                        placeholders = ", ".join("?" for _ in range(len(fields) + 3))
                        cursor = db.execute(
                            f"INSERT INTO properties ({columns}) VALUES ({placeholders})",
                            (*property_values, data["price_pkr"], data["price_pkr"], 0),
                        )
                        target_id = cursor.lastrowid

                    for image in removable_images:
                        db.execute("DELETE FROM property_images WHERE id = ?", (image["id"],))
                    for image_data in uploaded_images:
                        db.execute(
                            """INSERT OR IGNORE INTO property_images
                               (property_id, image_path, is_primary) VALUES (?, ?, 0)""",
                            (target_id, image_data["path"]),
                        )
                    remaining = get_property_images(db, target_id)
                    if remaining and not any(image["is_primary"] for image in remaining):
                        db.execute(
                            "UPDATE property_images SET is_primary = 1 WHERE id = ?",
                            (remaining[0]["id"],),
                        )
                    primary = db.execute(
                        """SELECT image_path FROM property_images
                           WHERE property_id = ? ORDER BY is_primary DESC, id LIMIT 1""",
                        (target_id,),
                    ).fetchone()
                    db.execute(
                        "UPDATE properties SET image_path = ? WHERE id = ?",
                        (primary["image_path"] if primary else None, target_id),
                    )
            except (OSError, sqlite3.Error):
                for saved_path in saved_paths:
                    try:
                        saved_path.unlink(missing_ok=True)
                    except OSError:
                        app.logger.exception("Unable to clean up a failed property image upload")
                app.logger.exception("Unable to save property changes")
                flash("The property could not be saved. Please try again.", "error")
                return render_template(
                    "property_form.html", property=existing, values=request.form,
                    images=existing_images, editing=existing is not None,
                ), 500

            failed_image_removals = []
            for image in removable_images:
                if not delete_image_file(image["image_path"], app.config["UPLOAD_FOLDER"]):
                    failed_image_removals.append(image["image_path"])
            flash("Property updated." if existing else "Property added.", "success")
            if failed_image_removals:
                flash("Some removed image files could not be cleaned up; see the application log.", "error")
            return redirect(url_for("property_detail", property_id=target_id))

        form_values = dict(existing) if existing else {}
        if existing:
            form_values["amount"] = str(
                existing["price"] if existing["price_review_required"]
                else existing["price_pkr"] or round(existing["price"])
            )
            form_values["price_unit"] = "PKR"
        return render_template(
            "property_form.html", property=existing, values=form_values,
            images=existing_images,
            editing=existing is not None,
        )

    @app.route("/properties/<int:property_id>")
    def property_detail(property_id):
        row = get_db().execute(
            "SELECT * FROM properties WHERE id = ?", (property_id,)
        ).fetchone()
        if row is None:
            abort(404)
        images = get_property_images(get_db(), property_id)
        return render_template("property_detail.html", property=row, images=images)

    @app.post("/properties/<int:property_id>/delete")
    def property_delete(property_id):
        db = get_db()
        row = db.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
        if row is None:
            abort(404)
        images = get_property_images(db, property_id)
        db.execute("DELETE FROM properties WHERE id = ?", (property_id,))
        db.commit()
        failed_image_removals = []
        for image in images:
            if not delete_image_file(image["image_path"], app.config["UPLOAD_FOLDER"]):
                failed_image_removals.append(image["image_path"])
        flash("Property deleted.", "success")
        if failed_image_removals:
            flash("Some image files could not be cleaned up; see the application log.", "error")
        return redirect(url_for("properties"))

    @app.route("/clients")
    def clients():
        query = request.args.get("q", "").strip()
        rows = get_db().execute(
            """SELECT * FROM clients WHERE full_name LIKE ? OR phone LIKE ?
               OR COALESCE(email, '') LIKE ? ORDER BY created_at DESC, id DESC""",
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        ).fetchall()
        return render_template("clients.html", clients=rows, query=query)

    @app.route("/clients/new", methods=["GET", "POST"])
    @app.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
    def client_form(client_id=None):
        db = get_db()
        existing = None
        if client_id is not None:
            existing = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
            if existing is None:
                abort(404)
        if request.method == "POST":
            data, errors = validate_client(request.form)
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "client_form.html", client=existing, values=request.form,
                    editing=existing is not None,
                ), 400
            fields = (
                "full_name", "phone", "email", "client_type", "budget_min",
                "budget_max", "preferred_location", "preferred_property_type",
                "preferred_purpose", "required_size", "required_size_unit", "notes",
            )
            values = [data[field] for field in fields]
            if existing:
                assignments = ", ".join(f"{field} = ?" for field in fields)
                db.execute(
                    f"UPDATE clients SET {assignments} WHERE id = ?",
                    (*values, client_id),
                )
                target_id = client_id
                flash("Client updated.", "success")
            else:
                placeholders = ", ".join("?" for _ in fields)
                cursor = db.execute(
                    f"INSERT INTO clients ({', '.join(fields)}) VALUES ({placeholders})",
                    values,
                )
                target_id = cursor.lastrowid
                flash("Client added.", "success")
            db.commit()
            return redirect(url_for("client_detail", client_id=target_id))
        return render_template(
            "client_form.html", client=existing, values=existing or {},
            editing=existing is not None,
        )

    @app.route("/clients/<int:client_id>")
    def client_detail(client_id):
        db = get_db()
        client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        if client is None:
            abort(404)
        leads = db.execute(
            """SELECT leads.*, properties.title AS property_title
               FROM leads LEFT JOIN properties ON properties.id = leads.property_id
               WHERE client_id = ? ORDER BY leads.created_at DESC""",
            (client_id,),
        ).fetchall()
        return render_template("client_detail.html", client=client, leads=leads)

    @app.post("/clients/<int:client_id>/delete")
    def client_delete(client_id):
        db = get_db()
        if db.execute("SELECT id FROM clients WHERE id = ?", (client_id,)).fetchone() is None:
            abort(404)
        db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        db.commit()
        flash("Client and their associated leads deleted.", "success")
        return redirect(url_for("clients"))

    @app.route("/leads")
    def leads():
        query, status = request.args.get("q", "").strip(), request.args.get("status", "")
        sql = """SELECT leads.*, clients.full_name AS client_name,
                 properties.title AS property_title
                 FROM leads JOIN clients ON clients.id = leads.client_id
                 LEFT JOIN properties ON properties.id = leads.property_id"""
        clauses, params = [], []
        if query:
            clauses.append("(clients.full_name LIKE ? OR leads.notes LIKE ?)")
            params.extend((f"%{query}%", f"%{query}%"))
        if status in LEAD_STATUSES:
            clauses.append("leads.status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CASE WHEN next_follow_up IS NULL THEN 1 ELSE 0 END, next_follow_up, leads.created_at DESC"
        rows = get_db().execute(sql, params).fetchall()
        return render_template("leads.html", leads=rows, query=query, selected_status=status)

    @app.route("/leads/new", methods=["GET", "POST"])
    @app.route("/leads/<int:lead_id>/edit", methods=["GET", "POST"])
    def lead_form(lead_id=None):
        db = get_db()
        existing = None
        if lead_id is not None:
            existing = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if existing is None:
                abort(404)
        clients = db.execute("SELECT id, full_name FROM clients ORDER BY full_name").fetchall()
        properties = db.execute(
            "SELECT id, title FROM properties ORDER BY title"
        ).fetchall()
        if request.method == "POST":
            data, errors = validate_lead(request.form, db)
            if errors:
                for error in errors:
                    flash(error, "error")
                return render_template(
                    "lead_form.html", lead=existing, values=request.form, clients=clients,
                    properties=properties, editing=existing is not None,
                ), 400
            fields = (
                "client_id", "property_id", "source", "status", "next_follow_up", "notes"
            )
            values = [data[field] for field in fields]
            if existing:
                assignments = ", ".join(f"{field} = ?" for field in fields)
                db.execute(
                    f"UPDATE leads SET {assignments} WHERE id = ?",
                    (*values, lead_id),
                )
                flash("Lead updated.", "success")
            else:
                placeholders = ", ".join("?" for _ in fields)
                db.execute(
                    f"INSERT INTO leads ({', '.join(fields)}) VALUES ({placeholders})",
                    values,
                )
                flash("Lead added.", "success")
            db.commit()
            return redirect(url_for("leads"))
        return render_template(
            "lead_form.html", lead=existing, values=existing or {}, clients=clients,
            properties=properties, editing=existing is not None,
        )

    @app.post("/leads/<int:lead_id>/delete")
    def lead_delete(lead_id):
        db = get_db()
        if db.execute("SELECT id FROM leads WHERE id = ?", (lead_id,)).fetchone() is None:
            abort(404)
        db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        db.commit()
        flash("Lead deleted.", "success")
        return redirect(url_for("leads"))

    @app.route("/follow-ups")
    def followups():
        today = date.today().isoformat()
        rows = get_db().execute(
            """SELECT leads.*, clients.full_name AS client_name,
                      clients.phone AS client_phone, properties.title AS property_title
               FROM leads JOIN clients ON clients.id = leads.client_id
               LEFT JOIN properties ON properties.id = leads.property_id
               WHERE next_follow_up IS NOT NULL AND leads.status NOT IN ('converted', 'lost')
               ORDER BY CASE WHEN next_follow_up < ? THEN 0 ELSE 1 END,
                        next_follow_up, leads.id""",
            (today,),
        ).fetchall()
        return render_template("followups.html", leads=rows, today=today)

    @app.route("/matching")
    def matching():
        db = get_db()
        clients = db.execute("SELECT id, full_name FROM clients ORDER BY full_name").fetchall()
        selected_id = request.args.get("client_id", type=int)
        client = db.execute("SELECT * FROM clients WHERE id = ?", (selected_id,)).fetchone() if selected_id else None
        matches = []
        if client:
            candidates = db.execute(
                """SELECT * FROM properties
                   WHERE status = 'available' AND price_review_required = 0
                   ORDER BY price_pkr"""
            ).fetchall()
            for candidate in candidates:
                if client["preferred_location"] and client["preferred_location"].lower() not in candidate["location"].lower():
                    continue
                if client["preferred_property_type"] and client["preferred_property_type"] != candidate["property_type"]:
                    continue
                if client["preferred_purpose"] and client["preferred_purpose"] != candidate["purpose"]:
                    continue
                if client["budget_min"] is not None and candidate["price_pkr"] < client["budget_min"]:
                    continue
                if client["budget_max"] is not None and candidate["price_pkr"] > client["budget_max"]:
                    continue
                if client["required_size"] is not None:
                    if client["required_size_unit"] != candidate["size_unit"] or candidate["size"] < client["required_size"]:
                        continue
                matches.append(candidate)
        return render_template(
            "matching.html", clients=clients, client=client, matches=matches,
            selected_id=selected_id,
        )

    @app.cli.command("demo-data")
    def demo_data():
        """Load fictional demo records, without duplicating an existing demo set."""
        db = get_db()
        if db.execute("SELECT id FROM clients WHERE full_name = ?", ("Sample Buyer",)).fetchone():
            raise RuntimeError("Demo data already exists; no records were added.")
        cursor = db.execute(
            """INSERT INTO properties
               (title, property_type, purpose, location, size, size_unit, price, price_pkr, status,
                bedrooms, bathrooms, description)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("Fictional Residential Plot", "plot", "sale", "Gulberg Greens, Islamabad",
             10, "marla", 8500000, 8500000, "available", None, None, "Fictional demo listing."),
        )
        property_id = cursor.lastrowid
        cursor = db.execute(
            """INSERT INTO clients
               (full_name, phone, client_type, budget_min, budget_max,
                preferred_location, preferred_property_type, preferred_purpose,
                required_size, required_size_unit, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("Sample Buyer", "0300-0000000", "buyer", 5000000, 10000000,
             "Gulberg Greens", "plot", "sale", 5, "marla", "Fictional demo client."),
        )
        db.execute(
            """INSERT INTO leads (client_id, property_id, source, status, next_follow_up, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cursor.lastrowid, property_id, "website", "new", date.today().isoformat(),
             "Fictional demo follow-up."),
        )
        db.commit()
        print("Added fictional demo property, client, and lead.")

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def internal_error(error):
        original = getattr(error, "original_exception", None)
        if original is not None:
            app.logger.error(
                "Unhandled application exception",
                exc_info=(type(original), original, original.__traceback__),
            )
        else:
            app.logger.error("Request failed with an internal application error")
        return render_template("errors/500.html"), 500

    @app.errorhandler(413)
    def request_too_large(_error):
        return render_template("errors/413.html"), 413

    return app


def format_south_asian(amount):
    digits = str(abs(int(amount)))
    if len(digits) <= 3:
        return ("-" if amount < 0 else "") + digits
    last_three, leading = digits[-3:], digits[:-3]
    groups = []
    while leading:
        groups.append(leading[-2:])
        leading = leading[:-2]
    return ("-" if amount < 0 else "") + ",".join(reversed(groups)) + "," + last_three


def get_property_images(db, property_id):
    return db.execute(
        """SELECT id, property_id, image_path, is_primary
           FROM property_images WHERE property_id = ?
           ORDER BY is_primary DESC, id""",
        (property_id,),
    ).fetchall()


def attach_property_images(db, properties):
    ids = [property_row["id"] for property_row in properties]
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT id, property_id, image_path, is_primary
            FROM property_images WHERE property_id IN ({placeholders})
            ORDER BY is_primary DESC, id""",
        ids,
    ).fetchall()
    image_map = {}
    for row in rows:
        image_map.setdefault(row["property_id"], []).append(row)
    for property_row in properties:
        property_row["images"] = image_map.get(property_row["id"], [])


def validate_images(files, config):
    valid, errors = [], []
    for image_file in files:
        if not image_file or not image_file.filename:
            continue
        extension = Path(image_file.filename).suffix.lower().lstrip(".")
        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            errors.append("Images must use PNG, JPG, JPEG, or WEBP format.")
            continue
        content = image_file.stream.read(config["MAX_IMAGE_BYTES"] + 1)
        image_file.stream.seek(0)
        if len(content) > config["MAX_IMAGE_BYTES"]:
            errors.append(
                f"Each image must be {config['MAX_IMAGE_BYTES'] // (1024 * 1024)} MB or smaller."
            )
            continue
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.format != IMAGE_FORMATS[extension]:
                    errors.append("An image's file extension does not match its actual format.")
                    continue
                if image.width * image.height > config["MAX_IMAGE_PIXELS"]:
                    errors.append("An image exceeds the maximum allowed pixel dimensions.")
                    continue
                image.verify()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            errors.append("One or more uploaded files are not valid images.")
            continue
        valid.append({"extension": extension, "content": content})
    return valid, errors


def delete_image_file(image_path, upload_folder):
    candidate = Path(upload_folder) / Path(image_path).name
    try:
        candidate.unlink(missing_ok=True)
        return True
    except OSError:
        logging.getLogger(__name__).exception("Unable to remove property image file")
        return False


def validate_property(form):
    errors = []
    data = {}
    data["title"] = form.get("title", "").strip()
    data["location"] = form.get("location", "").strip()
    data["description"] = form.get("description", "").strip()
    data["owner_name"] = form.get("owner_name", "").strip()
    data["owner_phone"] = form.get("owner_phone", "").strip()
    validate_text_lengths(
        data,
        {"title": 150, "location": 150, "description": 3000,
         "owner_name": 100, "owner_phone": 50},
        errors,
    )
    for key, label in (("title", "Title"), ("location", "Location")):
        if not data[key]:
            errors.append(f"{label} is required.")
    for key, allowed, label in (
        ("property_type", PROPERTY_TYPES, "Property type"),
        ("purpose", PURPOSES, "Purpose"),
        ("status", PROPERTY_STATUSES, "Status"),
        ("size_unit", SIZE_UNITS, "Size unit"),
    ):
        data[key] = form.get(key, "").strip()
        if data[key] not in allowed:
            errors.append(f"Select a valid {label.lower()}.")
    for key in ("size",):
        try:
            number = float(form.get(key, ""))
            if not math.isfinite(number) or number <= 0:
                raise ValueError
            data[key] = number
        except ValueError:
            data[key] = None
            errors.append(f"{key.title()} must be greater than zero.")
    data["price_pkr"], price_error = normalize_price(
        form.get("amount", form.get("price", "")),
        form.get("price_unit", "PKR"),
    )
    if price_error:
        errors.append(price_error)
    for key in ("bedrooms", "bathrooms"):
        raw = form.get(key, "").strip()
        try:
            data[key] = int(raw) if raw else None
            if data[key] is not None and data[key] < 0:
                raise ValueError
        except ValueError:
            data[key] = None
            errors.append(f"{key.title()} must be a non-negative whole number.")
    return data, errors


def normalize_price(amount, unit):
    if unit not in PRICE_UNITS:
        return None, "Select a valid price unit."
    try:
        value = Decimal(amount.strip().replace(",", ""))
        if not value.is_finite() or value <= 0:
            raise InvalidOperation
        normalized = (value * PRICE_UNITS[unit]).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        if normalized <= 0 or normalized > Decimal("9223372036854775807"):
            raise InvalidOperation
        return int(normalized), None
    except (InvalidOperation, ValueError):
        return None, "Enter a valid amount greater than zero."


def price_matches_legacy_value(amount, unit, legacy_price):
    if unit != "PKR":
        return False
    try:
        return Decimal(amount.strip().replace(",", "")) == Decimal(str(legacy_price))
    except (InvalidOperation, ValueError):
        return False


def validate_client(form):
    errors = []
    data = {
        "full_name": form.get("full_name", "").strip(),
        "phone": form.get("phone", "").strip(),
        "email": form.get("email", "").strip() or None,
        "preferred_location": form.get("preferred_location", "").strip(),
        "notes": form.get("notes", "").strip(),
    }
    validate_text_lengths(
        data,
        {"full_name": 150, "phone": 50, "email": 254,
         "preferred_location": 150, "notes": 3000},
        errors,
    )
    if not data["full_name"]:
        errors.append("Full name is required.")
    if not data["phone"]:
        errors.append("Phone number is required.")
    if data["email"] and ("@" not in data["email"] or data["email"].startswith("@") or data["email"].endswith("@")):
        errors.append("Enter a valid email address.")
    data["client_type"] = form.get("client_type", "")
    if data["client_type"] not in CLIENT_TYPES:
        errors.append("Select a valid client type.")
    data["preferred_property_type"] = form.get("preferred_property_type", "") or None
    if data["preferred_property_type"] and data["preferred_property_type"] not in PROPERTY_TYPES:
        errors.append("Select a valid preferred property type.")
    data["preferred_purpose"] = form.get("preferred_purpose", "") or None
    if data["preferred_purpose"] and data["preferred_purpose"] not in PURPOSES:
        errors.append("Select a valid preferred purpose.")
    for key in ("budget_min", "budget_max", "required_size"):
        raw = form.get(key, "").strip()
        try:
            data[key] = float(raw) if raw else None
            if data[key] is not None and (not math.isfinite(data[key]) or data[key] <= 0):
                raise ValueError
        except ValueError:
            data[key] = None
            errors.append(f"{key.replace('_', ' ').title()} must be greater than zero.")
    if data.get("budget_min") and data.get("budget_max") and data["budget_min"] > data["budget_max"]:
        errors.append("Minimum budget cannot exceed maximum budget.")
    data["required_size_unit"] = form.get("required_size_unit", "") or None
    if data["required_size_unit"] and data["required_size_unit"] not in SIZE_UNITS:
        errors.append("Select a valid required size unit.")
    if data["required_size"] is not None and not data["required_size_unit"]:
        errors.append("Select a valid required size unit.")
    return data, errors


def validate_lead(form, db):
    errors = []
    try:
        client_id = int(form.get("client_id", ""))
        if db.execute("SELECT id FROM clients WHERE id = ?", (client_id,)).fetchone() is None:
            raise ValueError
    except ValueError:
        client_id = None
        errors.append("Select an existing client.")
    raw_property_id = form.get("property_id", "").strip()
    property_id = None
    if raw_property_id:
        try:
            property_id = int(raw_property_id)
            if db.execute("SELECT id FROM properties WHERE id = ?", (property_id,)).fetchone() is None:
                raise ValueError
        except ValueError:
            property_id = None
            errors.append("Select an existing property.")
    source, status = form.get("source", ""), form.get("status", "")
    if source not in LEAD_SOURCES:
        errors.append("Select a valid lead source.")
    if status not in LEAD_STATUSES:
        errors.append("Select a valid lead status.")
    followup = form.get("next_follow_up", "").strip() or None
    if followup:
        try:
            date.fromisoformat(followup)
        except ValueError:
            errors.append("Enter a valid follow-up date.")
    data = {
        "client_id": client_id, "property_id": property_id, "source": source,
        "status": status, "next_follow_up": followup,
        "notes": form.get("notes", "").strip(),
    }
    validate_text_lengths(data, {"notes": 3000}, errors)
    return data, errors


def validate_text_lengths(data, limits, errors):
    for field, limit in limits.items():
        if data[field] is not None and len(data[field]) > limit:
            errors.append(
                f"{field.replace('_', ' ').title()} must be {limit} characters or fewer."
            )


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
