"""Stock Tracker - a small inventory management API with a web dashboard.

Run:  python app.py   (then open http://localhost:5000)
"""
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request, send_from_directory

DEFAULT_DB = os.path.join(os.path.dirname(__file__), "inventory.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sku           TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    category      TEXT    NOT NULL DEFAULT 'General',
    unit          TEXT    NOT NULL DEFAULT 'pcs',
    quantity      INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    reorder_level INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    created_at    TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL CHECK (kind IN ('in', 'out')),
    quantity   INTEGER NOT NULL CHECK (quantity > 0),
    note       TEXT,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_movements_item ON movements(item_id);
"""

SORTABLE = {"name", "sku", "category", "quantity", "created_at"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def create_app(db_path=DEFAULT_DB):
    app = Flask(__name__, static_folder="static")
    app.config["DB_PATH"] = db_path

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB_PATH"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    with app.app_context():
        get_db().executescript(SCHEMA)

    @app.errorhandler(ApiError)
    def handle_api_error(err):
        return jsonify(error=err.message), err.status

    def item_dict(row):
        item = dict(row)
        item["low_stock"] = item["quantity"] <= item["reorder_level"]
        return item

    def fetch_item(item_id):
        row = get_db().execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ApiError("Item not found", 404)
        return row

    def read_int(data, field, default=None, minimum=0):
        value = data.get(field, default)
        if value is None:
            raise ApiError(f"'{field}' is required")
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ApiError(f"'{field}' must be a whole number")
        if value < minimum:
            raise ApiError(f"'{field}' must be at least {minimum}")
        return value

    # ---------- pages ----------
    @app.get("/")
    def dashboard():
        return send_from_directory(app.static_folder, "index.html")

    # ---------- items ----------
    @app.get("/api/items")
    def list_items():
        """Search and filter: ?q=&category=&low_stock=true&sort=name&order=asc&page=1&per_page=20"""
        args = request.args
        where, params = [], []
        if q := args.get("q", "").strip():
            where.append("(name LIKE ? OR sku LIKE ?)")
            params += [f"%{q}%", f"%{q}%"]
        if category := args.get("category", "").strip():
            where.append("category = ?")
            params.append(category)
        if args.get("low_stock") == "true":
            where.append("quantity <= reorder_level")

        sort = args.get("sort", "name")
        if sort not in SORTABLE:
            raise ApiError(f"Cannot sort by '{sort}'")
        order = "DESC" if args.get("order", "asc").lower() == "desc" else "ASC"
        page = read_int(args, "page", 1, minimum=1)
        per_page = min(read_int(args, "per_page", 20, minimum=1), 100)

        clause = f"WHERE {' AND '.join(where)}" if where else ""
        db = get_db()
        total = db.execute(f"SELECT COUNT(*) FROM items {clause}", params).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM items {clause} ORDER BY {sort} {order} LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
        return jsonify(items=[item_dict(r) for r in rows], total=total, page=page, per_page=per_page)

    @app.post("/api/items")
    def create_item():
        data = request.get_json(silent=True) or {}
        sku, name = str(data.get("sku", "")).strip(), str(data.get("name", "")).strip()
        if not sku or not name:
            raise ApiError("'sku' and 'name' are required")
        values = (
            sku,
            name,
            str(data.get("category") or "General").strip(),
            str(data.get("unit") or "pcs").strip(),
            read_int(data, "quantity", 0),
            read_int(data, "reorder_level", 0),
            now(),
        )
        db = get_db()
        try:
            cur = db.execute(
                "INSERT INTO items (sku, name, category, unit, quantity, reorder_level, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        except sqlite3.IntegrityError:
            raise ApiError(f"SKU '{sku}' already exists", 409)
        if values[4] > 0:
            db.execute(
                "INSERT INTO movements (item_id, kind, quantity, note, created_at) VALUES (?, 'in', ?, ?, ?)",
                (cur.lastrowid, values[4], "Opening stock", now()),
            )
        db.commit()
        return jsonify(item_dict(fetch_item(cur.lastrowid))), 201

    @app.get("/api/items/<int:item_id>")
    def get_item(item_id):
        return jsonify(item_dict(fetch_item(item_id)))

    @app.patch("/api/items/<int:item_id>")
    def update_item(item_id):
        fetch_item(item_id)
        data = request.get_json(silent=True) or {}
        updates, params = [], []
        for field in ("name", "category", "unit"):
            if field in data:
                value = str(data[field]).strip()
                if not value:
                    raise ApiError(f"'{field}' cannot be empty")
                updates.append(f"{field} = ?")
                params.append(value)
        if "reorder_level" in data:
            updates.append("reorder_level = ?")
            params.append(read_int(data, "reorder_level"))
        if "quantity" in data:
            raise ApiError("Change quantity with a stock movement (POST /api/items/<id>/movements)")
        if not updates:
            raise ApiError("Nothing to update")
        db = get_db()
        db.execute(f"UPDATE items SET {', '.join(updates)} WHERE id = ?", params + [item_id])
        db.commit()
        return jsonify(item_dict(fetch_item(item_id)))

    @app.delete("/api/items/<int:item_id>")
    def delete_item(item_id):
        fetch_item(item_id)
        db = get_db()
        db.execute("DELETE FROM items WHERE id = ?", (item_id,))
        db.commit()
        return "", 204

    # ---------- stock movements ----------
    @app.post("/api/items/<int:item_id>/movements")
    def add_movement(item_id):
        data = request.get_json(silent=True) or {}
        kind = data.get("kind")
        if kind not in ("in", "out"):
            raise ApiError("'kind' must be 'in' or 'out'")
        qty = read_int(data, "quantity", minimum=1)
        db = get_db()
        # Single conditional UPDATE so two simultaneous 'out' requests can't push stock below zero.
        if kind == "in":
            cur = db.execute("UPDATE items SET quantity = quantity + ? WHERE id = ?", (qty, item_id))
        else:
            cur = db.execute(
                "UPDATE items SET quantity = quantity - ? WHERE id = ? AND quantity >= ?",
                (qty, item_id, qty),
            )
        if cur.rowcount == 0:
            item = fetch_item(item_id)  # raises 404 if missing
            raise ApiError(f"Not enough stock: {item['quantity']} {item['unit']} available", 409)
        db.execute(
            "INSERT INTO movements (item_id, kind, quantity, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (item_id, kind, qty, (data.get("note") or "").strip() or None, now()),
        )
        db.commit()
        return jsonify(item_dict(fetch_item(item_id))), 201

    @app.get("/api/items/<int:item_id>/movements")
    def list_movements(item_id):
        fetch_item(item_id)
        rows = get_db().execute(
            "SELECT * FROM movements WHERE item_id = ? ORDER BY id DESC", (item_id,)
        ).fetchall()
        return jsonify(movements=[dict(r) for r in rows])

    # ---------- summary ----------
    @app.get("/api/summary")
    def summary():
        db = get_db()
        row = db.execute(
            "SELECT COUNT(*) AS items, COALESCE(SUM(quantity), 0) AS units,"
            " COALESCE(SUM(quantity <= reorder_level), 0) AS low_stock FROM items"
        ).fetchone()
        categories = [r[0] for r in db.execute("SELECT DISTINCT category FROM items ORDER BY category")]
        return jsonify(dict(row) | {"categories": categories})

    return app


if __name__ == "__main__":
    create_app().run(debug=True)
