CREATE TABLE IF NOT EXISTS eta_observations (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    fetched_at_utc DATETIME(3) NOT NULL,
    stop_code      VARCHAR(16) NOT NULL,
    route_code     VARCHAR(16) NOT NULL,
    veh_code       VARCHAR(16) NOT NULL,
    eta_minutes    SMALLINT    NOT NULL,
    collector_version TINYINT NOT NULL DEFAULT 2,

    UNIQUE KEY unique_obs (fetched_at_utc, stop_code, route_code, veh_code),

    KEY idx_track (stop_code, route_code, veh_code, fetched_at_utc)
);

CREATE TABLE IF NOT EXISTS poll_log (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    fetched_at_utc DATETIME(3) NOT NULL,
    stop_code      VARCHAR(16) NOT NULL,
    status         ENUM('ok','empty','error') NOT NULL,
    KEY idx_time (fetched_at_utc)
);

