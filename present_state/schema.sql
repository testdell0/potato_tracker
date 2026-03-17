-- ============================================================
-- Potato Tracker — Oracle Schema
-- Table: app_activity_daily
--
-- Design rationale
-- ----------------
-- One row per (username, device_name, activity_date, app_name).
-- The tracker clubs all window-focus periods for the same app
-- within a calendar day into this single record before inserting.
-- Only records with total_seconds >= 300 (5 minutes) are pushed.
-- ============================================================

CREATE TABLE app_activity_daily (
    id              NUMBER          GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- When & which app
    activity_date   DATE            NOT NULL,   -- calendar date (no time part)
    app_name        VARCHAR2(200)   NOT NULL,   -- friendly/normalized app name

    -- Who & where
    username        VARCHAR2(100)   NOT NULL,
    device_name     VARCHAR2(200),
    machine_guid    VARCHAR2(100),
    system_uuid     VARCHAR2(100),
    os              VARCHAR2(400),

    -- Accumulated time (seconds) across all focus windows for that app+day
    total_seconds   NUMBER(10, 2)   NOT NULL,   -- wall-clock time in foreground
    active_seconds  NUMBER(10, 2)   NOT NULL,   -- subset where keyboard/mouse used
    session_count   NUMBER(6)       NOT NULL,   -- number of focus windows clubbed

    -- Bookend timestamps (first & last focus window for this app on this day)
    first_seen      TIMESTAMP       NOT NULL,
    last_seen       TIMESTAMP       NOT NULL,

    -- Audit
    created_at      TIMESTAMP       DEFAULT SYSTIMESTAMP NOT NULL
);

-- Prevent duplicate rows for the same (user, machine, date, app).
-- If the tracker is ever restarted mid-day and replays data,
-- enforce uniqueness at the DB level.
ALTER TABLE app_activity_daily
    ADD CONSTRAINT uq_activity_daily
    UNIQUE (username, device_name, activity_date, app_name);

-- Useful indexes for dashboard queries (filter by user/date)
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
