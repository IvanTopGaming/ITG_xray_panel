package main

import (
	"encoding/json"
	"fmt"
)

// securityHeaders mirrors the static response-header set from the original caddy.json.
func securityHeaders() map[string]any {
	return map[string]any{
		"handler": "headers",
		"response": map[string]any{
			"set": map[string]any{
				"Strict-Transport-Security": []any{"max-age=63072000; includeSubDomains; preload"},
				"X-XSS-Protection":          []any{"1; mode=block"},
				"X-Content-Type-Options":    []any{"nosniff"},
				"X-Frame-Options":           []any{"SAMEORIGIN"},
				"Referrer-Policy":           []any{"strict-origin-when-cross-origin"},
				"Content-Security-Policy": []any{
					"default-src 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'; " +
						"script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; " +
						"font-src 'self' https://fonts.gstatic.com data:; img-src 'self' https: data: blob:; " +
						"connect-src 'self' https: wss:;",
				},
			},
		},
	}
}

// reverseProxy builds a reverse_proxy handler to a single upstream with standard request headers.
func reverseProxy(upstream string) map[string]any {
	return map[string]any{
		"handler":   "reverse_proxy",
		"upstreams": []any{map[string]any{"dial": upstream}},
		"headers": map[string]any{
			"request": map[string]any{
				"set": map[string]any{
					"X-Forwarded-Proto": []any{"https"},
				},
			},
		},
	}
}

// httpServer builds one loopback HTTP security-layer server.
// If onlyPaths is non-empty, requests outside those prefixes get a 404.
func httpServer(listen string, upstream string, onlyPaths []string) map[string]any {
	var routes []any
	if len(onlyPaths) > 0 {
		globs := make([]any, 0, len(onlyPaths))
		for _, p := range onlyPaths {
			globs = append(globs, p+"*")
		}
		routes = append(routes, map[string]any{
			"match":  []any{map[string]any{"path": globs}},
			"handle": []any{securityHeaders(), reverseProxy(upstream)},
		})
		routes = append(routes, map[string]any{
			"handle": []any{map[string]any{"handler": "static_response", "status_code": 404}},
		})
	} else {
		routes = append(routes, map[string]any{
			"handle": []any{securityHeaders(), reverseProxy(upstream)},
		})
	}
	return map[string]any{
		"listen": []any{listen},
		"listener_wrappers": []any{
			map[string]any{"wrapper": "proxy_protocol", "allow": []any{"127.0.0.1/32"}},
		},
		"routes": routes,
	}
}

// httpRedirectServer: a plain :80 server that 308-redirects every request to
// its https:// equivalent. Caddy's own automatic-HTTPS redirect is disabled so
// it doesn't try to manage :80/:443 itself (layer4 owns :443).
func httpRedirectServer() map[string]any {
	return map[string]any{
		"listen":          []any{":80"},
		"automatic_https": map[string]any{"disable": true},
		"routes": []any{
			map[string]any{
				"handle": []any{
					map[string]any{
						"handler":     "static_response",
						"status_code": 308,
						"headers": map[string]any{
							"Location": []any{"https://{http.request.host}{http.request.uri}"},
						},
					},
				},
			},
		},
	}
}

// layer4Passthrough: raw TCP proxy with PROXY protocol v2 to upstream.
func layer4Passthrough(sni, upstream string) map[string]any {
	return map[string]any{
		"match": []any{map[string]any{"tls": map[string]any{"sni": []any{sni}}}},
		"handle": []any{map[string]any{
			"handler":        "proxy",
			"proxy_protocol": "v2",
			"upstreams":      []any{map[string]any{"dial": []any{upstream}}},
		}},
	}
}

// layer4TLS: terminate TLS (alpn http/1.1) then PROXY-protocol to a loopback http server.
func layer4TLS(sni, loopback string) map[string]any {
	return map[string]any{
		"match": []any{map[string]any{"tls": map[string]any{"sni": []any{sni}}}},
		"handle": []any{
			map[string]any{
				"handler":             "tls",
				"connection_policies": []any{map[string]any{"alpn": []any{"http/1.1"}}},
			},
			map[string]any{
				"handler":        "proxy",
				"proxy_protocol": "v2",
				"upstreams":      []any{map[string]any{"dial": []any{loopback}}},
			},
		},
	}
}

// Generate builds the full Caddy JSON config from the parsed routes.
func Generate(cfg *Config) ([]byte, error) {
	httpServers := map[string]any{}
	var l4routes []any
	port := 8081

	for _, r := range cfg.SNIRoutes {
		if !r.TLS {
			l4routes = append(l4routes, layer4Passthrough(r.Match, r.Upstream))
			continue
		}
		loopback := fmt.Sprintf("127.0.0.1:%d", port)
		name := r.Name
		if name == "" {
			name = fmt.Sprintf("srv%d", port)
		}
		httpServers[name+"_security_layer"] = httpServer(loopback, r.Upstream, r.OnlyPaths)
		l4routes = append(l4routes, layer4TLS(r.Match, loopback))
		port++
	}

	// Plain :80 server that redirects everything to https (camouflage + UX).
	httpServers["http_redirect"] = httpRedirectServer()

	root := map[string]any{
		"apps": map[string]any{
			"tls": map[string]any{
				"certificates": map[string]any{
					"load_files": []any{
						map[string]any{
							"certificate": "/root/cert/fullchain.pem",
							"key":         "/root/cert/key.pem",
						},
					},
				},
			},
			"http": map[string]any{
				"servers": httpServers,
			},
			"layer4": map[string]any{
				"servers": map[string]any{
					"main": map[string]any{
						"listen": []any{":443"},
						"routes": l4routes,
					},
				},
			},
		},
	}
	return json.MarshalIndent(root, "", "\t")
}
