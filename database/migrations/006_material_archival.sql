-- Archive referenced supplier offers without deleting procurement history.
USE construction_management;

ALTER TABLE materials
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE AFTER stock_quantity,
    ADD INDEX idx_material_active_name (is_active, material_name);
