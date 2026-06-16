package store

import (
	"testing"

	"github.com/itg/metrics-agent/internal/model"
)

func TestSeriesPicksHotForSmallWindow(t *testing.T) {
	s := openTest(t)
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost})
	s.InsertSamples([]Reading{{id, 100, 7}, {id, 101, 9}})
	got, err := s.Series(id, 100, 101, 3600)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0].Avg != 7 || got[1].Max != 9 {
		t.Fatalf("hot series = %+v", got)
	}
}

func TestSeriesPicksRollupForLargeWindow(t *testing.T) {
	s := openTest(t)
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost})
	s.InsertSamples([]Reading{{id, 0, 10}, {id, 30, 20}})
	s.BuildRollup1m(0, 60)
	got, err := s.Series(id, 0, 100000, 60)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Avg != 15 || got[0].Min != 10 || got[0].Max != 20 {
		t.Fatalf("rollup series = %+v", got)
	}
}
