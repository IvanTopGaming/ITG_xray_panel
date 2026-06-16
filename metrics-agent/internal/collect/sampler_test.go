package collect

import (
	"os"
	"testing"

	"github.com/itg/metrics-agent/internal/model"
	"github.com/itg/metrics-agent/internal/store"
)

func TestSampleCPUPercent(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	smp := &Sampler{Store: s, Proc: &Proc{Root: os.DirFS("testdata/proc")}}
	if err := smp.SampleCPU(100); err != nil {
		t.Fatal(err)
	}
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricCPUHost, Scope: model.ScopeHost})
	if pts, _ := s.RangeSamples(id, 0, 200); len(pts) != 0 {
		t.Fatalf("first call must seed only, got %+v", pts)
	}
	smp.prevCPUTotal, smp.prevCPUIdle = 900, 660
	if err := smp.SampleCPU(102); err != nil {
		t.Fatal(err)
	}
	pts, _ := s.RangeSamples(id, 0, 200)
	if len(pts) != 1 || pts[0].Val != 6000 {
		t.Fatalf("cpu pct = %+v", pts)
	}
}

func TestSampleOnceWritesHostSeries(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	smp := &Sampler{
		Store: s,
		Proc:  &Proc{Root: os.DirFS("testdata/proc")},
	}
	if err := smp.SampleHost(1000); err != nil {
		t.Fatal(err)
	}
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricRAMHost, Scope: model.ScopeHost})
	pts, _ := s.RangeSamples(id, 0, 2000)
	if len(pts) != 1 || pts[0].Val != 4096000*1024 {
		t.Fatalf("ram series = %+v", pts)
	}
}

func TestSampleContainersRAMGauge(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	smp := &Sampler{
		Store:  s,
		Proc:   &Proc{Root: os.DirFS("testdata/proc")},
		Cgroup: &Cgroup{Root: os.DirFS("testdata/sys/fs/cgroup")},
	}
	if err := smp.SampleContainers(100); err != nil {
		t.Fatal(err)
	}
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricRAMCtr, Scope: model.ScopeContainer, Entity: "abc123"})
	pts, _ := s.RangeSamples(id, 0, 200)
	if len(pts) != 1 || pts[0].Val != 104857600 {
		t.Fatalf("ctr ram = %+v", pts)
	}
}

func TestSampleDiskIORate(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	smp := &Sampler{Store: s, Proc: &Proc{Root: os.DirFS("testdata/proc")}}
	if err := smp.SampleDisk(100); err != nil {
		t.Fatal(err)
	}
	smp.prevDiskRead, smp.prevDiskWrite, smp.prevDiskTs = 0, 0, 90
	if err := smp.SampleDisk(100); err != nil {
		t.Fatal(err)
	}
	id, _ := s.SeriesID(model.SeriesKey{Metric: model.MetricDiskIO, Scope: model.ScopeDisk, Entity: "read"})
	pts, _ := s.RangeSamples(id, 0, 200)
	if len(pts) != 1 {
		t.Fatalf("disk read rate points = %+v", pts)
	}
}

func TestSampleProcsStores(t *testing.T) {
	s, _ := store.Open(":memory:")
	defer s.Close()
	smp := &Sampler{Store: s, Proc: &Proc{Root: os.DirFS("testdata/proc")}, Procs: &Procs{Root: os.DirFS("testdata/proc"), PageSize: 4096}}
	if err := smp.SampleProcs(100); err != nil {
		t.Fatal(err)
	}
	if err := smp.SampleProcs(110); err != nil {
		t.Fatal(err)
	}
	rows, _ := s.LatestProcs(10)
	if len(rows) == 0 {
		t.Fatalf("expected proc rows")
	}
}
