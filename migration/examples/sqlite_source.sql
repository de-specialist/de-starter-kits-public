PRAGMA foreign_keys = ON;

CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    country TEXT
);

CREATE TABLE products (
    product_id TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    category TEXT
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    order_status TEXT NOT NULL,
    order_date DATE NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE order_items (
    order_item_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL,
    unit_price NUMERIC NOT NULL
);

INSERT INTO customers VALUES
    ('C001', '2025-01-05T10:15:00Z', 'AU'),
    ('C002', '2025-01-08T09:00:00Z', 'NZ'),
    ('C003', '2025-01-12T12:30:00Z', 'AU');

INSERT INTO products VALUES
    ('P001', 'Canvas Tote', 'Bags'),
    ('P002', 'Travel Mug', 'Home'),
    ('P003', 'Notebook', 'Stationery');

INSERT INTO orders VALUES
    ('O100', 'C001', 'complete', '2025-02-01', '2025-02-01T11:05:00Z'),
    ('O101', 'C002', 'returned', '2025-02-02', '2025-02-05T08:00:00Z'),
    ('O102', 'C001', 'complete', '2025-02-03', '2025-02-03T16:50:00Z');

INSERT INTO order_items VALUES
    ('I001', 'O100', 'P001', 2, 18.50),
    ('I002', 'O100', 'P003', 1, 6.00),
    ('I003', 'O101', 'P002', 1, 24.00),
    ('I004', 'O102', 'P003', 3, 6.00);
