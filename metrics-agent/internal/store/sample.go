package store

import "github.com/itg/metrics-agent/internal/model"

type Reading struct {
	SeriesID int64
	Ts       int64
	Val      int64
}

func (s *Store) InsertSamples(rs []Reading) error {
	if len(rs) == 0 {
		return nil
	}
	tx, err := s.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	stmt, err := tx.Prepare(`INSERT OR REPLACE INTO sample(series_id,ts,value) VALUES(?,?,?)`)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, r := range rs {
		if _, err := stmt.Exec(r.SeriesID, r.Ts, r.Val); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s *Store) RangeSamples(seriesID, from, to int64) ([]model.Point, error) {
	rows, err := s.db.Query(
		`SELECT ts,value FROM sample WHERE series_id=? AND ts>=? AND ts<=? ORDER BY ts`,
		seriesID, from, to,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Point
	for rows.Next() {
		var p model.Point
		if err := rows.Scan(&p.Ts, &p.Val); err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}
