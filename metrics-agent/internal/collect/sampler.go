package collect

import (
	"github.com/itg/metrics-agent/internal/model"
	"github.com/itg/metrics-agent/internal/store"
)

type Sampler struct {
	Store  *store.Store
	Proc   *Proc
	Cgroup *Cgroup
	Procs  *Procs

	prevNetRx, prevNetTx int64
	prevNetTs            int64

	prevCPUTotal, prevCPUIdle int64
	cpuSeeded                 bool

	prevCtrCPU map[string]int64
	prevCtrIO  map[string]int64
	prevCtrTs  map[string]int64

	prevDiskRead, prevDiskWrite, prevDiskTs int64
	diskSeeded                              bool

	prevProcJiffies map[int]int64
	prevProcCPUTot  int64
	procSeeded      bool
}

func (s *Sampler) write(now int64, k model.SeriesKey, val int64) error {
	id, err := s.Store.SeriesID(k)
	if err != nil {
		return err
	}
	return s.Store.InsertSamples([]store.Reading{{SeriesID: id, Ts: now, Val: val}})
}

func (s *Sampler) SampleHost(now int64) error {
	m, err := s.Proc.Mem()
	if err != nil {
		return err
	}
	if err := s.write(now, model.SeriesKey{Metric: model.MetricRAMHost, Scope: model.ScopeHost}, m.UsedBytes); err != nil {
		return err
	}
	return nil
}

func (s *Sampler) SampleCPU(now int64) error {
	c, err := s.Proc.CPU()
	if err != nil {
		return err
	}
	if s.cpuSeeded {
		dt := c.Total - s.prevCPUTotal
		di := c.Idle - s.prevCPUIdle
		if dt > 0 {
			busy := dt - di
			pct := busy * 10000 / dt
			if err := s.write(now, model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost}, pct); err != nil {
				return err
			}
		}
	}
	s.prevCPUTotal, s.prevCPUIdle, s.cpuSeeded = c.Total, c.Idle, true
	return nil
}

func (s *Sampler) SampleNet(now int64) error {
	rx, tx, err := s.Proc.NetTotals()
	if err != nil {
		return err
	}
	if s.prevNetTs != 0 && now > s.prevNetTs {
		dt := now - s.prevNetTs
		drx := (rx - s.prevNetRx) / dt
		dtx := (tx - s.prevNetTx) / dt
		if drx < 0 {
			drx = 0
		}
		if dtx < 0 {
			dtx = 0
		}
		if err := s.write(now, model.SeriesKey{Metric: model.MetricNetHostRx, Scope: model.ScopeHost}, drx); err != nil {
			return err
		}
		if err := s.write(now, model.SeriesKey{Metric: model.MetricNetHostTx, Scope: model.ScopeHost}, dtx); err != nil {
			return err
		}
	}
	s.prevNetRx, s.prevNetTx, s.prevNetTs = rx, tx, now
	return nil
}

func (s *Sampler) SampleContainers(now int64) error {
	if s.Cgroup == nil {
		return nil
	}
	if s.prevCtrCPU == nil {
		s.prevCtrCPU, s.prevCtrIO, s.prevCtrTs = map[string]int64{}, map[string]int64{}, map[string]int64{}
	}
	ids, err := s.Cgroup.Containers()
	if err != nil {
		return err
	}
	for _, id := range ids {
		st, err := s.Cgroup.Stats(id)
		if err != nil {
			return err
		}
		if err := s.write(now, model.SeriesKey{Metric: model.MetricRAMCtr, Scope: model.ScopeContainer, Entity: id}, st.MemBytes); err != nil {
			return err
		}
		if pts, ok := s.prevCtrTs[id]; ok && now > pts {
			dt := now - pts
			if rate := (st.CPUUsec - s.prevCtrCPU[id]) / dt; rate >= 0 {
				if err := s.write(now, model.SeriesKey{Metric: model.MetricCPUCtr, Scope: model.ScopeContainer, Entity: id}, rate); err != nil {
					return err
				}
			}
			if rate := (st.IOBytes - s.prevCtrIO[id]) / dt; rate >= 0 {
				if err := s.write(now, model.SeriesKey{Metric: model.MetricIOCtr, Scope: model.ScopeContainer, Entity: id}, rate); err != nil {
					return err
				}
			}
		}
		s.prevCtrCPU[id], s.prevCtrIO[id], s.prevCtrTs[id] = st.CPUUsec, st.IOBytes, now
	}
	return nil
}

func (s *Sampler) SampleDisk(now int64) error {
	rd, wr, err := s.Proc.DiskIOBytes()
	if err != nil {
		return err
	}
	if s.diskSeeded && now > s.prevDiskTs {
		dt := now - s.prevDiskTs
		if r := (rd - s.prevDiskRead) / dt; r >= 0 {
			if err := s.write(now, model.SeriesKey{Metric: model.MetricDiskIO, Scope: model.ScopeDisk, Entity: "read"}, r); err != nil {
				return err
			}
		}
		if w := (wr - s.prevDiskWrite) / dt; w >= 0 {
			if err := s.write(now, model.SeriesKey{Metric: model.MetricDiskIO, Scope: model.ScopeDisk, Entity: "write"}, w); err != nil {
				return err
			}
		}
	}
	s.prevDiskRead, s.prevDiskWrite, s.prevDiskTs, s.diskSeeded = rd, wr, now, true
	return nil
}

func (s *Sampler) SampleProcs(now int64) error {
	cpu, err := s.Proc.CPU()
	if err != nil {
		return err
	}
	infos, err := s.Procs.Snapshot(64)
	if err != nil {
		return err
	}
	cur := map[int]int64{}
	for _, in := range infos {
		cur[in.PID] = in.CPUJiffies
	}
	if s.procSeeded {
		totDelta := cpu.Total - s.prevProcCPUTot
		var rows []store.ProcRow
		for _, in := range infos {
			var pct int64
			if totDelta > 0 {
				pct = (in.CPUJiffies - s.prevProcJiffies[in.PID]) * 10000 / totDelta
				if pct < 0 {
					pct = 0
				}
			}
			rows = append(rows, store.ProcRow{PID: in.PID, Comm: in.Comm, CPUPct: pct, RSSBytes: in.RSSBytes})
		}
		if err := s.Store.InsertProcs(now, rows); err != nil {
			return err
		}
	}
	s.prevProcJiffies, s.prevProcCPUTot, s.procSeeded = cur, cpu.Total, true
	return nil
}
