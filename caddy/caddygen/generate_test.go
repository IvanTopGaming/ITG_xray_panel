package main

import (
	"encoding/json"
	"os"
	"testing"
)

func envMap(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

const routesYAML = `
sni_routes:
  - name: proxy
    match: "${PROXY_DOMAIN}"
    upstream: "xray:443"
  - name: panel
    match: "${PANEL_DOMAIN}"
    upstream: "frontend:80"
    tls: true
  - name: sub
    match: "${SUB_DOMAIN}"
    upstream: "backend:5000"
    tls: true
    only_paths:
      - "/api/sub/"
`

func TestLoadConfig_DropsEmptySub(t *testing.T) {
	cfg, err := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if len(cfg.SNIRoutes) != 2 {
		t.Fatalf("want 2 routes (sub dropped), got %d", len(cfg.SNIRoutes))
	}
	for _, r := range cfg.SNIRoutes {
		if r.Name == "sub" {
			t.Fatal("sub route should have been dropped")
		}
	}
}

func TestLoadConfig_KeepsSubWhenSet(t *testing.T) {
	cfg, err := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig: %v", err)
	}
	if len(cfg.SNIRoutes) != 3 {
		t.Fatalf("want 3 routes, got %d", len(cfg.SNIRoutes))
	}
	var sub *Route
	for i := range cfg.SNIRoutes {
		if cfg.SNIRoutes[i].Name == "sub" {
			sub = &cfg.SNIRoutes[i]
		}
	}
	if sub == nil {
		t.Fatal("sub route missing")
	}
	if sub.Match != "sub.example.com" || sub.Upstream != "backend:5000" {
		t.Fatalf("sub route not interpolated: %+v", sub)
	}
	if !sub.TLS || len(sub.OnlyPaths) != 1 {
		t.Fatalf("sub route flags wrong: %+v", sub)
	}
}

func jsonValid(t *testing.T, b []byte) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	return out
}

func TestGenerate_WithSub(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	root := jsonValid(t, b)

	apps := root["apps"].(map[string]any)
	l4 := apps["layer4"].(map[string]any)
	servers := l4["servers"].(map[string]any)
	main := servers["main"].(map[string]any)
	routes := main["routes"].([]any)
	if len(routes) != 3 {
		t.Fatalf("want 3 layer4 routes, got %d", len(routes))
	}

	httpApp := apps["http"].(map[string]any)
	httpServers := httpApp["servers"].(map[string]any)

	if len(httpServers) != 3 {
		t.Fatalf("want 3 http servers, got %d", len(httpServers))
	}
}

func TestGenerate_WithoutSub(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	root := jsonValid(t, b)
	apps := root["apps"].(map[string]any)
	l4 := apps["layer4"].(map[string]any)
	main := l4["servers"].(map[string]any)["main"].(map[string]any)
	if len(main["routes"].([]any)) != 2 {
		t.Fatal("want 2 layer4 routes without sub")
	}
	httpServers := apps["http"].(map[string]any)["servers"].(map[string]any)

	if len(httpServers) != 2 {
		t.Fatalf("want 2 http servers (panel + redirect) without sub, got %d", len(httpServers))
	}
	if containsString(string(b), "backend:5000") {
		t.Fatal("backend upstream leaked without SUB_DOMAIN")
	}
}

func TestGenerate_HTTPRedirectOn80(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	redirect := apps(t, b)["http"].(map[string]any)["servers"].(map[string]any)["http_redirect"]
	if redirect == nil {
		t.Fatal("http_redirect server missing")
	}
	srv := redirect.(map[string]any)
	if listen := srv["listen"].([]any); len(listen) != 1 || listen[0] != ":80" {
		t.Fatalf("redirect server must listen on :80, got %v", srv["listen"])
	}
	str := string(b)
	if !containsString(str, "\"status_code\": 308") {
		t.Fatal("redirect must use 308")
	}
	if !containsString(str, "https://{http.request.host}{http.request.uri}") {
		t.Fatal("redirect Location target missing")
	}
}

func apps(t *testing.T, b []byte) map[string]any {
	t.Helper()
	return jsonValid(t, b)["apps"].(map[string]any)
}

func TestGenerate_SubServerPathFilters(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	b, _ := Generate(cfg)
	str := string(b)
	if !containsString(str, "/api/sub/*") {
		t.Fatal("sub path matcher missing")
	}
	if !containsString(str, "backend:5000") {
		t.Fatal("backend upstream missing")
	}
	if !containsString(str, "\"status_code\": 404") && !containsString(str, "\"status_code\":404") {
		t.Fatal("404 catch-all missing for sub server")
	}
}

func TestGenerate_NoDeadXRealIP(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAML), envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	b, _ := Generate(cfg)
	str := string(b)

	if containsString(str, "X-Real-IP") {
		t.Fatal("dead X-Real-IP header should be gone")
	}
	if containsString(str, "client_ip") {
		t.Fatal("stale http.vars.client_ip placeholder should be gone")
	}
	if !containsString(str, "X-Forwarded-Proto") {
		t.Fatal("X-Forwarded-Proto must remain")
	}
}

func TestGenerate_DefaultRoutesFileValidJSON(t *testing.T) {
	data, err := os.ReadFile("../routes.yaml")
	if err != nil {
		t.Skipf("routes.yaml not found: %v", err)
	}
	cfg, err := LoadConfig(data, envMap(map[string]string{
		"PROXY_DOMAIN": "proxy.example.com",
		"PANEL_DOMAIN": "panel.example.com",
		"SUB_DOMAIN":   "sub.example.com",
	}))
	if err != nil {
		t.Fatalf("LoadConfig real routes.yaml: %v", err)
	}
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	jsonValid(t, b)
}

const routesYAMLWithAPI = `
sni_routes:
  - name: panel
    match: "${PANEL_DOMAIN}"
    upstream: "frontend:80"
    tls: true
    api_path: "/${PANEL_SECRET_PATH}/api/"
    api_upstream: "backend:5000"
`

func TestGenerate_PanelAPIRoute(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAMLWithAPI), envMap(map[string]string{
		"PANEL_DOMAIN":      "panel.example.com",
		"PANEL_SECRET_PATH": "s3cret",
	}))
	b, err := Generate(cfg)
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	str := string(b)
	if !containsString(str, "/s3cret/api/*") {
		t.Fatal("panel API path matcher missing")
	}
	if !containsString(str, "strip_path_prefix") || !containsString(str, "/s3cret") {
		t.Fatal("strip_path_prefix for secret prefix missing")
	}
	if !containsString(str, "backend:5000") {
		t.Fatal("panel API backend upstream missing")
	}
	if !containsString(str, "frontend:80") {
		t.Fatal("panel SPA upstream missing")
	}
}

func TestGenerate_PanelAPIRouteSkippedWithoutSecret(t *testing.T) {
	cfg, _ := LoadConfig([]byte(routesYAMLWithAPI), envMap(map[string]string{
		"PANEL_DOMAIN": "panel.example.com",
	}))
	b, _ := Generate(cfg)
	if containsString(string(b), "backend:5000") {
		t.Fatal("API route must be skipped when PANEL_SECRET_PATH is empty")
	}
}

func containsString(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (indexOf(haystack, needle) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
