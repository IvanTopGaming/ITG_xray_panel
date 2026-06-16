package store

import (
	"sync"

	"github.com/itg/metrics-agent/internal/model"
)

type seriesCache struct {
	mu sync.Mutex
	m  map[model.SeriesKey]int64
}

func (s *Store) SeriesID(k model.SeriesKey) (int64, error) {
	s.cache.mu.Lock()
	defer s.cache.mu.Unlock()
	if s.cache.m == nil {
		s.cache.m = make(map[model.SeriesKey]int64)
	}
	if id, ok := s.cache.m[k]; ok {
		return id, nil
	}
	if _, err := s.db.Exec(
		`INSERT OR IGNORE INTO series(metric,scope,entity) VALUES(?,?,?)`,
		k.Metric, k.Scope, k.Entity,
	); err != nil {
		return 0, err
	}
	var id int64
	if err := s.db.QueryRow(
		`SELECT series_id FROM series WHERE metric=? AND scope=? AND entity=?`,
		k.Metric, k.Scope, k.Entity,
	).Scan(&id); err != nil {
		return 0, err
	}
	s.cache.m[k] = id
	return id, nil
}

func (s *Store) AllSeries() (map[int64]model.SeriesKey, error) {
	rows, err := s.db.Query(`SELECT series_id, metric, scope, entity FROM series`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[int64]model.SeriesKey{}
	for rows.Next() {
		var id int64
		var k model.SeriesKey
		if err := rows.Scan(&id, &k.Metric, &k.Scope, &k.Entity); err != nil {
			return nil, err
		}
		out[id] = k
	}
	return out, rows.Err()
}
