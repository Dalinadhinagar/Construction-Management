-- Preserve the request lines, supplier offer, and agreed price on each order.
-- Safe to apply to an existing database; existing orders and request rows remain intact.
USE construction_management;

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    request_item_id INT NOT NULL,
    material_id INT NOT NULL,
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

CREATE TABLE IF NOT EXISTS order_assignment_history (
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

CREATE TABLE IF NOT EXISTS order_assignment_history_items (
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

INSERT INTO order_items (order_id, request_item_id, material_id, quantity, unit_price)
SELECT o.order_id, ri.request_item_id, ri.material_id, ri.quantity, ri.unit_price
FROM orders o
JOIN request_items ri ON ri.request_id = o.request_id
JOIN materials m ON m.material_id = ri.material_id AND m.supplier_id = o.supplier_id
WHERE NOT EXISTS (
    SELECT 1 FROM order_items oi WHERE oi.request_item_id = ri.request_item_id
);
