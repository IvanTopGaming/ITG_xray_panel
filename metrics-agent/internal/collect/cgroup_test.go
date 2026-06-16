package collect

import (
	"os"
	"testing"
)

func cgFS(t *testing.T) *Cgroup {
	t.Helper()
	return &Cgroup{Root: os.DirFS("testdata/sys/fs/cgroup")}
}

func TestDiscoverContainers(t *testing.T) {
	cg := cgFS(t)
	ids, err := cg.Containers()
	if err != nil {
		t.Fatal(err)
	}
	found := map[string]bool{}
	for _, id := range ids {
		found[id] = true
	}
	if !found["abc123"] {
		t.Fatalf("ids = %+v", ids)
	}
}

func TestContainerStats(t *testing.T) {
	cg := cgFS(t)
	st, err := cg.Stats("abc123")
	if err != nil {
		t.Fatal(err)
	}
	if st.CPUUsec != 5000000 || st.MemBytes != 104857600 || st.IOBytes != 1024+2048 {
		t.Fatalf("stats = %+v", st)
	}
}

func TestDiscoverBothDrivers(t *testing.T) {
	cg := cgFS(t)
	ids, err := cg.Containers()
	if err != nil {
		t.Fatal(err)
	}
	found := map[string]bool{}
	for _, id := range ids {
		found[id] = true
	}
	if !found["abc123"] || !found["deadbeef"] {
		t.Fatalf("ids = %+v", ids)
	}
	st, err := cg.Stats("deadbeef")
	if err != nil || st.CPUUsec != 7000000 || st.MemBytes != 52428800 || st.IOBytes != 1024 {
		t.Fatalf("deadbeef stats = %+v err=%v", st, err)
	}
}
