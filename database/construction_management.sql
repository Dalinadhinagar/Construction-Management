-- Construction Management System schema and demonstration data.
-- Run this file once in MySQL before starting the Flask application.
CREATE DATABASE IF NOT EXISTS construction_management
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
USE construction_management;

SET FOREIGN_KEY_CHECKS = 0;
DROP TABLE IF EXISTS deliveries;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS request_items;
DROP TABLE IF EXISTS purchase_requests;
DROP TABLE IF EXISTS materials;
DROP TABLE IF EXISTS users;
SET FOREIGN_KEY_CHECKS = 1;

CREATE TABLE users (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(120) NOT NULL,
    email VARCHAR(254) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('Admin', 'Supplier', 'Purchaser', 'User') NOT NULL,
    phone VARCHAR(30),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT chk_users_name CHECK (CHAR_LENGTH(TRIM(name)) > 0),
    CONSTRAINT chk_users_email CHECK (CHAR_LENGTH(TRIM(email)) > 0)
) ENGINE=InnoDB;

CREATE TABLE materials (
    material_id INT AUTO_INCREMENT PRIMARY KEY,
    supplier_id INT NOT NULL,
    material_name VARCHAR(150) NOT NULL,
    description TEXT,
    unit VARCHAR(40) NOT NULL,
    price DECIMAL(12, 2) NOT NULL,
    stock_quantity INT NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    INDEX idx_material_active_name (is_active, material_name),
    CONSTRAINT fk_materials_supplier FOREIGN KEY (supplier_id)
        REFERENCES users(user_id) ON DELETE RESTRICT,
    CONSTRAINT chk_material_price CHECK (price > 0),
    CONSTRAINT chk_material_stock CHECK (stock_quantity >= 0),
    CONSTRAINT chk_material_name CHECK (CHAR_LENGTH(TRIM(material_name)) > 0),
    CONSTRAINT chk_material_unit CHECK (CHAR_LENGTH(TRIM(unit)) > 0)
) ENGINE=InnoDB;

CREATE TABLE purchase_requests (
    request_id INT AUTO_INCREMENT PRIMARY KEY,
    purchaser_id INT NOT NULL,
    request_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    required_date DATE NOT NULL,
    status ENUM('Pending', 'Approved', 'Rejected') NOT NULL DEFAULT 'Pending',
    CONSTRAINT fk_requests_purchaser FOREIGN KEY (purchaser_id)
        REFERENCES users(user_id) ON DELETE RESTRICT,
    CONSTRAINT chk_required_date CHECK (required_date >= DATE(request_date))
) ENGINE=InnoDB;

