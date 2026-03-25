-- Table: app_activity_daily
CREATE TABLE app_activity_daily (
    id              NUMBER          GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- When & which app
    activity_date   DATE            NOT NULL,
    app_name        VARCHAR2(200)   NOT NULL,

    -- Accumulated time (seconds)
    total_seconds   NUMBER   NOT NULL,
    active_seconds  NUMBER   NOT NULL,
    session_count   NUMBER       NOT NULL,

    -- Who & where
    username        VARCHAR2(100)   NOT NULL,
    device_name     VARCHAR2(200),
    os              VARCHAR2(400),

    -- Bookend timestamps across all batches for this app on this day
    first_seen      TIMESTAMP       NOT NULL,
    last_seen       TIMESTAMP       NOT NULL,
);

-- MERGE key: one row per (user, machine, date, app).
ALTER TABLE app_activity_daily
    ADD CONSTRAINT uq_activity_daily
    UNIQUE (username, device_name, activity_date, app_name);

-- Indexes for dashboard queries (filter by user / date / app)
CREATE INDEX idx_aad_username      ON app_activity_daily (username);
CREATE INDEX idx_aad_activity_date ON app_activity_daily (activity_date);
CREATE INDEX idx_aad_app_name      ON app_activity_daily (app_name);

-- ============================================================
-- Reference: OLD table (kept for historical data migration)
-- ============================================================
-- CREATE TABLE app_usage_sessions (
--     id               NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
--     start_time       TIMESTAMP,
--     end_time         TIMESTAMP,
--     duration_seconds NUMBER(10, 2),
--     active_seconds   NUMBER(10, 2),
--     window_title     VARCHAR2(500),
--     app_process      VARCHAR2(200),
--     app_name         VARCHAR2(200),
--     pid              NUMBER,
--     hwnd             NUMBER,
--     device_name      VARCHAR2(200),
--     username         VARCHAR2(100),
--     machine_guid     VARCHAR2(100),
--     system_uuid      VARCHAR2(100),
--     os               VARCHAR2(400)
-- );
