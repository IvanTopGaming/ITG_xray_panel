package collect

import (
	"os"
	"testing"
)

func TestTopProcesses(t *testing.T) {
	pr := &Procs{Root: os.DirFS("testdata/proc"), PageSize: 4096}
	got, err := pr.Snapshot(10)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("want 2 procs, got %d", len(got))
	}
	if got[0].Comm != "xray" || got[0].CPUJiffies != 2000 {
		t.Fatalf("top = %+v", got[0])
	}
	if got[0].RSSBytes != 43000*4096 {
		t.Fatalf("rss = %d", got[0].RSSBytes)
	}
}
