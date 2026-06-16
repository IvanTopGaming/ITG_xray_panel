package config

import (
	"testing"
	"time"
)

func TestDefaults(t *testing.T) {
	c := Load(func(string) (string, bool) { return "", false })
	if c.Listen != ":9100" {
		t.Fatalf("Listen = %q", c.Listen)
	}
	if c.DBPath != "/data/metrics.db" {
		t.Fatalf("DBPath = %q", c.DBPath)
	}
	if c.HotWindow != 48*time.Hour {
		t.Fatalf("HotWindow = %v", c.HotWindow)
	}
	if c.HotWindowVPN != 24*time.Hour {
		t.Fatalf("HotWindowVPN = %v", c.HotWindowVPN)
	}
	if c.HotWindowDisk != 168*time.Hour {
		t.Fatalf("HotWindowDisk = %v", c.HotWindowDisk)
	}
	if c.XrayAPIAddr != "xray:10085" {
		t.Fatalf("XrayAPIAddr = %q", c.XrayAPIAddr)
	}
}

func TestOverrides(t *testing.T) {
	env := map[string]string{
		"METRICS_LISTEN": ":9999",
		"HOT_WINDOW":     "12h",
		"HOST_PROC":      "/custom/proc",
	}
	c := Load(func(k string) (string, bool) { v, ok := env[k]; return v, ok })
	if c.Listen != ":9999" || c.HotWindow != 12*time.Hour || c.ProcPath != "/custom/proc" {
		t.Fatalf("override failed: %+v", c)
	}
}
