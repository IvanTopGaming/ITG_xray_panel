package collect

import (
	"os"
	"testing"
)

func procFS(t *testing.T) *Proc {
	t.Helper()
	return &Proc{Root: os.DirFS("testdata/proc")}
}

func TestParseCPU(t *testing.T) {
	p := procFS(t)
	c, err := p.CPU()
	if err != nil {
		t.Fatal(err)
	}
	if c.Total != 1000 || c.Idle != 700 {
		t.Fatalf("cpu = %+v", c)
	}
}

func TestParseMem(t *testing.T) {
	p := procFS(t)
	m, err := p.Mem()
	if err != nil {
		t.Fatal(err)
	}
	if m.TotalBytes != 8192000*1024 || m.UsedBytes != 4096000*1024 {
		t.Fatalf("mem = %+v", m)
	}
}

func TestParseNetDev(t *testing.T) {
	p := procFS(t)
	rx, tx, err := p.NetTotals()
	if err != nil {
		t.Fatal(err)
	}
	if rx != 5000000 || tx != 2000000 {
		t.Fatalf("net rx=%d tx=%d", rx, tx)
	}
}

func TestParseDiskstats(t *testing.T) {
	p := procFS(t)
	rd, wr, err := p.DiskIOBytes()
	if err != nil {
		t.Fatal(err)
	}
	if rd != 8800*512 || wr != 17600*512 {
		t.Fatalf("disk rd=%d wr=%d", rd, wr)
	}
}

func TestIsPartition(t *testing.T) {
	all := []string{"sda", "sda1", "nvme0n1", "nvme0n1p1", "dm-1", "dm-10", "mmcblk0", "mmcblk0p1"}
	cases := map[string]bool{
		"sda": false, "sda1": true, "nvme0n1": false, "nvme0n1p1": true,
		"dm-1": false, "dm-10": false, "mmcblk0": false, "mmcblk0p1": true,
	}
	for name, want := range cases {
		if got := isPartition(name, all); got != want {
			t.Fatalf("isPartition(%q)=%v want %v", name, got, want)
		}
	}
}
