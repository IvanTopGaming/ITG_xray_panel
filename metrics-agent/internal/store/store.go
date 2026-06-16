package store

import (
	"database/sql"

	_ "modernc.org/sqlite"
)

type Store struct {
	db    *sql.DB
	cache seriesCache
}

const schema = `
CREATE TABLE IF NOT EXISTS series (
  series_id INTEGER PRIMARY KEY,
  metric TEXT NOT NULL, scope TEXT NOT NULL, entity TEXT NOT NULL,
  UNIQUE(metric, scope, entity)
);
CREATE TABLE IF NOT EXISTS sample (
  series_id INTEGER NOT NULL, ts INTEGER NOT NULL, value INTEGER NOT NULL,
  PRIMARY KEY (series_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS raw_archive (
  series_id INTEGER NOT NULL, hour_ts INTEGER NOT NULL,
  n INTEGER NOT NULL, codec INTEGER NOT NULL, data BLOB NOT NULL,
  PRIMARY KEY (series_id, hour_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rollup_1m (
  series_id INTEGER NOT NULL, minute_ts INTEGER NOT NULL,
  avg REAL NOT NULL, max INTEGER NOT NULL, min INTEGER NOT NULL,
  PRIMARY KEY (series_id, minute_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rollup_1h (
  series_id INTEGER NOT NULL, hour_ts INTEGER NOT NULL,
  avg REAL NOT NULL, max INTEGER NOT NULL, min INTEGER NOT NULL,
  PRIMARY KEY (series_id, hour_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS proc_sample (
  ts INTEGER NOT NULL, pid INTEGER NOT NULL,
  comm TEXT NOT NULL, cpu_pct INTEGER NOT NULL, rss_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_proc_ts ON proc_sample(ts);
`

func Open(path string) (*Store, error) {
	dsn := path + "?_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=busy_timeout(5000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) SizeBytes() int64 {
	var page, count int64
	s.db.QueryRow(`PRAGMA page_size`).Scan(&page)
	s.db.QueryRow(`PRAGMA page_count`).Scan(&count)
	return page * count
}
