CREATE TABLE IF NOT EXISTS tblruns (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    meeting_id BIGINT NOT NULL,
    race_date DATE NOT NULL,
    race_course VARCHAR(50) NOT NULL,
    race_courseid INT NULL,
    race_no INT NOT NULL,
    race_dist INT NULL,
    jockey_id BIGINT NOT NULL,
    jockey VARCHAR(100) NOT NULL,
    runner VARCHAR(100) NULL,
    finish_pos INT NULL,
    sp_price DECIMAL(10,2) NULL,
    INDEX idx_meeting_filter (race_date, race_course, race_courseid),
    INDEX idx_jockey_history (jockey_id, race_date)
);