CREATE TABLE request_items (
    request_item_id INT AUTO_INCREMENT PRIMARY KEY,
    request_id INT NOT NULL,
    material_id INT NOT NULL,
    material_name_snapshot VARCHAR(150) NOT NULL,
    unit_snapshot VARCHAR(40) NOT NULL,
    supplier_id INT NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(12, 2) NOT NULL,
    CONSTRAINT fk_request_items_request FOREIGN KEY (request_id)
        REFERENCES purchase_requests(request_id) ON DELETE CASCADE,
    CONSTRAINT fk_request_items_material FOREIGN KEY (material_id)
        REFERENCES materials(material_id) ON DELETE RESTRICT,
    CONSTRAINT fk_request_items_supplier FOREIGN KEY (supplier_id)
        REFERENCES users(user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_request_material UNIQUE (request_id, material_id),
    CONSTRAINT chk_request_quantity CHECK (quantity > 0),
    CONSTRAINT chk_request_unit_price CHECK (unit_price > 0)
) ENGINE=InnoDB;

CREATE TABLE orders (
    order_id INT AUTO_INCREMENT PRIMARY KEY,
    request_id INT NOT NULL,
    supplier_id INT NOT NULL,
    order_date TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    total_amount DECIMAL(14, 2) NOT NULL,
    order_status ENUM('Confirmed', 'Processing', 'Dispatched', 'Delivered', 'Cancelled')
        NOT NULL DEFAULT 'Confirmed',
    CONSTRAINT fk_orders_request FOREIGN KEY (request_id)
        REFERENCES purchase_requests(request_id) ON DELETE RESTRICT,
    CONSTRAINT fk_orders_supplier FOREIGN KEY (supplier_id)
        REFERENCES users(user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_request_supplier_order UNIQUE (request_id, supplier_id),
    CONSTRAINT chk_order_total CHECK (total_amount >= 0)
) ENGINE=InnoDB;

CREATE TABLE order_items (
    order_item_id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    request_item_id INT NOT NULL,
    material_id INT NOT NULL,
    material_name_snapshot VARCHAR(150) NOT NULL,
    unit_snapshot VARCHAR(40) NOT NULL,
    quantity INT NOT NULL,
    unit_price DECIMAL(12, 2) NOT NULL,
    CONSTRAINT uq_order_item_request_item UNIQUE (request_item_id),
    CONSTRAINT fk_order_items_order FOREIGN KEY (order_id)
        REFERENCES orders(order_id) ON DELETE CASCADE,
    CONSTRAINT fk_order_items_request_item FOREIGN KEY (request_item_id)
        REFERENCES request_items(request_item_id) ON DELETE RESTRICT,
    CONSTRAINT fk_order_items_material FOREIGN KEY (material_id)
        REFERENCES materials(material_id) ON DELETE RESTRICT,
    CONSTRAINT chk_order_items_quantity CHECK (quantity > 0),
    CONSTRAINT chk_order_items_unit_price CHECK (unit_price > 0)
) ENGINE=InnoDB;

CREATE TABLE order_assignment_history (
    assignment_history_id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    previous_supplier_id INT NOT NULL,
    new_supplier_id INT NOT NULL,
    previous_total DECIMAL(14, 2) NOT NULL,
    new_total DECIMAL(14, 2) NOT NULL,
    previous_status ENUM('Confirmed', 'Cancelled') NOT NULL,
    changed_by INT NOT NULL,
    changed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_assignment_history_order FOREIGN KEY (order_id)
        REFERENCES orders(order_id) ON DELETE RESTRICT,
    CONSTRAINT fk_assignment_history_previous_supplier FOREIGN KEY (previous_supplier_id)
        REFERENCES users(user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_assignment_history_new_supplier FOREIGN KEY (new_supplier_id)
        REFERENCES users(user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_assignment_history_user FOREIGN KEY (changed_by)
        REFERENCES users(user_id) ON DELETE RESTRICT
) ENGINE=InnoDB;

CREATE TABLE order_assignment_history_items (
    assignment_history_item_id INT AUTO_INCREMENT PRIMARY KEY,
    assignment_history_id INT NOT NULL,
    previous_material_name VARCHAR(150) NOT NULL,
    previous_unit VARCHAR(40) NOT NULL,
    quantity INT NOT NULL,
    previous_unit_price DECIMAL(12, 2) NOT NULL,
    new_material_name VARCHAR(150) NOT NULL,
    new_unit VARCHAR(40) NOT NULL,
    new_unit_price DECIMAL(12, 2) NOT NULL,
    CONSTRAINT fk_assignment_history_items_history FOREIGN KEY (assignment_history_id)
        REFERENCES order_assignment_history(assignment_history_id) ON DELETE RESTRICT,
    CONSTRAINT chk_assignment_history_item_quantity CHECK (quantity > 0),
    CONSTRAINT chk_assignment_history_previous_price CHECK (previous_unit_price > 0),
    CONSTRAINT chk_assignment_history_new_price CHECK (new_unit_price > 0)
) ENGINE=InnoDB;

CREATE TABLE deliveries (
    delivery_id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL UNIQUE,
    delivery_address VARCHAR(500) NOT NULL,
    dispatch_date DATE,
    delivery_date DATE,
    delivery_status ENUM('Pending', 'In Transit', 'Delivered') NOT NULL DEFAULT 'Pending',
    CONSTRAINT fk_deliveries_order FOREIGN KEY (order_id)
        REFERENCES orders(order_id) ON DELETE CASCADE,
    CONSTRAINT chk_delivery_address CHECK (CHAR_LENGTH(TRIM(delivery_address)) > 0),
    CONSTRAINT chk_delivery_dates CHECK (
        delivery_date IS NULL OR dispatch_date IS NULL OR delivery_date >= dispatch_date
    )
) ENGINE=InnoDB;

-- Demo account password for all seeded users: Construction@123
-- Passwords use Werkzeug PBKDF2-SHA256 hashes.
INSERT INTO users (user_id, name, email, password_hash, role, phone) VALUES
(1, 'System Administrator', 'admin@construction.local',
 'pbkdf2:sha256:600000$nSbrtMGRC5HEvyEb$f8b9530a186cf387395379af3ac53437c2f8819f77f8884bf13cabc55b379895', 'Admin', '555-0100'),
(2, 'BuildRight Supplies', 'supplier1@construction.local',
 'pbkdf2:sha256:600000$nSbrtMGRC5HEvyEb$f8b9530a186cf387395379af3ac53437c2f8819f77f8884bf13cabc55b379895', 'Supplier', '555-0101'),
(3, 'NorthStar Materials', 'supplier2@construction.local',
 'pbkdf2:sha256:600000$nSbrtMGRC5HEvyEb$f8b9530a186cf387395379af3ac53437c2f8819f77f8884bf13cabc55b379895', 'Supplier', '555-0102'),
(4, 'Avery Construction', 'purchaser1@construction.local',
 'pbkdf2:sha256:600000$nSbrtMGRC5HEvyEb$f8b9530a186cf387395379af3ac53437c2f8819f77f8884bf13cabc55b379895', 'Purchaser', '555-0103'),
(5, 'Morgan Projects', 'purchaser2@construction.local',
 'pbkdf2:sha256:600000$nSbrtMGRC5HEvyEb$f8b9530a186cf387395379af3ac53437c2f8819f77f8884bf13cabc55b379895', 'Purchaser', '555-0104'),
(6, 'Site Engineer Demo', 'user@construction.local',
 'pbkdf2:sha256:600000$y1LpnnehQLnQsPCk$fdd6e8b859cfa6f02d0eb7328d2a9b5203c7bfbb95f6bbacdfca256de733a043', 'User', '555-0105');

INSERT INTO materials
(material_id, supplier_id, material_name, description, unit, price, stock_quantity) VALUES
(1, 2, 'Portland Cement', 'General purpose cement, 50 kg bag', 'bag', 8.75, 460),
(2, 2, 'Reinforcing Bar', 'Grade 60 steel rebar, 12 mm', 'piece', 12.40, 350),
(3, 2, 'Concrete Blocks', 'Standard hollow concrete block', 'piece', 1.85, 2000),
(4, 2, 'Washed Sand', 'Clean screened construction sand', 'ton', 32.00, 120),
(5, 3, 'Portland Cement', 'High-strength cement, 50 kg bag', 'bag', 8.50, 420),
(6, 3, 'Reinforcing Bar', 'Grade 60 steel rebar, 12 mm', 'piece', 12.75, 500),
(7, 3, 'Roofing Sheets', 'Galvanized corrugated sheet, 3 m', 'sheet', 18.90, 180),
(8, 3, 'Gravel Aggregate', 'Washed aggregate, 20 mm', 'ton', 28.50, 160);

INSERT INTO purchase_requests (request_id, purchaser_id, request_date, required_date, status) VALUES
(1, 4, CURRENT_TIMESTAMP, DATE_ADD(CURDATE(), INTERVAL 21 DAY), 'Approved'),
(2, 5, CURRENT_TIMESTAMP, DATE_ADD(CURDATE(), INTERVAL 30 DAY), 'Pending');

INSERT INTO request_items (
   request_id, material_id, material_name_snapshot, unit_snapshot,
   supplier_id, quantity, unit_price
) VALUES
(1, 1, 'Portland Cement', 'bag', 2, 40, 8.75),
(1, 5, 'Portland Cement', 'bag', 3, 30, 8.50),
(2, 3, 'Concrete Blocks', 'piece', 2, 200, 1.85),
(2, 7, 'Roofing Sheets', 'sheet', 3, 25, 18.90);

INSERT INTO orders (order_id, request_id, supplier_id, order_date, total_amount, order_status) VALUES
(1, 1, 2, CURRENT_TIMESTAMP, 350.00, 'Processing');

INSERT INTO order_items (
   order_id, request_item_id, material_id, material_name_snapshot,
   unit_snapshot, quantity, unit_price
) VALUES
(1, 1, 1, 'Portland Cement', 'bag', 40, 8.75);

INSERT INTO deliveries
(order_id, delivery_address, dispatch_date, delivery_date, delivery_status) VALUES
(1, '100 College Avenue, Building A', NULL, NULL, 'Pending');

ALTER TABLE users AUTO_INCREMENT = 7;
ALTER TABLE materials AUTO_INCREMENT = 9;
ALTER TABLE purchase_requests AUTO_INCREMENT = 3;
ALTER TABLE orders AUTO_INCREMENT = 2;
