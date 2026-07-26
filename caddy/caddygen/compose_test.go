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
	"variable the bot host's caddy container can see turns another route on. `${PANEL_DOMAIN:-}` does not " +
	"pass an empty string -- compose's `:-` defaults only when the variable is ABSENT, and PANEL_DOMAIN is " +
	"mandatory on a bot host. `env_file: .env` re-injects them all regardless of the environment block. " +
	"A live panel route there answers https://<PANEL_DOMAIN>/<PANEL_SECRET_PATH>/api/... on the bot box's " +
	"IP with bot-api, exposing /api/billing/checkout and /bot-service/*."

type composeService struct {
	Environment []string `yaml:"environment"`
	EnvFile     any      `yaml:"env_file"`
}

type composeFile struct {
	Services map[string]composeService `yaml:"services"`
}

var composeRef = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[-?][^}]*)?\}`)

func botHostEnv(t *testing.T) map[string]string {
	t.Helper()
	data, err := os.ReadFile("../../docker-compose.bot.yml")
	if err != nil {
		t.Fatalf("read docker-compose.bot.yml: %v", err)
	}
	var file composeFile
	if err := yaml.Unmarshal(data, &file); err != nil {
		t.Fatalf("parse docker-compose.bot.yml: %v", err)
	}
	svc, ok := file.Services["caddy"]
	if !ok {
		t.Fatalf("docker-compose.bot.yml has no caddy service; this guard would pass vacuously")
	}
	if svc.EnvFile != nil {
		t.Fatalf("docker-compose.bot.yml caddy declares env_file, which hands it the whole .env.\n%s", composeWhy)
	}
	dotEnv := map[string]string{
		"PANEL_DOMAIN":      "panel.example.com",
		"PROXY_DOMAIN":      "www.google.com",
		"SUB_DOMAIN":        "sub.example.com",
		"BOT_DOMAIN":        "bot.example.com",
		"PANEL_SECRET_PATH": "s3cret",
	}
	env := map[string]string{}
	for _, entry := range svc.Environment {
		key, raw, found := strings.Cut(entry, "=")
		if !found {
			continue
		}
		env[key] = composeRef.ReplaceAllStringFunc(raw, func(m string) string {
			return dotEnv[composeRef.FindStringSubmatch(m)[1]]
		})
	}
	return env
}

func TestBotHostRendersOnlyTheWebhookRoute(t *testing.T) {
	env := botHostEnv(t)
	data, err := os.ReadFile("../routes.yaml")
	if err != nil {
		t.Fatalf("read routes.yaml: %v", err)
	}
	cfg, err := LoadConfig(data, envMap(env))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	var names []string
	for _, r := range cfg.SNIRoutes {
		names = append(names, r.Name+"="+r.Match)
	}
	sort.Strings(names)
	if len(cfg.SNIRoutes) != 1 || cfg.SNIRoutes[0].Name != "bot" {
		t.Fatalf("bot host renders %d layer4 routes %v, want exactly the bot route.\n%s", len(cfg.SNIRoutes), names, composeWhy)
	}

	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	root := jsonValid(t, b)
	l4 := root["apps"].(map[string]any)["layer4"].(map[string]any)["servers"].(map[string]any)["main"].(map[string]any)
	if routes := l4["routes"].([]any); len(routes) != 1 {
		t.Fatalf("rendered %d layer4 routes, want 1.\n%s", len(routes), composeWhy)
	}
	httpServers := root["apps"].(map[string]any)["http"].(map[string]any)["servers"].(map[string]any)
	var servers []string
	for name := range httpServers {
		servers = append(servers, name)
	}
	sort.Strings(servers)
	if strings.Join(servers, ",") != "bot_security_layer,http_redirect" {
		t.Fatalf("bot host http servers = %v, want [bot_security_layer http_redirect].\n%s", servers, composeWhy)
	}

	str := string(b)
	if !containsString(str, "/api/billing/yookassa/webhook") {
		t.Fatalf("the webhook path matcher is missing from the bot host config")
	}
	if containsString(str, "frontend:80") {
		t.Fatalf("the panel SPA upstream leaked onto the bot host.\n%s", composeWhy)
	}
	if containsString(str, "/api/sub/") {
		t.Fatalf("the subscription path leaked onto the bot host.\n%s", composeWhy)
	}
	if containsString(str, "strip_path_prefix") {
		t.Fatalf("the panel secret-path API route leaked onto the bot host.\n%s", composeWhy)
	}
}
