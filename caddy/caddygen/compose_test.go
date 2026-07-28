package main

import (
	"os"
	"regexp"
	"sort"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

const composeWhy = "caddygen drops a route only when its ${VAR} interpolates to the empty string, so every " +
	"domain variable a host's caddy container can see turns another route on, aimed at THAT box's own " +
	"services. `${PANEL_DOMAIN:-}` does not pass an empty string -- compose's `:-` defaults only when the " +
	"variable is ABSENT, and these variables are present on every host whose backend needs them. " +
	"`env_file: .env` re-injects them all regardless of the environment block. Since SNI is chosen by the " +
	"client and the box answers with its certificate for whatever name is asked, a stray route means " +
	"https://<that domain>/... aimed at this box's IP is served by this box's backend."

type composeService struct {
	Environment []string `yaml:"environment"`
	EnvFile     any      `yaml:"env_file"`
}

type composeFile struct {
	Services map[string]composeService `yaml:"services"`
}

var composeRef = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-?][^}]*)?\}`)

// A deliberately fat .env holding every domain in the deployment. The narrowing under test
// must come from each compose file's `environment:` block, not from the operator having
// happened to leave a variable out of that host's file.
var sharedDotEnv = map[string]string{
	"PANEL_DOMAIN":      "panel.example.com",
	"PROXY_DOMAIN":      "www.google.com",
	"SUB_DOMAIN":        "sub.example.com",
	"BOT_DOMAIN":        "bot.example.com",
	"PANEL_SECRET_PATH": "s3cret",
}

type hostCase struct {
	name        string
	compose     string
	wantRoutes  []string
	wantServers string
	mustContain []string
	mustNotHave []string
}

var hostCases = []hostCase{
	{
		name:        "master",
		compose:     "../../docker-compose.master.yml",
		wantRoutes:  []string{"panel"},
		wantServers: "http_redirect,panel_security_layer",
		mustContain: []string{"frontend:80", "strip_path_prefix", "/s3cret/api/"},
		mustNotHave: []string{"/api/sub/", "/api/billing/yookassa/webhook", "xray:443"},
	},
	{
		name:        "node",
		compose:     "../../docker-compose.node.yml",
		wantRoutes:  []string{"proxy", "panel"},
		wantServers: "http_redirect,panel_security_layer",
		mustContain: []string{"xray:443", "frontend:80", "strip_path_prefix"},
		mustNotHave: []string{"/api/sub/", "/api/billing/yookassa/webhook"},
	},
	{
		name:        "sub",
		compose:     "../../docker-compose.sub.yml",
		wantRoutes:  []string{"sub"},
		wantServers: "http_redirect,sub_security_layer",
		mustContain: []string{"/api/sub/", "backend:5000"},
		mustNotHave: []string{"frontend:80", "strip_path_prefix", "xray:443", "/api/billing/yookassa/webhook"},
	},
	{
		name:        "bot",
		compose:     "../../docker-compose.bot.yml",
		wantRoutes:  []string{"bot"},
		wantServers: "bot_security_layer,http_redirect",
		mustContain: []string{"/api/billing/yookassa/webhook", "backend:5000"},
		mustNotHave: []string{"frontend:80", "strip_path_prefix", "/api/sub/", "xray:443"},
	},
}

func hostCaddyEnv(t *testing.T, composePath string) map[string]string {
	t.Helper()
	data, err := os.ReadFile(composePath)
	if err != nil {
		t.Fatalf("read %s: %v", composePath, err)
	}
	var file composeFile
	if err := yaml.Unmarshal(data, &file); err != nil {
		t.Fatalf("parse %s: %v", composePath, err)
	}
	svc, ok := file.Services["caddy"]
	if !ok {
		t.Fatalf("%s has no caddy service; this guard would pass vacuously", composePath)
	}
	if svc.EnvFile != nil {
		t.Fatalf("%s caddy declares env_file, which hands it the whole .env.\n%s", composePath, composeWhy)
	}
	env := map[string]string{}
	for _, entry := range svc.Environment {
		key, raw, found := strings.Cut(entry, "=")
		if !found {
			continue
		}
		env[key] = composeRef.ReplaceAllStringFunc(raw, func(m string) string {
			return sharedDotEnv[composeRef.FindStringSubmatch(m)[1]]
		})
	}
	if len(env) == 0 {
		t.Fatalf("%s caddy declares no environment at all; this guard would pass vacuously", composePath)
	}
	return env
}

func TestEachHostRendersOnlyItsOwnRoutes(t *testing.T) {
	routes, err := os.ReadFile("../routes.yaml")
	if err != nil {
		t.Fatalf("read routes.yaml: %v", err)
	}
	for _, tc := range hostCases {
		t.Run(tc.name, func(t *testing.T) {
			cfg, err := LoadConfig(routes, envMap(hostCaddyEnv(t, tc.compose)))
			if err != nil {
				t.Fatalf("LoadConfig: %v", err)
			}
			var names []string
			for _, r := range cfg.SNIRoutes {
				names = append(names, r.Name+"="+r.Match)
			}
			if len(cfg.SNIRoutes) != len(tc.wantRoutes) {
				t.Fatalf("%s host renders %d layer4 routes %v, want exactly %v.\n%s",
					tc.name, len(cfg.SNIRoutes), names, tc.wantRoutes, composeWhy)
			}
			for i, want := range tc.wantRoutes {
				if cfg.SNIRoutes[i].Name != want {
					t.Fatalf("%s host route %d is %q, want %q (full set %v).\n%s",
						tc.name, i, cfg.SNIRoutes[i].Name, want, names, composeWhy)
				}
			}

			b, err := Generate(cfg)
			if err != nil {
				t.Fatalf("Generate: %v", err)
			}
			root := jsonValid(t, b)
			l4 := root["apps"].(map[string]any)["layer4"].(map[string]any)["servers"].(map[string]any)["main"].(map[string]any)
			if rendered := l4["routes"].([]any); len(rendered) != len(tc.wantRoutes) {
				t.Fatalf("%s host rendered %d layer4 routes, want %d.\n%s",
					tc.name, len(rendered), len(tc.wantRoutes), composeWhy)
			}
			httpServers := root["apps"].(map[string]any)["http"].(map[string]any)["servers"].(map[string]any)
			var servers []string
			for name := range httpServers {
				servers = append(servers, name)
			}
			sort.Strings(servers)
			if strings.Join(servers, ",") != tc.wantServers {
				t.Fatalf("%s host http servers = %v, want %q.\n%s", tc.name, servers, tc.wantServers, composeWhy)
			}

			str := string(b)
			for _, needle := range tc.mustContain {
				if !containsString(str, needle) {
					t.Fatalf("%s host config is missing %q, which is what the host exists to serve", tc.name, needle)
				}
			}
			for _, needle := range tc.mustNotHave {
				if containsString(str, needle) {
					t.Fatalf("%s host config contains %q, which belongs to another host.\n%s",
						tc.name, needle, composeWhy)
				}
			}
		})
	}
}
