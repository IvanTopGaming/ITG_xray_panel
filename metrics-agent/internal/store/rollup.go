package store

import "github.com/itg/metrics-agent/internal/model"

func (s *Store) BuildRollup1m(from, to int64) error {
	_, err := s.db.Exec(`
INSERT INTO rollup_1m(series_id,minute_ts,avg,max,min)
SELECT series_id, (ts/60)*60 AS m, AVG(value), MAX(value), MIN(value)
FROM sample WHERE ts>=? AND ts<? GROUP BY series_id, m
ON CONFLICT(series_id,minute_ts) DO UPDATE SET
  avg=excluded.avg, max=excluded.max, min=excluded.min`, from, to)
	return err
}

func (s *Store) BuildRollup1h(from, to int64) error {
	_, err := s.db.Exec(`
INSERT INTO rollup_1h(series_id,hour_ts,avg,max,min)
SELECT series_id, (minute_ts/3600)*3600 AS h,
       AVG(avg), MAX(max), MIN(min)
FROM rollup_1m WHERE minute_ts>=? AND minute_ts<? GROUP BY series_id, h
ON CONFLICT(series_id,hour_ts) DO UPDATE SET
  avg=excluded.avg, max=excluded.max, min=excluded.min`, from, to)
	return err
}

func scanAggs(rows interface {
	Next() bool
	Scan(...any) error
	Err() error
}) ([]model.Agg, error) {
	var out []model.Agg
	for rows.Next() {
		var a model.Agg
		if err := rows.Scan(&a.Ts, &a.Avg, &a.Min, &a.Max); err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

func (s *Store) RangeRollup1m(seriesID, from, to int64) ([]model.Agg, error) {
	rows, err := s.db.Query(
		`SELECT minute_ts,avg,min,max FROM rollup_1m WHERE series_id=? AND minute_ts>=? AND minute_ts<=? ORDER BY minute_ts`,
		seriesID, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanAggs(rows)
}

func (s *Store) RangeRollup1h(seriesID, from, to int64) ([]model.Agg, error) {
	rows, err := s.db.Query(
		`SELECT hour_ts,avg,min,max FROM rollup_1h WHERE series_id=? AND hour_ts>=? AND hour_ts<=? ORDER BY hour_ts`,
		seriesID, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanAggs(rows)
}
