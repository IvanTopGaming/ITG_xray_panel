package api

import (
	"encoding/json"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/itg/metrics-agent/internal/model"
	"github.com/itg/metrics-agent/internal/store"
)

func TestSeriesHandler(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost})
	s.InsertSamples([]store.Reading{{SeriesID: id, Ts: 100, Val: 42}})

	srv := New(s)
	req := httptest.NewRequest("GET",
		"/api/v1/series?metric=cpu_host&scope=host&entity=&from=0&to=200", nil)
	req.Header.Set("Authorization", "Bearer secret")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)

	if rec.Code != 200 {
		t.Fatalf("code = %d body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Points []model.Agg `json:"points"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Points) != 1 || resp.Points[0].Avg != 42 {
		t.Fatalf("points = %+v", resp.Points)
	}
}

func TestHealthzNoAuth(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	srv := New(s)
	req := httptest.NewRequest("GET", "/healthz", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("healthz code = %d", rec.Code)
	}
}

func TestSeriesRawWindowCap(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	srv := New(s)
	req := httptest.NewRequest("GET", "/api/v1/series/raw?metric=cpu_host&scope=host&entity=&from=0&to=99999", nil)
	req.Header.Set("Authorization", "Bearer secret")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != 400 {
		t.Fatalf("expected 400 for >6h window, got %d", rec.Code)
	}
}

func TestSeriesRawReturnsPoints(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost})
	s.InsertSamples([]store.Reading{{SeriesID: id, Ts: 100, Val: 7}})
	srv := New(s)
	req := httptest.NewRequest("GET", "/api/v1/series/raw?metric=cpu_host&scope=host&entity=&from=0&to=200", nil)
	req.Header.Set("Authorization", "Bearer secret")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("code=%d", rec.Code)
	}
}

func TestHealthzReportsDBSize(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	srv := New(s)
	req := httptest.NewRequest("GET", "/healthz", nil)
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	var resp map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if _, ok := resp["db_bytes"]; !ok {
		t.Fatalf("healthz missing db_bytes: %v", resp)
	}
}

func TestSnapshotHandler(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost})
	now := time.Now().Unix()
	s.InsertSamples([]store.Reading{{SeriesID: id, Ts: now, Val: 4200}})
	s.InsertProcs(now, []store.ProcRow{{PID: 1, Comm: "xray", CPUPct: 2200, RSSBytes: 1000}})

	srv := New(s)
	req := httptest.NewRequest("GET", "/api/v1/snapshot", nil)
	req.Header.Set("Authorization", "Bearer secret")
	rec := httptest.NewRecorder()
	srv.Handler().ServeHTTP(rec, req)
	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Series []struct {
			Metric, Scope, Entity string
			Value                 int64
		} `json:"series"`
		Procs []struct {
			Comm   string
			CPUPct int64 `json:"cpu_pct"`
		} `json:"procs"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Series) != 1 || resp.Series[0].Value != 4200 || resp.Series[0].Metric != "cpu_host" {
		t.Fatalf("series = %+v", resp.Series)
	}
	if len(resp.Procs) != 1 || resp.Procs[0].Comm != "xray" {
		t.Fatalf("procs = %+v", resp.Procs)
	}
}
