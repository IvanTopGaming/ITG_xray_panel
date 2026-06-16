package store

import (
	"testing"

	"github.com/itg/metrics-agent/internal/model"
)

func TestRollup1m(t *testing.T) {
	s := openTest(t)
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricNetHostRx, Scope: model.ScopeHost})
	s.InsertSamples([]Reading{
		{id, 0, 10}, {id, 5, 30}, {id, 59, 20},
		{id, 60, 100},
	})
	if err := s.BuildRollup1m(0, 120); err != nil {
		t.Fatal(err)
	}
	got, err := s.RangeRollup1m(id, 0, 59)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 {
		t.Fatalf("want 1 bucket, got %d", len(got))
	}
	a := got[0]
	if a.Ts != 0 || a.Avg != 20 || a.Min != 10 || a.Max != 30 {
		t.Fatalf("agg = %+v", a)
	}
}
