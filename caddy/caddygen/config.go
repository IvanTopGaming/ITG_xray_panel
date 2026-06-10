package main

import (
	"os"
	"regexp"

	"gopkg.in/yaml.v3"
)

type Route struct {
	Name        string   `yaml:"name"`
	Match       string   `yaml:"match"`
	Upstream    string   `yaml:"upstream"`
	TLS         bool     `yaml:"tls"`
	OnlyPaths   []string `yaml:"only_paths"`
	APIPath     string   `yaml:"api_path"`
	APIUpstream string   `yaml:"api_upstream"`
}

type Config struct {
	SNIRoutes []Route `yaml:"sni_routes"`
}

var envPattern = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

func interpolate(s string, lookup func(string) string) string {
	return envPattern.ReplaceAllStringFunc(s, func(m string) string {
		key := envPattern.FindStringSubmatch(m)[1]
		return lookup(key)
	})
}

func LoadConfig(data []byte, lookup func(string) string) (*Config, error) {
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	kept := cfg.SNIRoutes[:0]
	for _, r := range cfg.SNIRoutes {
		r.Match = interpolate(r.Match, lookup)
		r.Upstream = interpolate(r.Upstream, lookup)
		r.APIPath = interpolate(r.APIPath, lookup)
		r.APIUpstream = interpolate(r.APIUpstream, lookup)
		if r.Match == "" {
			continue
		}
		if len(r.OnlyPaths) > 0 {
			r.TLS = true
		}
		kept = append(kept, r)
	}
	cfg.SNIRoutes = kept
	return &cfg, nil
}

func osLookup(key string) string {
	return os.Getenv(key)
}
