package store

import (
	"testing"

	"github.com/itg/metrics-agent/internal/model"
)

func TestCompactPacksAndDeletes(t *testing.T) {
	s := openTest(t)
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricNetHostRx, Scope: model.ScopeHost})
	s.InsertSamples([]Reading{
		{id, 3600, 100}, {id, 3601, 110}, {id, 3700, 90},
		{id, 8000, 5},
	})
	if err := s.Compact(7200); err != nil {
		t.Fatal(err)
	}
	left, _ := s.RangeSamples(id, 0, 999999)
	if len(left) != 1 || left[0].Ts != 8000 {
		t.Fatalf("hot remainder = %+v", left)
	}
	pts, err := s.DecodeArchiveRange(id, 0, 7200)
	if err != nil {
		t.Fatal(err)
	}
	want := []model.Point{{Ts: 3600, Val: 100}, {Ts: 3601, Val: 110}, {Ts: 3700, Val: 90}}
	if len(pts) != 3 {
		t.Fatalf("decoded %d points: %+v", len(pts), pts)
	}
	for i := range want {
		if pts[i] != want[i] {
			t.Fatalf("point %d: %+v != %+v", i, pts[i], want[i])
		}
	}
}

func TestCompactKeepsBoundaryHourHot(t *testing.T) {
	s := openTest(t)
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricNetHostRx, Scope: model.ScopeHost})
	s.InsertSamples([]Reading{{id, 3601, 1}, {id, 5401, 2}, {id, 7000, 3}})
	if err := s.Compact(5400); err != nil {
		t.Fatal(err)
	}
	left, _ := s.RangeSamples(id, 0, 999999)
	if len(left) != 3 {
		t.Fatalf("boundary hour must stay hot until fully elapsed, got %+v", left)
	}
}
