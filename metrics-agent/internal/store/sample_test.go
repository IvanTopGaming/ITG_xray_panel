package store

import (
	"testing"

	"github.com/itg/metrics-agent/internal/model"
)

func TestInsertAndRangeSamples(t *testing.T) {
	s := openTest(t)
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost})
	batch := []Reading{
		{SeriesID: id, Ts: 100, Val: 10},
		{SeriesID: id, Ts: 101, Val: 20},
		{SeriesID: id, Ts: 102, Val: 30},
	}
	if err := s.InsertSamples(batch); err != nil {
		t.Fatal(err)
	}
	got, err := s.RangeSamples(id, 101, 102)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != (model.Point{Ts: 101, Val: 20}) || got[1] != (model.Point{Ts: 102, Val: 30}) {
		t.Fatalf("range = %+v", got)
	}
}
