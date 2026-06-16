package store

import (
	"github.com/itg/metrics-agent/internal/codec"
	"github.com/itg/metrics-agent/internal/model"
)

func (s *Store) Compact(cutoff int64) error {
	cutoff = (cutoff / 3600) * 3600
	rows, err := s.db.Query(
		`SELECT DISTINCT series_id, (ts/3600)*3600 AS h
		 FROM sample WHERE ts < ? ORDER BY series_id, h`, cutoff)
	if err != nil {
		return err
	}
	type bucket struct{ sid, hour int64 }
	var buckets []bucket
	for rows.Next() {
		var b bucket
		if err := rows.Scan(&b.sid, &b.hour); err != nil {
			rows.Close()
			return err
		}
		buckets = append(buckets, b)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	for _, b := range buckets {
		pts, err := s.RangeSamples(b.sid, b.hour, b.hour+3599)
		if err != nil {
			return err
		}
		if len(pts) == 0 {
			continue
		}
		blob, err := codec.Encode(pts)
		if err != nil {
			return err
		}
		tx, err := s.db.Begin()
		if err != nil {
			return err
		}
		if _, err := tx.Exec(
			`INSERT OR REPLACE INTO raw_archive(series_id,hour_ts,n,codec,data) VALUES(?,?,?,?,?)`,
			b.sid, b.hour, len(pts), codec.Codec, blob); err != nil {
			tx.Rollback()
			return err
		}
		if _, err := tx.Exec(
			`DELETE FROM sample WHERE series_id=? AND ts>=? AND ts<=?`,
			b.sid, b.hour, b.hour+3599); err != nil {
			tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) DecodeArchiveRange(seriesID, from, to int64) ([]model.Point, error) {
	rows, err := s.db.Query(
		`SELECT n,data FROM raw_archive WHERE series_id=? AND hour_ts+3599>=? AND hour_ts<=? ORDER BY hour_ts`,
		seriesID, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []model.Point
	for rows.Next() {
		var n int
		var data []byte
		if err := rows.Scan(&n, &data); err != nil {
			return nil, err
		}
		pts, err := codec.Decode(data, n)
		if err != nil {
			return nil, err
		}
		for _, p := range pts {
			if p.Ts >= from && p.Ts <= to {
				out = append(out, p)
			}
		}
	}
	return out, rows.Err()
}
