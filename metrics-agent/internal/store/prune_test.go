package store

import "testing"

func TestPruneRollup1mAndProc(t *testing.T) {
	s := openTest(t)
	s.db.Exec(`INSERT INTO rollup_1m(series_id,minute_ts,avg,max,min) VALUES (1,100,1,1,1),(1,5000,1,1,1)`)
	s.db.Exec(`INSERT INTO proc_sample(ts,pid,comm,cpu_pct,rss_bytes) VALUES (100,1,'x',1,1),(5000,2,'y',1,1)`)
	if err := s.Prune(4000, 4000); err != nil {
		t.Fatal(err)
	}
	var rc, pc int
	s.db.QueryRow(`SELECT COUNT(*) FROM rollup_1m`).Scan(&rc)
	s.db.QueryRow(`SELECT COUNT(*) FROM proc_sample`).Scan(&pc)
	if rc != 1 || pc != 1 {
		t.Fatalf("rollup=%d proc=%d, want 1/1", rc, pc)
	}
}
