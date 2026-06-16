package store

import "github.com/itg/metrics-agent/internal/model"

type ProcRow struct {
	PID      int    `json:"pid"`
	Comm     string `json:"comm"`
	CPUPct   int64  `json:"cpu_pct"`
	RSSBytes int64  `json:"rss_bytes"`
}

func (s *Store) InsertProcs(ts int64, rows []ProcRow) error {
	if len(rows) == 0 {
		return nil
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT INTO proc_sample(ts,pid,comm,cpu_pct,rss_bytes) VALUES(?,?,?,?,?)`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, r := range rows {
		if _, err := stmt.Exec(ts, r.PID, r.Comm, r.CPUPct, r.RSSBytes); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *Store) LatestProcs(n int) ([]ProcRow, error) {
	var maxTs int64
	if err := s.db.QueryRow(`SELECT COALESCE(MAX(ts),0) FROM proc_sample`).Scan(&maxTs); err != nil {
		return nil, err
	}
	rows, err := s.db.Query(
		`SELECT pid,comm,cpu_pct,rss_bytes FROM proc_sample WHERE ts=? ORDER BY cpu_pct DESC LIMIT ?`, maxTs, n)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ProcRow
	for rows.Next() {
		var r ProcRow
		if err := rows.Scan(&r.PID, &r.Comm, &r.CPUPct, &r.RSSBytes); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *Store) LatestSamples(since int64) (map[int64]model.Point, error) {
	rows, err := s.db.Query(`
SELECT s.series_id, s.ts, s.value FROM sample s
JOIN (SELECT series_id, MAX(ts) AS mts FROM sample GROUP BY series_id) m
  ON s.series_id=m.series_id AND s.ts=m.mts
WHERE s.ts >= ?`, since)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]model.Point{}
	for rows.Next() {
		var id int64
		var p model.Point
		if err := rows.Scan(&id, &p.Ts, &p.Val); err != nil {
			return nil, err
		}
		out[id] = p
	}
	return out, rows.Err()
}
