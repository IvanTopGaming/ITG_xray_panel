package store

import "github.com/itg/metrics-agent/internal/model"

func (s *Store) Series(seriesID, from, to, points int64) ([]model.Agg, error) {
	if points <= 0 {
		points = 300
	}
	span := to - from
	if span < 1 {
		span = 1
	}
	bucket := span / points
	if bucket < 1 {
		bucket = 1
	}

	var q string
	switch {
	case bucket < 60:
		q = `SELECT (ts/?)*?, AVG(value), MIN(value), MAX(value) FROM sample
WHERE series_id=? AND ts>=? AND ts<=? GROUP BY ts/? ORDER BY 1`
	case bucket < 3600:
		q = `SELECT (minute_ts/?)*?, AVG(avg), MIN(min), MAX(max) FROM rollup_1m
WHERE series_id=? AND minute_ts>=? AND minute_ts<=? GROUP BY minute_ts/? ORDER BY 1`
	default:
		q = `SELECT (hour_ts/?)*?, AVG(avg), MIN(min), MAX(max) FROM rollup_1h
WHERE series_id=? AND hour_ts>=? AND hour_ts<=? GROUP BY hour_ts/? ORDER BY 1`
	}

	rows, err := s.db.Query(q, bucket, bucket, seriesID, from, to, bucket)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanAggs(rows)
}

func (s *Store) RawSeries(seriesID, from, to int64) ([]model.Point, error) {
	hot, err := s.RangeSamples(seriesID, from, to)
	if err != nil {
		return nil, err
	}
	cold, err := s.DecodeArchiveRange(seriesID, from, to)
	if err != nil {
		return nil, err
	}
	return append(cold, hot...), nil
}
