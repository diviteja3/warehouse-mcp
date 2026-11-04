"""Create a small e-commerce database with fake PII for demos and tests.

python examples/seed_demo.py                         # SQLite: examples/demo.db
python examples/seed_demo.py postgresql+psycopg://...  # any SQLAlchemy URL
"""

import random
import sys
from datetime import date, timedelta

from sqlalchemy import create_engine, text

SCHEMA = [
    """CREATE TABLE customers (
        id INTEGER PRIMARY KEY, full_name TEXT NOT NULL, email TEXT NOT NULL,
        phone TEXT, region TEXT NOT NULL, signup_date DATE NOT NULL)""",
    """CREATE TABLE products (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price NUMERIC(10,2) NOT NULL)""",
    """CREATE TABLE orders (
        id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
        product_id INTEGER NOT NULL REFERENCES products(id), quantity INTEGER NOT NULL,
        order_date DATE NOT NULL, status TEXT NOT NULL)""",
    """CREATE TABLE support_tickets (
        id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
        opened DATE NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL)""",
]

FIRST = ["Ana", "Ben", "Chen", "Dev", "Eli", "Fatima", "Gus", "Hana", "Ivan", "Jo"]
LAST = ["Lopez", "Smith", "Wang", "Patel", "Kim", "Okafor", "Novak", "Sato", "Reyes", "Berg"]
REGIONS = ["NA", "EU", "APAC", "LATAM"]
PRODUCTS = [
    ("Trail Shoe", "footwear", 129),
    ("Rain Shell", "outerwear", 189),
    ("Wool Sock", "accessories", 18),
    ("Day Pack", "bags", 79),
    ("Base Layer", "apparel", 59),
    ("Headlamp", "gear", 39),
]
STATUSES = ["delivered"] * 8 + ["returned", "cancelled"]


def seed(url: str) -> None:
    rng = random.Random(7)
    engine = create_engine(url)
    with engine.begin() as conn:
        for table in ("support_tickets", "orders", "products", "customers"):
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        for ddl in SCHEMA:
            conn.execute(text(ddl))

        # ISO strings, not date objects: sqlite3's default date adapter is deprecated in 3.12.
        start = date(2026, 1, 1)
        customers = []
        for i in range(1, 51):
            first, last = rng.choice(FIRST), rng.choice(LAST)
            customers.append(
                {
                    "id": i,
                    "full_name": f"{first} {last}",
                    "email": f"{first.lower()}.{last.lower()}{i}@example.com",
                    "phone": f"+1-555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
                    "region": rng.choice(REGIONS),
                    "signup_date": (start + timedelta(days=rng.randint(0, 120))).isoformat(),
                }
            )
        conn.execute(
            text("INSERT INTO customers VALUES (:id, :full_name, :email, :phone, :region, :signup_date)"), customers
        )
        conn.execute(
            text("INSERT INTO products VALUES (:id, :name, :category, :price)"),
            [{"id": i, "name": n, "category": c, "price": p} for i, (n, c, p) in enumerate(PRODUCTS, 1)],
        )
        conn.execute(
            text("INSERT INTO orders VALUES (:id, :c, :p, :q, :d, :s)"),
            [
                {
                    "id": i,
                    "c": rng.randint(1, 50),
                    "p": rng.randint(1, len(PRODUCTS)),
                    "q": rng.randint(1, 4),
                    "d": (start + timedelta(days=rng.randint(0, 300))).isoformat(),
                    "s": rng.choice(STATUSES),
                }
                for i in range(1, 501)
            ],
        )
        # Free-text bodies that contain PII, to exercise value-level masking.
        conn.execute(
            text("INSERT INTO support_tickets VALUES (:id, :c, :d, :s, :b)"),
            [
                {
                    "id": 1,
                    "c": 3,
                    "d": "2026-03-02",
                    "s": "Refund",
                    "b": "Please refund to card 4111 1111 1111 1111.",
                },
                {"id": 2, "c": 9, "d": "2026-04-09", "s": "Login", "b": "Reach me at backup.mail@example.org."},
                {"id": 3, "c": 12, "d": "2026-05-01", "s": "Tax form", "b": "My SSN is 123-45-6789 for the form."},
                {"id": 4, "c": 20, "d": "2026-06-03", "s": "Sizing", "b": "Do the trail shoes run small?"},
            ],
        )


if __name__ == "__main__":
    seed(sys.argv[1] if len(sys.argv) > 1 else "sqlite:///examples/demo.db")
    print("seeded")
