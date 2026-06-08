package main

import (
	"flag"
	"fmt"
	"os"
)

func main() {
	in := flag.String("in", "/etc/caddy/routes.yaml", "path to routes.yaml")
	out := flag.String("out", "", "output path (default: stdout)")
	flag.Parse()

	data, err := os.ReadFile(*in)
	if err != nil {
		fmt.Fprintf(os.Stderr, "caddygen: read %s: %v\n", *in, err)
		os.Exit(1)
	}
	cfg, err := LoadConfig(data, osLookup)
	if err != nil {
		fmt.Fprintf(os.Stderr, "caddygen: parse: %v\n", err)
		os.Exit(1)
	}
	b, err := Generate(cfg)
	if err != nil {
		fmt.Fprintf(os.Stderr, "caddygen: generate: %v\n", err)
		os.Exit(1)
	}
	if *out == "" {
		os.Stdout.Write(b)
		return
	}
	if err := os.WriteFile(*out, b, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "caddygen: write %s: %v\n", *out, err)
		os.Exit(1)
	}
}
