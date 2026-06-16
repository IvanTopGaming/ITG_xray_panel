package collect

import (
	"strings"

	"github.com/itg/metrics-agent/internal/model"
	"github.com/itg/metrics-agent/internal/store"
	"github.com/itg/metrics-agent/internal/xrayapi"
)

type StatsQuerier interface {
	QueryStats(pattern string) ([]*xrayapi.Stat, error)
}

type VPNSampler struct {
	Store *store.Store
	Xray  StatsQuerier

	prev   map[string]int64
	prevTs int64
}

func parseUserStat(name string) (email, dir string, ok bool) {
	parts := strings.Split(name, ">>>")
	if len(parts) != 4 || parts[0] != "user" || parts[2] != "traffic" {
		return "", "", false
	}
	return parts[1], parts[3], true
}

func (v *VPNSampler) Sample(now int64) error {
	stats, err := v.Xray.QueryStats("user>>>")
	if err != nil {
		return err
	}
	cur := map[string]int64{}
	for _, st := range stats {
		email, dir, ok := parseUserStat(st.GetName())
		if !ok {
			continue
		}
		cur[dir+"|"+email] = st.GetValue()
	}
	if v.prev != nil && v.prevTs != 0 && now > v.prevTs {
		dt := now - v.prevTs
		for k, val := range cur {
			prev := v.prev[k]
			d := val - prev
			if val < prev {
				d = val
			}
			rate := d / dt
			dir, email, _ := strings.Cut(k, "|")
			metric := model.MetricVPNDown
			if dir == "uplink" {
				metric = model.MetricVPNUp
			}
			id, err := v.Store.SeriesID(model.SeriesKey{Metric: metric, Scope: model.ScopeVPN, Entity: email})
			if err != nil {
				return err
			}
			if err := v.Store.InsertSamples([]store.Reading{{SeriesID: id, Ts: now, Val: rate}}); err != nil {
				return err
			}
		}
	}
	v.prev, v.prevTs = cur, now
	return nil
}
