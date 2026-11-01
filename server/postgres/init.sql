-- Bubble Craps database schema
-- Auto-run by Postgres on first container start

CREATE TABLE IF NOT EXISTS rolls (
    roll_id         text PRIMARY KEY,
    device_id       text NOT NULL,
    die1            integer,
    die2            integer,
    total           integer,
    roll_status     text NOT NULL,
    roll_status_reason text,
    attempts        jsonb,
    motor_temp_c    integer,
    image_path      text,
    timestamp       timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rolls_device_id ON rolls (device_id);
CREATE INDEX IF NOT EXISTS idx_rolls_timestamp ON rolls (timestamp);
CREATE INDEX IF NOT EXISTS idx_rolls_roll_status ON rolls (roll_status);
