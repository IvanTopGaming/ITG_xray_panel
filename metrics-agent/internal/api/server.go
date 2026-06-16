package api

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/itg/metrics-agent/internal/model"
	"github.com/itg/metrics-agent/internal/store"
)

const snapshotStaleness = 60

type Server struct {
	store *store.Store
}

func New(s *store.Store) *Server {
	return &Server{store: s}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"status": "ok", "db_bytes": s.store.SizeBytes()})
	})
	mux.HandleFunc("/api/v1/series", s.handleSeries)
	mux.HandleFunc("/api/v1/series/raw", s.handleSeriesRaw)
	mux.HandleFunc("/api/v1/snapshot", s.handleSnapshot)
	return mux
}

func (s *Server) handleSeries(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	key := model.SeriesKey{Metric: q.Get("metric"), Scope: q.Get("scope"), Entity: q.Get("entity")}
	from, _ := strconv.ParseInt(q.Get("from"), 10, 64)
	to, _ := strconv.ParseInt(q.Get("to"), 10, 64)
	points, _ := strconv.ParseInt(q.Get("points"), 10, 64)
	id, err := s.store.SeriesID(key)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	pts, err := s.store.Series(id, from, to, points)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	writeJSON(w, map[string]any{"points": pts})
}

const maxRawWindow = 6 * 3600

func (s *Server) handleSeriesRaw(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	from, _ := strconv.ParseInt(q.Get("from"), 10, 64)
	to, _ := strconv.ParseInt(q.Get("to"), 10, 64)
	if to < from || to-from > maxRawWindow {
		http.Error(w, "window too large (max 6h)", http.StatusBadRequest)
		return
	}
	id, err := s.store.SeriesID(model.SeriesKey{Metric: q.Get("metric"), Scope: q.Get("scope"), Entity: q.Get("entity")})
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	pts, err := s.store.RawSeries(id, from, to)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	writeJSON(w, map[string]any{"points": pts})
}

type snapPoint struct {
	Metric string `json:"metric"`
	Scope  string `json:"scope"`
	Entity string `json:"entity"`
	Ts     int64  `json:"ts"`
	Value  int64  `json:"value"`
}

func (s *Server) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	latest, err := s.store.LatestSamples(time.Now().Unix() - snapshotStaleness)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	meta, err := s.store.AllSeries()
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	series := make([]snapPoint, 0, len(latest))
	for id, p := range latest {
		k := meta[id]
		series = append(series, snapPoint{Metric: k.Metric, Scope: k.Scope, Entity: k.Entity, Ts: p.Ts, Value: p.Val})
	}
	procs, err := s.store.LatestProcs(20)
	if err != nil {
		http.Error(w, err.Error(), 500)
		return
	}
	writeJSON(w, map[string]any{"series": series, "procs": procs})
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}
