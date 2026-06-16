package store

import "testing"

func openTest(t *testing.T) *Store {
	t.Helper()
	s, err := Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

func TestSchemaTablesExist(t *testing.T) {
	s := openTest(t)
	want := []string{"series", "sample", "raw_archive", "rollup_1m", "rollup_1h", "proc_sample"}
	for _, name := range want {
		var got string
		err := s.db.QueryRow(
			`SELECT name FROM sqlite_master WHERE type='table' AND name=?`, name,
		).Scan(&got)
		if err != nil {
			t.Fatalf("table %q missing: %v", name, err)
		}
	}
}
