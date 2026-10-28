-- Keep the purchaser's chosen supplier offer stable if the catalog listing changes.
USE construction_management;

ALTER TABLE request_items
    ADD COLUMN supplier_id INT NULL AFTER material_id;

UPDATE request_items ri
JOIN materials m ON m.material_id = ri.material_id
SET ri.supplier_id = m.supplier_id
WHERE ri.supplier_id IS NULL;

ALTER TABLE request_items
    MODIFY COLUMN supplier_id INT NOT NULL,
    ADD CONSTRAINT fk_request_items_supplier FOREIGN KEY (supplier_id)
        REFERENCES users(user_id) ON DELETE RESTRICT;
