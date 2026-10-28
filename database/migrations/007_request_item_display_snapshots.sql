-- Preserve the material name and unit selected on each request line.
USE construction_management;

ALTER TABLE request_items
    ADD COLUMN material_name_snapshot VARCHAR(150) NULL AFTER material_id,
    ADD COLUMN unit_snapshot VARCHAR(40) NULL AFTER material_name_snapshot;

UPDATE request_items ri
JOIN materials m ON m.material_id = ri.material_id
SET ri.material_name_snapshot = m.material_name,
    ri.unit_snapshot = m.unit
WHERE ri.material_name_snapshot IS NULL OR ri.unit_snapshot IS NULL;

ALTER TABLE request_items
    MODIFY COLUMN material_name_snapshot VARCHAR(150) NOT NULL,
    MODIFY COLUMN unit_snapshot VARCHAR(40) NOT NULL;
