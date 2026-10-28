-- Preserve the material label and unit shown on a supplier order if a listing changes.
USE construction_management;

ALTER TABLE order_items
    ADD COLUMN material_name_snapshot VARCHAR(150) NULL AFTER material_id,
    ADD COLUMN unit_snapshot VARCHAR(40) NULL AFTER material_name_snapshot;

UPDATE order_items oi
JOIN materials m ON m.material_id = oi.material_id
SET oi.material_name_snapshot = m.material_name,
    oi.unit_snapshot = m.unit
WHERE oi.material_name_snapshot IS NULL OR oi.unit_snapshot IS NULL;

ALTER TABLE order_items
    MODIFY COLUMN material_name_snapshot VARCHAR(150) NOT NULL,
    MODIFY COLUMN unit_snapshot VARCHAR(40) NOT NULL;
