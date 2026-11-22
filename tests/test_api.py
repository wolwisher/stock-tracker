import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    app.config["TESTING"] = True
    return app.test_client()


def add(client, **overrides):
    item = {"sku": "BTL-500", "name": "PET Bottle 500ml", "category": "Packaging",
            "quantity": 100, "reorder_level": 20}
    item.update(overrides)
    return client.post("/api/items", json=item)


def test_create_item_records_opening_stock(client):
    res = add(client)
    assert res.status_code == 201
    item = res.get_json()
    assert item["quantity"] == 100 and item["low_stock"] is False
    moves = client.get(f"/api/items/{item['id']}/movements").get_json()["movements"]
    assert [(m["kind"], m["quantity"]) for m in moves] == [("in", 100)]


def test_duplicate_sku_rejected(client):
    add(client)
    res = add(client)
    assert res.status_code == 409


def test_missing_fields_rejected(client):
    assert client.post("/api/items", json={"sku": "X"}).status_code == 400
    assert add(client, quantity=-5).status_code == 400


def test_search_filter_and_pagination(client):
    add(client)
    add(client, sku="CAP-28", name="Bottle Cap 28mm", quantity=5, reorder_level=50)
    add(client, sku="VIT-C", name="Vitamin C Tablets", category="Products", quantity=40)

    assert client.get("/api/items?q=cap").get_json()["total"] == 1
    assert client.get("/api/items?category=Packaging").get_json()["total"] == 2
    low = client.get("/api/items?low_stock=true").get_json()
    assert [i["sku"] for i in low["items"]] == ["CAP-28"]

    page = client.get("/api/items?per_page=2&page=2&sort=name").get_json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert client.get("/api/items?sort=password").status_code == 400


def test_stock_in_and_out(client):
    item_id = add(client).get_json()["id"]
    res = client.post(f"/api/items/{item_id}/movements", json={"kind": "out", "quantity": 85})
    assert res.status_code == 201
    assert res.get_json()["quantity"] == 15
    assert res.get_json()["low_stock"] is True

    res = client.post(f"/api/items/{item_id}/movements", json={"kind": "in", "quantity": 50, "note": "PO-114"})
    assert res.get_json()["quantity"] == 65


def test_cannot_go_below_zero(client):
    item_id = add(client, quantity=10).get_json()["id"]
    res = client.post(f"/api/items/{item_id}/movements", json={"kind": "out", "quantity": 11})
    assert res.status_code == 409
    assert client.get(f"/api/items/{item_id}").get_json()["quantity"] == 10


def test_invalid_movement(client):
    item_id = add(client).get_json()["id"]
    assert client.post(f"/api/items/{item_id}/movements", json={"kind": "lost", "quantity": 1}).status_code == 400
    assert client.post(f"/api/items/{item_id}/movements", json={"kind": "in", "quantity": 0}).status_code == 400
    assert client.post("/api/items/999/movements", json={"kind": "in", "quantity": 1}).status_code == 404


def test_update_and_delete(client):
    item_id = add(client).get_json()["id"]
    res = client.patch(f"/api/items/{item_id}", json={"name": "PET Bottle 500ml Clear", "reorder_level": 150})
    assert res.get_json()["name"] == "PET Bottle 500ml Clear"
    assert res.get_json()["low_stock"] is True
    assert client.patch(f"/api/items/{item_id}", json={"quantity": 5}).status_code == 400
    assert client.delete(f"/api/items/{item_id}").status_code == 204
    assert client.get(f"/api/items/{item_id}").status_code == 404


def test_summary(client):
    add(client)
    add(client, sku="CAP-28", name="Bottle Cap 28mm", quantity=5, reorder_level=50)
    s = client.get("/api/summary").get_json()
    assert s == {"items": 2, "units": 105, "low_stock": 1, "categories": ["Packaging"]}


def test_dashboard_served(client):
    res = client.get("/")
    assert res.status_code == 200 and b"Stock Tracker" in res.data
