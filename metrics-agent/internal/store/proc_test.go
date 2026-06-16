package store

import (
	"testing"

	"github.com/itg/metrics-agent/internal/model"
)

func TestInsertAndLatestProcs(t *testing.T) {
	s := openTest(t)
	s.InsertProcs(100, []ProcRow{{PID: 1, Comm: "xray", CPUPct: 2200, RSSBytes: 1000}})
	s.InsertProcs(200, []ProcRow{{PID: 1, Comm: "xray", CPUPct: 1500, RSSBytes: 1200}})
	rows, err := s.LatestProcs(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].Comm != "xray" || rows[0].CPUPct != 1500 {
		t.Fatalf("latest procs = %+v", rows)
	}
}

func TestLatestSamples(t *testing.T) {
	s := openTest(t)
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost})
	s.InsertSamples([]Reading{{id, 100, 5}, {id, 200, 9}})
	m, err := s.LatestSamples(0)
	if err != nil {
		t.Fatal(err)
	}
	if m[id].Val != 9 || m[id].Ts != 200 {
		t.Fatalf("latest = %+v", m)
	}
}
