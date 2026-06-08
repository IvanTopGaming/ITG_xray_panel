package main

import (
	"os"
	"regexp"

	"gopkg.in/yaml.v3"
)

// Route is one declarative SNI route from routes.yaml.
type Route struct {
	Name      string   `yaml:"name"`
	Match     string   `yaml:"match"`
	Upstream  string   `yaml:"upstream"`
	TLS       bool     `yaml:"tls"`
	OnlyPaths []string `yaml:"only_paths"`
}

// Config is the parsed routes.yaml.
type Config struct {
	SNIRoutes []Route `yaml:"sni_routes"`
}

var envPattern = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}`)

// interpolate replaces ${VAR} with lookup(VAR) (empty string if absent).
func interpolate(s string, lookup func(string) string) string {
	return envPattern.ReplaceAllStringFunc(s, func(m string) string {
		key := envPattern.FindStringSubmatch(m)[1]
		return lookup(key)
	})
}

// LoadConfig parses YAML bytes and interpolates ${ENV} in match/upstream using lookup.
// Routes whose Match is empty after interpolation are dropped.
func LoadConfig(data []byte, lookup func(string) string) (*Config, error) {
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	kept := cfg.SNIRoutes[:0]
	for _, r := range cfg.SNIRoutes {
		r.Match = interpolate(r.Match, lookup)
		r.Upstream = interpolate(r.Upstream, lookup)
		if r.Match == "" {
			continue
		}
		if len(r.OnlyPaths) > 0 {
			r.TLS = true // path filtering requires HTTP termination
		}
		kept = append(kept, r)
	}
	cfg.SNIRoutes = kept
	return &cfg, nil
}

// osLookup is the production env lookup.
func osLookup(key string) string {
	return os.Getenv(key)
}
