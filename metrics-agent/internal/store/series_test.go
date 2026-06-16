package store

import (
	"testing"

	"github.com/itg/metrics-agent/internal/model"
)

func TestSeriesIDStableAndCached(t *testing.T) {
	s := openTest(t)
	k := model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost, Entity: ""}
	id1, err := s.SeriesID(k)
	if err != nil {
		t.Fatal(err)
	}
	id2, err := s.SeriesID(k)
	if err != nil {
		t.Fatal(err)
	}
	if id1 != id2 {
		t.Fatalf("ids differ: %d != %d", id1, id2)
	}
	other, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricRAMHost, Scope: model.ScopeHost})
	if other == id1 {
		t.Fatal("distinct keys share id")
	}
}
