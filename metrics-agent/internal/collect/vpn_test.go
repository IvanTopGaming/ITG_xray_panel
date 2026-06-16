package collect

import (
	"testing"

	"github.com/itg/metrics-agent/internal/model"
	"github.com/itg/metrics-agent/internal/store"
	"github.com/itg/metrics-agent/internal/xrayapi"
)

type fakeQuerier struct{ stats []*xrayapi.Stat }

func (f *fakeQuerier) QueryStats(string) ([]*xrayapi.Stat, error) { return f.stats, nil }

func TestSampleVPNWritesRatePerUser(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	q := &fakeQuerier{}
	v := &VPNSampler{Store: s, Xray: q}

	q.stats = []*xrayapi.Stat{{Name: "user>>>tg42>>>traffic>>>downlink", Value: 0}}
	if err := v.Sample(100); err != nil {
		t.Fatal(err)
	}
	q.stats = []*xrayapi.Stat{{Name: "user>>>tg42>>>traffic>>>downlink", Value: 10000}}
	if err := v.Sample(110); err != nil {
		t.Fatal(err)
	}
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricVPNDown, Scope: model.ScopeVPN, Entity: "tg42"})
	pts, _ := s.RangeSamples(id, 0, 200)
	if len(pts) != 1 || pts[0].Val != 1000 {
		t.Fatalf("vpn down series = %+v", pts)
	}
}

func TestSampleVPNResetDetection(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	q := &fakeQuerier{}
	v := &VPNSampler{Store: s, Xray: q}

	q.stats = []*xrayapi.Stat{{Name: "user>>>tg7>>>traffic>>>downlink", Value: 9000}}
	if err := v.Sample(100); err != nil {
		t.Fatal(err)
	}
	q.stats = []*xrayapi.Stat{{Name: "user>>>tg7>>>traffic>>>downlink", Value: 2000}}
	if err := v.Sample(110); err != nil {
		t.Fatal(err)
	}
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricVPNDown, Scope: model.ScopeVPN, Entity: "tg7"})
	pts, _ := s.RangeSamples(id, 0, 200)
	if len(pts) != 1 || pts[0].Val != 200 {
		t.Fatalf("reset detection = %+v", pts)
	}
}
