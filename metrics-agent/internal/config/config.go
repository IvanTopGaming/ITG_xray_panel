package config

import "time"

type Config struct {
	Listen        string
	DBPath        string
	ProcPath      string
	SysPath       string
	XrayAPIAddr   string
	HotWindow     time.Duration
	HotWindowVPN  time.Duration
	HotWindowDisk time.Duration
}

type Getenv func(string) (string, bool)

func Load(get Getenv) Config {
	return Config{
		Listen:        str(get, "METRICS_LISTEN", ":9100"),
		DBPath:        str(get, "DB_PATH", "/data/metrics.db"),
		ProcPath:      str(get, "HOST_PROC", "/host/proc"),
		SysPath:       str(get, "HOST_SYS", "/host/sys"),
		XrayAPIAddr:   str(get, "XRAY_API_ADDR", "xray:10085"),
		HotWindow:     dur(get, "HOT_WINDOW", 48*time.Hour),
		HotWindowVPN:  dur(get, "HOT_WINDOW_VPN", 24*time.Hour),
		HotWindowDisk: dur(get, "HOT_WINDOW_DISK", 168*time.Hour),
	}
}

func str(get Getenv, k, def string) string {
	if v, ok := get(k); ok && v != "" {
		return v
	}
	return def
}

func dur(get Getenv, k string, def time.Duration) time.Duration {
	if v, ok := get(k); ok && v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}
