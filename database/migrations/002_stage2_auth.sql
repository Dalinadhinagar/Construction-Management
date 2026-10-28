-- Stage 2 authentication migration. Safe to re-run; preserves existing accounts.
USE construction_management;

ALTER TABLE users
    MODIFY COLUMN role ENUM('Admin', 'Supplier', 'Purchaser', 'User') NOT NULL;

SET @has_is_active = (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'users'
      AND COLUMN_NAME = 'is_active'
);
SET @add_is_active_sql = IF(
    @has_is_active = 0,
    'ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE AFTER phone',
    'SELECT 1'
);
PREPARE add_is_active_stmt FROM @add_is_active_sql;
EXECUTE add_is_active_stmt;
DEALLOCATE PREPARE add_is_active_stmt;

-- Demo account password: Construction@123
INSERT INTO users (name, email, password_hash, role, phone)
SELECT
    'Site Engineer Demo',
    'user@construction.local',
    'pbkdf2:sha256:600000$y1LpnnehQLnQsPCk$fdd6e8b859cfa6f02d0eb7328d2a9b5203c7bfbb95f6bbacdfca256de733a043',
    'User',
    '555-0105'
WHERE NOT EXISTS (
    SELECT 1 FROM users WHERE email = 'user@construction.local'
);
